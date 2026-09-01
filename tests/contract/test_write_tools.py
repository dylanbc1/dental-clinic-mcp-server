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
from backend.enums import Especialidad, EstadoCita, EstadoSlot
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
        paciente_id=scenario.ana_id,
        slot_id=scenario.slots_general[0],
        usuario="setup",
    )
    backend_session.commit()
    return result.cita.id


class TestLaPrimeraLlamadaNoEjecuta:
    async def test_agendar_pide_confirmacion_y_no_crea_nada(
        self, mcp: MCPTestClient, backend_session: Session, scenario: Scenario
    ) -> None:
        args = {"paciente_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, ESCRITURA):
            question = await mcp.ask("agendar_cita", args)

        assert question["resultType"] == "input_required"
        assert question["requestState"]
        assert contar_citas(backend_session) == 0
        backend_session.expire_all()
        assert backend_session.get(AgendaSlot, scenario.slots_general[0]).estado is EstadoSlot.LIBRE

    async def test_la_pregunta_describe_lo_que_va_a_pasar(
        self, mcp: MCPTestClient, scenario: Scenario
    ) -> None:
        """Read aloud to a receptionist, so it names the hour and the
        professional rather than a slot id."""
        args = {"paciente_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, ESCRITURA):
            mensaje = mcp.mensaje_de(await mcp.ask("agendar_cita", args))

        assert str(scenario.fecha_futura) in mensaje
        assert "Dra. General" in mensaje
        assert "Esto va a pasar:" in mensaje
        assert "¿Confirmas la operación?" in mensaje

    async def test_la_pregunta_pide_un_booleano_y_nada_mas(
        self, mcp: MCPTestClient, scenario: Scenario
    ) -> None:
        """A form with more fields invites changing the operation instead of
        judging it."""
        args = {"paciente_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, ESCRITURA):
            question = await mcp.ask("agendar_cita", args)
        key = next(iter(question["inputRequests"]))
        esquema = question["inputRequests"][key]["params"]["requestedSchema"]
        assert set(esquema["properties"]) == {"confirmado"}
        assert esquema["properties"]["confirmado"]["type"] == "boolean"

    async def test_advierte_de_la_afiliacion_inactiva(
        self, mcp: MCPTestClient, scenario: Scenario
    ) -> None:
        args = {"paciente_id": scenario.bruno_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, ESCRITURA):
            mensaje = mcp.mensaje_de(await mcp.ask("agendar_cita", args))
        assert "inactiva" in mensaje
        assert "tarifa particular" in mensaje

    async def test_advierte_de_la_mora_sin_bloquear(
        self, mcp: MCPTestClient, scenario: Scenario
    ) -> None:
        args = {"paciente_id": scenario.deudor_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, ESCRITURA):
            question = await mcp.ask("agendar_cita", args)
        mensaje = mcp.mensaje_de(question)
        assert "mora" in mensaje
        assert "No impide agendar" in mensaje
        assert question["requestState"], "la operación sigue disponible para aprobar"

    async def test_cancelar_pregunta_sin_cancelar(
        self, mcp: MCPTestClient, backend_session: Session, existing_appointment: int
    ) -> None:
        args = {"cita_id": existing_appointment, "motivo": "El paciente viajó"}
        with as_caller(SUBJECT, ESCRITURA):
            await mcp.ask("cancelar_cita", args)
        backend_session.expire_all()
        assert backend_session.get(Appointment, existing_appointment).estado is EstadoCita.AGENDADA


class TestLaSegundaLlamadaEjecuta:
    async def test_el_ciclo_completo_agenda_de_verdad(
        self, mcp: MCPTestClient, backend_session: Session, scenario: Scenario
    ) -> None:
        args = {"paciente_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, ESCRITURA):
            result = await mcp.aprobar("agendar_cita", args)

        assert result["cita"]["estado"] == "agendada"
        assert contar_citas(backend_session) == 1

    async def test_el_actor_del_token_queda_en_la_auditoria_del_backend(
        self, mcp: MCPTestClient, scenario: Scenario
    ) -> None:
        """The audit row must name the human's subject, not "mcp-server"."""
        args = {"paciente_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller("dra.ospina@clinica.test", ESCRITURA):
            result = await mcp.aprobar("agendar_cita", args)
        assert result["cita"]["historial"][0]["usuario"] == "dra.ospina@clinica.test"

    async def test_cancelar_libera_el_cupo(
        self,
        mcp: MCPTestClient,
        backend_session: Session,
        existing_appointment: int,
        scenario: Scenario,
    ) -> None:
        args = {"cita_id": existing_appointment, "motivo": "El paciente viajó"}
        with as_caller(SUBJECT, ESCRITURA):
            result = await mcp.aprobar("cancelar_cita", args)
        assert result["libero_cupo"] is True
        backend_session.expire_all()
        assert backend_session.get(AgendaSlot, scenario.slots_general[0]).estado is EstadoSlot.LIBRE

    async def test_confirmar(
        self, mcp: MCPTestClient, backend_session: Session, existing_appointment: int
    ) -> None:
        with as_caller(SUBJECT, ESCRITURA):
            result = await mcp.aprobar("confirmar_cita", {"cita_id": existing_appointment})
        assert result["estado_nuevo"] == "confirmada"
        backend_session.expire_all()
        assert (
            backend_session.get(Appointment, existing_appointment).estado is EstadoCita.CONFIRMADA
        )

    async def test_registrar_asistencia_genera_el_cargo(
        self, mcp: MCPTestClient, backend_session: Session, existing_appointment: int
    ) -> None:
        from backend.domain.services import confirm_appointment, record_attendance

        confirm_appointment(backend_session, existing_appointment, usuario="setup")
        record_attendance(
            backend_session, existing_appointment, EstadoCita.EN_ESPERA, usuario="setup"
        )
        backend_session.commit()

        with as_caller(SUBJECT, ESCRITURA):
            result = await mcp.aprobar(
                "registrar_asistencia", {"cita_id": existing_appointment, "estado": "atendida"}
            )
        assert result["genero_cargo"] is True
        assert result["cargo"]["concepto"] == "cuota_moderadora"

    async def test_reprogramar_encadena_la_cita_nueva(
        self,
        mcp: MCPTestClient,
        backend_session: Session,
        existing_appointment: int,
        scenario: Scenario,
    ) -> None:
        args = {"cita_id": existing_appointment, "nuevo_slot_id": scenario.slots_general[2]}
        with as_caller(SUBJECT, ESCRITURA):
            result = await mcp.aprobar("reprogramar_cita", args)
        assert result["cita"]["cita_origen_id"] == existing_appointment
        backend_session.expire_all()
        assert backend_session.get(AgendaSlot, scenario.slots_general[0]).estado is EstadoSlot.LIBRE

    async def test_ofrecer_cupo_contacta_sin_agendar(
        self, mcp: MCPTestClient, backend_session: Session, scenario: Scenario
    ) -> None:
        join_waiting_list(
            backend_session,
            paciente_id=scenario.carla_id,
            especialidad=Especialidad.ORTODONCIA,
        )
        backend_session.commit()

        with as_caller(SUBJECT, ESCRITURA):
            question = await mcp.ask(
                "ofrecer_cupo_lista_espera", {"slot_id": scenario.slots_orto[0]}
            )
            assert "NO se agenda" in mcp.mensaje_de(question)
            oferta = await mcp.respond(
                "ofrecer_cupo_lista_espera",
                {"slot_id": scenario.slots_orto[0]},
                question,
            )

        assert oferta["paciente_id"] == scenario.carla_id
        assert oferta["telefono"]
        assert contar_citas(backend_session) == 0


class TestCuandoLaPersonaDiceQueNo:
    async def test_un_false_explicito_aborta_sin_tocar_nada(
        self, mcp: MCPTestClient, backend_session: Session, scenario: Scenario
    ) -> None:
        args = {"paciente_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, ESCRITURA):
            question = await mcp.ask("agendar_cita", args)
            with pytest.raises(ToolCallError) as exc:
                await mcp.respond("agendar_cita", args, question, confirmado=False)

        assert "OPERACION_NO_APROBADA" in exc.value.text_of
        assert "Nothing was changed" in exc.value.text_of
        assert contar_citas(backend_session) == 0

    async def test_el_rechazo_pide_no_reintentar(
        self, mcp: MCPTestClient, scenario: Scenario
    ) -> None:
        """Retrying an operation a person declined is how an agent nags."""
        args = {"paciente_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, ESCRITURA):
            question = await mcp.ask("agendar_cita", args)
            with pytest.raises(ToolCallError) as exc:
                await mcp.respond("agendar_cita", args, question, confirmado=False)
        assert "Do not retry" in exc.value.text_of

    async def test_declinar_la_elicitacion_tambien_aborta(
        self, mcp: MCPTestClient, backend_session: Session, scenario: Scenario
    ) -> None:
        """The client can decline instead of answering. The call must stop."""
        args = {"paciente_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, ESCRITURA):
            question = await mcp.ask("agendar_cita", args)
            with pytest.raises(ToolCallError):
                await mcp.respond("agendar_cita", args, question, action="decline")
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
                "agendar_cita",
                {"paciente_id": scenario.carla_id, "slot_id": scenario.slots_general[0]},
            )
        assert "SLOT_NO_DISPONIBLE" in exc.value.text_of
        assert "closest free slots" in exc.value.text_of

    async def test_no_pregunta_por_un_cupo_en_el_pasado(
        self, mcp: MCPTestClient, scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ESCRITURA), pytest.raises(ToolCallError) as exc:
            await mcp.ask(
                "agendar_cita",
                {"paciente_id": scenario.ana_id, "slot_id": scenario.slot_pasado_id},
            )
        assert "SLOT_EN_EL_PASADO" in exc.value.text_of

    async def test_no_pregunta_por_un_cruce_de_horario(
        self, mcp: MCPTestClient, backend_session: Session, scenario: Scenario
    ) -> None:
        book_appointment(
            backend_session,
            paciente_id=scenario.ana_id,
            slot_id=scenario.slots_general[0],
            usuario="setup",
        )
        backend_session.commit()
        with as_caller(SUBJECT, ESCRITURA), pytest.raises(ToolCallError) as exc:
            await mcp.ask(
                "agendar_cita",
                {"paciente_id": scenario.ana_id, "slot_id": scenario.slots_orto[0]},
            )
        assert "PACIENTE_YA_TIENE_CITA" in exc.value.text_of

    async def test_no_pregunta_por_una_transicion_imposible(
        self, mcp: MCPTestClient, existing_appointment: int
    ) -> None:
        with as_caller(SUBJECT, ESCRITURA), pytest.raises(ToolCallError) as exc:
            await mcp.ask(
                "registrar_asistencia", {"cita_id": existing_appointment, "estado": "atendida"}
            )
        assert "TRANSICION_INVALIDA" in exc.value.text_of
        assert "confirmada" in exc.value.text_of

    async def test_un_estado_de_asistencia_inventado_se_rechaza(
        self, mcp: MCPTestClient, existing_appointment: int
    ) -> None:
        with as_caller(SUBJECT, ESCRITURA), pytest.raises(ToolCallError) as exc:
            await mcp.ask(
                "registrar_asistencia", {"cita_id": existing_appointment, "estado": "cancelada"}
            )
        assert "is not an attendance state" in exc.value.text_of

    async def test_una_cita_inexistente_falla_antes_de_preguntar(
        self, mcp: MCPTestClient, scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ESCRITURA), pytest.raises(ToolCallError) as exc:
            await mcp.ask("confirmar_cita", {"cita_id": 424242})
        assert "CITA_NO_ENCONTRADA" in exc.value.text_of


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
            question = await mcp.ask("confirmar_cita", {"cita_id": existing_appointment})

            cancel_appointment(
                backend_session, existing_appointment, motivo="urgencia", usuario="otro"
            )
            backend_session.commit()

            with pytest.raises(ToolCallError) as exc:
                await mcp.respond("confirmar_cita", {"cita_id": existing_appointment}, question)
        assert "TRANSICION_INVALIDA" in exc.value.text_of or "estado final" in exc.value.text_of

    async def test_el_cupo_tomado_en_medio_se_detecta(
        self, mcp: MCPTestClient, backend_session: Session, scenario: Scenario
    ) -> None:
        args = {"paciente_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, ESCRITURA):
            question = await mcp.ask("agendar_cita", args)

            book_appointment(
                backend_session,
                paciente_id=scenario.carla_id,
                slot_id=scenario.slots_general[0],
                usuario="otro",
            )
            backend_session.commit()

            with pytest.raises(ToolCallError) as exc:
                await mcp.respond("agendar_cita", args, question)
        assert "SLOT_NO_DISPONIBLE" in exc.value.text_of


class TestAuditoria:
    async def test_la_pregunta_y_la_ejecucion_se_registran(
        self, mcp: MCPTestClient, ctx: Any, scenario: Scenario
    ) -> None:
        args = {"paciente_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, ESCRITURA):
            await mcp.aprobar("agendar_cita", args)

        resultados = [e["result"] for e in ctx.auditor.events if e["event"] == "tool.invocation"]
        # MRTR means two calls arrive per mutation, and the log records calls.
        assert resultados.count("input_required") == 2
        assert resultados[-1] == "ok"

    async def test_la_ejecucion_queda_marcada_como_aprobada(
        self, mcp: MCPTestClient, ctx: Any, scenario: Scenario
    ) -> None:
        args = {"paciente_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, ESCRITURA):
            await mcp.aprobar("agendar_cita", args)
        assert ctx.auditor.events[-1]["with_human_approval"] is True

    async def test_un_rechazo_por_validacion_queda_en_el_log(
        self, mcp: MCPTestClient, ctx: Any, existing_appointment: int
    ) -> None:
        """A log that records only what succeeded cannot tell you an agent spent
        an hour asking for something impossible."""
        with as_caller(SUBJECT, ESCRITURA), pytest.raises(ToolCallError):
            await mcp.ask(
                "registrar_asistencia", {"cita_id": existing_appointment, "estado": "atendida"}
            )
        evento = ctx.auditor.events[-1]
        assert evento["result"] == "error"
        assert evento["error_code"] == "TRANSICION_INVALIDA"

    async def test_el_motivo_no_se_copia_al_log(
        self, mcp: MCPTestClient, ctx: Any, existing_appointment: int
    ) -> None:
        secreto = "sangrado persistente desde el martes"
        with as_caller(SUBJECT, ESCRITURA):
            await mcp.ask("cancelar_cita", {"cita_id": existing_appointment, "motivo": secreto})
        assert secreto not in str(ctx.auditor.events)
        assert ctx.auditor.events[-1]["arguments"]["motivo"] == "«redacted»"

    async def test_el_request_state_no_se_copia_al_log(
        self, mcp: MCPTestClient, ctx: Any, scenario: Scenario
    ) -> None:
        """A logged request state is a redeemable approval sitting in a log."""
        args = {"paciente_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, ESCRITURA):
            question = await mcp.ask("agendar_cita", args)
        assert question["requestState"] not in str(ctx.auditor.events)


class TestUnClienteQueNoPuedeConfirmar:
    """Not every client speaks 2026-07-28 yet, and one that does not deserves to
    be told which half of this server it can still use."""

    async def test_las_escrituras_se_rechazan_con_un_mensaje_util(
        self, mcp_without_elicitation: MCPTestClient, scenario: Scenario
    ) -> None:
        args = {"paciente_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, ESCRITURA), pytest.raises(ToolCallError) as exc:
            await mcp_without_elicitation.call_tool("agendar_cita", args)

        mensaje = exc.value.text_of
        assert "CLIENTE_SIN_CONFIRMACION" in mensaje
        # Not a transport error the reader cannot act on.
        assert "back-channel" not in mensaje
        assert "elicitation" in mensaje
        assert "Read tools work" in mensaje

    async def test_las_lecturas_siguen_funcionando(
        self, mcp_without_elicitation: MCPTestClient, scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ESCRITURA):
            patients = await mcp_without_elicitation.call_tool(
                "buscar_paciente", {"documento": scenario.ana_documento}
            )
        assert [p["id"] for p in patients] == [scenario.ana_id]

    async def test_la_clinica_tambien_se_rechaza(
        self, mcp_without_elicitation: MCPTestClient, scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, [*ESCRITURA, "clinical"]), pytest.raises(ToolCallError) as exc:
            await mcp_without_elicitation.call_tool(
                "registrar_motivo_consulta", {"cita_id": 1, "motivo": "dolor"}
            )
        assert "CLIENTE_SIN_CONFIRMACION" in exc.value.text_of

    async def test_el_rechazo_nombra_el_protocolo_negociado(
        self, mcp_without_elicitation: MCPTestClient, scenario: Scenario
    ) -> None:
        """So the reader can tell an old client from a misconfigured one."""
        args = {"paciente_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}
        with as_caller(SUBJECT, ESCRITURA), pytest.raises(ToolCallError) as exc:
            await mcp_without_elicitation.call_tool("agendar_cita", args)
        assert "protocolo_negociado" in exc.value.text_of
