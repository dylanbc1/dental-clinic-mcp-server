"""Write tools over MRTR, end to end on the wire.

The property under test throughout: **the first call changes nothing**. It comes
back asking a person, and only the retry carrying their answer mutates anything.
Every test checks the database afterwards rather than trusting the tool's word.
"""

from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.domain.services import book_appointment, join_waiting_list
from backend.enums import AppointmentState, SlotState, Specialty
from backend.models import AgendaSlot, Appointment
from tests.conftest import SUBJECT, MCPTestClient, Scenario, ToolCallError, as_caller

pytestmark = pytest.mark.integration

ESCRITURA = ["read", "write"]


def contar_citas(session_: Session) -> int:
    return session_.scalar(select(func.count()).select_from(Appointment)) or 0


@pytest.fixture
def existing_appointment(backend_session: Session, scenario: Scenario) -> int:
    result = book_appointment(
        backend_session,
        patient_id=scenario.ana_id,
        slot_id=scenario.slots_general[0],
        user="setup",
    )
    backend_session.commit()
    return result.appointment.id


class TestLaPrimeraLlamadaNoEjecuta:
    async def test_agendar_pide_confirmacion_y_no_crea_nada(
        self, mcp: MCPTestClient, backend_session: Session, scenario: Scenario
    ) -> None:
        args = {"patient_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, ESCRITURA):
            question = await mcp.ask("book_appointment", args)

        assert question["resultType"] == "input_required"
        assert question["requestState"]
        assert contar_citas(backend_session) == 0
        backend_session.expire_all()
        assert backend_session.get(AgendaSlot, scenario.slots_general[0]).status is SlotState.FREE

    async def test_la_pregunta_describe_lo_que_va_a_pasar(
        self, mcp: MCPTestClient, scenario: Scenario
    ) -> None:
        """Read aloud to a receptionist, so it names the hour and the
        professional rather than a slot id."""
        args = {"patient_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, ESCRITURA):
            message = mcp.question_text(await mcp.ask("book_appointment", args))

        assert str(scenario.fecha_futura) in message
        assert "Dra. General" in message
        assert "Esto va a pasar:" in message
        assert "¿Confirmas la operación?" in message

    async def test_la_pregunta_pide_un_booleano_y_nada_mas(
        self, mcp: MCPTestClient, scenario: Scenario
    ) -> None:
        """A form with more fields invites changing the operation instead of
        judging it."""
        args = {"patient_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, ESCRITURA):
            question = await mcp.ask("book_appointment", args)
        key = next(iter(question["inputRequests"]))
        esquema = question["inputRequests"][key]["params"]["requestedSchema"]
        assert set(esquema["properties"]) == {"confirmed"}
        assert esquema["properties"]["confirmed"]["type"] == "boolean"

    async def test_advierte_de_la_afiliacion_inactiva(
        self, mcp: MCPTestClient, scenario: Scenario
    ) -> None:
        args = {"patient_id": scenario.bruno_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, ESCRITURA):
            message = mcp.question_text(await mcp.ask("book_appointment", args))
        assert "inactiva" in message
        assert "tarifa particular" in message

    async def test_advierte_de_la_mora_sin_bloquear(
        self, mcp: MCPTestClient, scenario: Scenario
    ) -> None:
        args = {"patient_id": scenario.deudor_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, ESCRITURA):
            question = await mcp.ask("book_appointment", args)
        message = mcp.question_text(question)
        assert "mora" in message
        assert "No impide agendar" in message
        assert question["requestState"], "la operación sigue disponible para aprobar"

    async def test_cancelar_pregunta_sin_cancelar(
        self, mcp: MCPTestClient, backend_session: Session, existing_appointment: int
    ) -> None:
        args = {"appointment_id": existing_appointment, "reason": "El paciente viajó"}
        with as_caller(SUBJECT, ESCRITURA):
            await mcp.ask("cancel_appointment", args)
        backend_session.expire_all()
        assert (
            backend_session.get(Appointment, existing_appointment).status
            is AppointmentState.SCHEDULED
        )


class TestLaSegundaLlamadaEjecuta:
    async def test_el_ciclo_completo_agenda_de_verdad(
        self, mcp: MCPTestClient, backend_session: Session, scenario: Scenario
    ) -> None:
        args = {"patient_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, ESCRITURA):
            result = await mcp.aprobar("book_appointment", args)

        assert result["appointment"]["status"] == "scheduled"
        assert contar_citas(backend_session) == 1

    async def test_el_actor_del_token_queda_en_la_auditoria_del_backend(
        self, mcp: MCPTestClient, scenario: Scenario
    ) -> None:
        """The audit row must name the human's subject, not "mcp-server"."""
        args = {"patient_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller("dra.ospina@clinica.test", ESCRITURA):
            result = await mcp.aprobar("book_appointment", args)
        assert result["appointment"]["history"][0]["user"] == "dra.ospina@clinica.test"

    async def test_cancelar_libera_el_cupo(
        self,
        mcp: MCPTestClient,
        backend_session: Session,
        existing_appointment: int,
        scenario: Scenario,
    ) -> None:
        args = {"appointment_id": existing_appointment, "reason": "El paciente viajó"}
        with as_caller(SUBJECT, ESCRITURA):
            result = await mcp.aprobar("cancel_appointment", args)
        assert result["freed_slot"] is True
        backend_session.expire_all()
        assert backend_session.get(AgendaSlot, scenario.slots_general[0]).status is SlotState.FREE

    async def test_confirmar(
        self, mcp: MCPTestClient, backend_session: Session, existing_appointment: int
    ) -> None:
        with as_caller(SUBJECT, ESCRITURA):
            result = await mcp.aprobar(
                "confirm_appointment", {"appointment_id": existing_appointment}
            )
        assert result["new_status"] == "confirmed"
        backend_session.expire_all()
        assert (
            backend_session.get(Appointment, existing_appointment).status
            is AppointmentState.CONFIRMED
        )

    async def test_record_attendance_genera_el_cargo(
        self, mcp: MCPTestClient, backend_session: Session, existing_appointment: int
    ) -> None:
        from backend.domain.services import confirm_appointment, record_attendance

        confirm_appointment(backend_session, existing_appointment, user="setup")
        record_attendance(
            backend_session, existing_appointment, AppointmentState.WAITING, user="setup"
        )
        backend_session.commit()

        with as_caller(SUBJECT, ESCRITURA):
            result = await mcp.aprobar(
                "record_attendance", {"appointment_id": existing_appointment, "status": "attended"}
            )
        assert result["created_charge"] is True
        assert result["charge"]["concept"] == "cuota_moderadora"

    async def test_reprogramar_encadena_la_cita_nueva(
        self,
        mcp: MCPTestClient,
        backend_session: Session,
        existing_appointment: int,
        scenario: Scenario,
    ) -> None:
        args = {"appointment_id": existing_appointment, "new_slot_id": scenario.slots_general[2]}
        with as_caller(SUBJECT, ESCRITURA):
            result = await mcp.aprobar("reschedule_appointment", args)
        assert result["appointment"]["source_appointment_id"] == existing_appointment
        backend_session.expire_all()
        assert backend_session.get(AgendaSlot, scenario.slots_general[0]).status is SlotState.FREE

    async def test_ofrecer_cupo_contacta_sin_agendar(
        self, mcp: MCPTestClient, backend_session: Session, scenario: Scenario
    ) -> None:
        join_waiting_list(
            backend_session,
            patient_id=scenario.carla_id,
            specialty=Specialty.ORTHODONTICS,
        )
        backend_session.commit()

        with as_caller(SUBJECT, ESCRITURA):
            question = await mcp.ask(
                "offer_slot_to_waiting_list", {"slot_id": scenario.slots_orto[0]}
            )
            assert "NO se agenda" in mcp.question_text(question)
            oferta = await mcp.respond(
                "offer_slot_to_waiting_list",
                {"slot_id": scenario.slots_orto[0]},
                question,
            )

        assert oferta["patient_id"] == scenario.carla_id
        assert oferta["phone"]
        assert contar_citas(backend_session) == 0


class TestCuandoLaPersonaDiceQueNo:
    async def test_un_false_explicito_aborta_sin_tocar_nada(
        self, mcp: MCPTestClient, backend_session: Session, scenario: Scenario
    ) -> None:
        args = {"patient_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, ESCRITURA):
            question = await mcp.ask("book_appointment", args)
            with pytest.raises(ToolCallError) as exc:
                await mcp.respond("book_appointment", args, question, confirmed=False)

        assert "OPERACION_NO_APROBADA" in exc.value.text_of
        assert "Nothing was changed" in exc.value.text_of
        assert contar_citas(backend_session) == 0

    async def test_el_rechazo_pide_no_reintentar(
        self, mcp: MCPTestClient, scenario: Scenario
    ) -> None:
        """Retrying an operation a person declined is how an agent nags."""
        args = {"patient_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, ESCRITURA):
            question = await mcp.ask("book_appointment", args)
            with pytest.raises(ToolCallError) as exc:
                await mcp.respond("book_appointment", args, question, confirmed=False)
        assert "Do not retry" in exc.value.text_of

    async def test_declinar_la_elicitacion_tambien_aborta(
        self, mcp: MCPTestClient, backend_session: Session, scenario: Scenario
    ) -> None:
        """The client can decline instead of answering. The call must stop."""
        args = {"patient_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, ESCRITURA):
            question = await mcp.ask("book_appointment", args)
            with pytest.raises(ToolCallError):
                await mcp.respond("book_appointment", args, question, action="decline")
        assert contar_citas(backend_session) == 0


class TestValidaAntesDePreguntar:
    """A question a human cannot act on is worse than an error.

    Every check here is repeated on the second round, because the resolver runs
    again. These exist so nobody is asked to approve something that will fail.
    """

    async def test_no_pregunta_por_un_cupo_ya_ocupado(
        self, mcp: MCPTestClient, scenario: Scenario, existing_appointment: int
    ) -> None:
        with as_caller(SUBJECT, ESCRITURA), pytest.raises(ToolCallError) as exc:
            await mcp.ask(
                "book_appointment",
                {"patient_id": scenario.carla_id, "slot_id": scenario.slots_general[0]},
            )
        assert "SLOT_UNAVAILABLE" in exc.value.text_of
        assert "closest free slots" in exc.value.text_of

    async def test_no_pregunta_por_un_cupo_en_el_pasado(
        self, mcp: MCPTestClient, scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ESCRITURA), pytest.raises(ToolCallError) as exc:
            await mcp.ask(
                "book_appointment",
                {"patient_id": scenario.ana_id, "slot_id": scenario.slot_pasado_id},
            )
        assert "SLOT_IN_THE_PAST" in exc.value.text_of

    async def test_no_pregunta_por_un_cruce_de_horario(
        self, mcp: MCPTestClient, backend_session: Session, scenario: Scenario
    ) -> None:
        book_appointment(
            backend_session,
            patient_id=scenario.ana_id,
            slot_id=scenario.slots_general[0],
            user="setup",
        )
        backend_session.commit()
        with as_caller(SUBJECT, ESCRITURA), pytest.raises(ToolCallError) as exc:
            await mcp.ask(
                "book_appointment",
                {"patient_id": scenario.ana_id, "slot_id": scenario.slots_orto[0]},
            )
        assert "PATIENT_ALREADY_BOOKED" in exc.value.text_of

    async def test_no_pregunta_por_una_transicion_imposible(
        self, mcp: MCPTestClient, existing_appointment: int
    ) -> None:
        with as_caller(SUBJECT, ESCRITURA), pytest.raises(ToolCallError) as exc:
            await mcp.ask(
                "record_attendance", {"appointment_id": existing_appointment, "status": "attended"}
            )
        assert "INVALID_TRANSITION" in exc.value.text_of
        assert "confirmed" in exc.value.text_of

    async def test_un_estado_de_asistencia_inventado_se_rechaza(
        self, mcp: MCPTestClient, existing_appointment: int
    ) -> None:
        with as_caller(SUBJECT, ESCRITURA), pytest.raises(ToolCallError) as exc:
            await mcp.ask(
                "record_attendance", {"appointment_id": existing_appointment, "status": "cancelled"}
            )
        assert "is not an attendance state" in exc.value.text_of

    async def test_una_cita_inexistente_falla_antes_de_preguntar(
        self, mcp: MCPTestClient, scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ESCRITURA), pytest.raises(ToolCallError) as exc:
            await mcp.ask("confirm_appointment", {"appointment_id": 424242})
        assert "APPOINTMENT_NOT_FOUND" in exc.value.text_of


class TestLaValidacionSeRepiteAlEjecutar:
    async def test_la_aprobacion_no_legaliza_lo_que_dejo_de_ser_legal(
        self, mcp: MCPTestClient, backend_session: Session, existing_appointment: int
    ) -> None:
        """The state can change between the two rounds.

        The resolver runs again on the retry, so the domain refuses even though
        the human approved. Approval authorises an action; it does not freeze the
        world it saw.
        """
        from backend.domain.services import cancel_appointment

        with as_caller(SUBJECT, ESCRITURA):
            question = await mcp.ask(
                "confirm_appointment", {"appointment_id": existing_appointment}
            )

            cancel_appointment(
                backend_session, existing_appointment, reason="urgencia", user="otro"
            )
            backend_session.commit()

            with pytest.raises(ToolCallError) as exc:
                await mcp.respond(
                    "confirm_appointment", {"appointment_id": existing_appointment}, question
                )
        assert "INVALID_TRANSITION" in exc.value.text_of or "estado final" in exc.value.text_of

    async def test_el_cupo_tomado_en_medio_se_detecta(
        self, mcp: MCPTestClient, backend_session: Session, scenario: Scenario
    ) -> None:
        args = {"patient_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, ESCRITURA):
            question = await mcp.ask("book_appointment", args)

            book_appointment(
                backend_session,
                patient_id=scenario.carla_id,
                slot_id=scenario.slots_general[0],
                user="otro",
            )
            backend_session.commit()

            with pytest.raises(ToolCallError) as exc:
                await mcp.respond("book_appointment", args, question)
        assert "SLOT_UNAVAILABLE" in exc.value.text_of


class TestAuditoria:
    async def test_la_pregunta_y_la_ejecucion_se_registran(
        self, mcp: MCPTestClient, ctx: Any, scenario: Scenario
    ) -> None:
        args = {"patient_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, ESCRITURA):
            await mcp.aprobar("book_appointment", args)

        resultados = [e["result"] for e in ctx.auditor.events if e["event"] == "tool.invocation"]
        # MRTR means two calls arrive per mutation, and the log records calls.
        assert resultados.count("input_required") == 2
        assert resultados[-1] == "ok"

    async def test_la_ejecucion_queda_marcada_como_aprobada(
        self, mcp: MCPTestClient, ctx: Any, scenario: Scenario
    ) -> None:
        args = {"patient_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, ESCRITURA):
            await mcp.aprobar("book_appointment", args)
        assert ctx.auditor.events[-1]["with_human_approval"] is True

    async def test_un_rechazo_por_validacion_queda_en_el_log(
        self, mcp: MCPTestClient, ctx: Any, existing_appointment: int
    ) -> None:
        """A log that records only what succeeded cannot tell you an agent spent
        an hour asking for something impossible."""
        with as_caller(SUBJECT, ESCRITURA), pytest.raises(ToolCallError):
            await mcp.ask(
                "record_attendance", {"appointment_id": existing_appointment, "status": "attended"}
            )
        evento = ctx.auditor.events[-1]
        assert evento["result"] == "error"
        assert evento["error_code"] == "INVALID_TRANSITION"

    async def test_el_motivo_no_se_copia_al_log(
        self, mcp: MCPTestClient, ctx: Any, existing_appointment: int
    ) -> None:
        secreto = "sangrado persistente desde el martes"
        with as_caller(SUBJECT, ESCRITURA):
            await mcp.ask(
                "cancel_appointment", {"appointment_id": existing_appointment, "reason": secreto}
            )
        assert secreto not in str(ctx.auditor.events)
        assert ctx.auditor.events[-1]["arguments"]["reason"] == "«redacted»"

    async def test_el_request_state_no_se_copia_al_log(
        self, mcp: MCPTestClient, ctx: Any, scenario: Scenario
    ) -> None:
        """A logged request state is a redeemable approval sitting in a log."""
        args = {"patient_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, ESCRITURA):
            question = await mcp.ask("book_appointment", args)
        assert question["requestState"] not in str(ctx.auditor.events)


class TestUnClienteQueNoPuedeConfirmar:
    """Not every client speaks 2026-07-28 yet, and one that does not deserves to
    be told which half of this server it can still use."""

    async def test_las_escrituras_se_rechazan_con_un_mensaje_util(
        self, mcp_without_elicitation: MCPTestClient, scenario: Scenario
    ) -> None:
        args = {"patient_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, ESCRITURA), pytest.raises(ToolCallError) as exc:
            await mcp_without_elicitation.call_tool("book_appointment", args)

        message = exc.value.text_of
        assert "CLIENT_CANNOT_CONFIRM" in message
        # Not a transport error the reader cannot act on.
        assert "back-channel" not in message
        assert "elicitation" in message
        assert "Read tools work" in message

    async def test_las_lecturas_siguen_funcionando(
        self, mcp_without_elicitation: MCPTestClient, scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ESCRITURA):
            patients = await mcp_without_elicitation.call_tool(
                "search_patients", {"document_number": scenario.ana_documento}
            )
        assert [p["id"] for p in patients] == [scenario.ana_id]

    async def test_la_clinica_tambien_se_rechaza(
        self, mcp_without_elicitation: MCPTestClient, scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, [*ESCRITURA, "clinical"]), pytest.raises(ToolCallError) as exc:
            await mcp_without_elicitation.call_tool(
                "record_visit_reason", {"appointment_id": 1, "reason": "dolor"}
            )
        assert "CLIENT_CANNOT_CONFIRM" in exc.value.text_of

    async def test_el_rechazo_nombra_el_protocolo_negociado(
        self, mcp_without_elicitation: MCPTestClient, scenario: Scenario
    ) -> None:
        """So the reader can tell an old client from a misconfigured one."""
        args = {"patient_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, ESCRITURA), pytest.raises(ToolCallError) as exc:
            await mcp_without_elicitation.call_tool("book_appointment", args)
        assert "negotiated_protocol" in exc.value.text_of


@pytest.mark.anyio
class TestLaPreguntaHumanaNoFiltraValoresInternos:
    """The question a receptionist approves is Spanish. State values, error
    codes and tool names are English. Neither belongs inside the other, so no
    internal value may appear in the text a person reads."""

    async def test_ningun_estado_interno_aparece_en_la_pregunta(
        self, mcp: MCPTestClient, scenario: Scenario, existing_appointment: int
    ) -> None:
        propuestas = (
            # A slot the `existing_appointment` fixture has not taken.
            (
                "book_appointment",
                {"patient_id": scenario.bruno_id, "slot_id": scenario.slots_general[1]},
            ),
            (
                "cancel_appointment",
                {"appointment_id": existing_appointment, "reason": "el paciente viaja"},
            ),
            ("confirm_appointment", {"appointment_id": existing_appointment}),
            # `no_show` is reachable straight from `scheduled`; `waiting` is not.
            ("record_attendance", {"appointment_id": existing_appointment, "status": "no_show"}),
        )
        internos = {s.value for s in AppointmentState} | {e.value for e in Specialty}
        for tool_name, args in propuestas:
            with as_caller(SUBJECT, ESCRITURA):
                question = await mcp.ask(tool_name, args)
            texto = mcp.question_text(question)
            filtrados = {v for v in internos if v in texto}
            assert not filtrados, f"{tool_name} le mostró {filtrados} a una persona"

    async def test_el_estado_que_si_importa_se_muestra_en_espanol(
        self, mcp: MCPTestClient, existing_appointment: int
    ) -> None:
        """Attendance is the one place the state carries information for the
        front desk, so it is rendered through the label map."""
        with as_caller(SUBJECT, ESCRITURA):
            question = await mcp.ask(
                "record_attendance", {"appointment_id": existing_appointment, "status": "no_show"}
            )
        assert "no asistió" in mcp.question_text(question)
