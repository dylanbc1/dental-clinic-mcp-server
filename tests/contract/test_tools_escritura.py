"""Write tools over MRTR, end to end on the wire.

The property under test throughout: **the first call changes nothing**. It comes
back asking a person, and only the retry carrying their answer mutates anything.
Every test checks the database afterwards rather than trusting the tool's word.
"""

from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.domain.servicios import agendar_cita, inscribir_en_lista_espera
from backend.enums import Especialidad, EstadoCita, EstadoSlot
from backend.models import AgendaSlot, Cita
from tests.conftest import SUJETO, ClienteMCP, ErrorDeHerramienta, Escenario, como

pytestmark = pytest.mark.integration

ESCRITURA = ["read", "write"]


def contar_citas(sesion: Session) -> int:
    return sesion.scalar(select(func.count()).select_from(Cita)) or 0


@pytest.fixture
def cita_existente(sesion_backend: Session, escenario: Escenario) -> int:
    resultado = agendar_cita(
        sesion_backend,
        paciente_id=escenario.ana_id,
        slot_id=escenario.slots_general[0],
        usuario="setup",
    )
    sesion_backend.commit()
    return resultado.cita.id


class TestLaPrimeraLlamadaNoEjecuta:
    async def test_agendar_pide_confirmacion_y_no_crea_nada(
        self, mcp: ClienteMCP, sesion_backend: Session, escenario: Escenario
    ) -> None:
        args = {"paciente_id": escenario.ana_id, "slot_id": escenario.slots_general[0]}
        with como(SUJETO, ESCRITURA):
            pregunta = await mcp.preguntar("agendar_cita", args)

        assert pregunta["resultType"] == "input_required"
        assert pregunta["requestState"]
        assert contar_citas(sesion_backend) == 0
        sesion_backend.expire_all()
        assert sesion_backend.get(AgendaSlot, escenario.slots_general[0]).estado is EstadoSlot.LIBRE

    async def test_la_pregunta_describe_lo_que_va_a_pasar(
        self, mcp: ClienteMCP, escenario: Escenario
    ) -> None:
        """Read aloud to a receptionist, so it names the hour and the
        professional rather than a slot id."""
        args = {"paciente_id": escenario.ana_id, "slot_id": escenario.slots_general[0]}
        with como(SUJETO, ESCRITURA):
            mensaje = mcp.mensaje_de(await mcp.preguntar("agendar_cita", args))

        assert str(escenario.fecha_futura) in mensaje
        assert "Dra. General" in mensaje
        assert "Esto va a pasar:" in mensaje
        assert "¿Confirmas la operación?" in mensaje

    async def test_la_pregunta_pide_un_booleano_y_nada_mas(
        self, mcp: ClienteMCP, escenario: Escenario
    ) -> None:
        """A form with more fields invites changing the operation instead of
        judging it."""
        args = {"paciente_id": escenario.ana_id, "slot_id": escenario.slots_general[0]}
        with como(SUJETO, ESCRITURA):
            pregunta = await mcp.preguntar("agendar_cita", args)
        clave = next(iter(pregunta["inputRequests"]))
        esquema = pregunta["inputRequests"][clave]["params"]["requestedSchema"]
        assert set(esquema["properties"]) == {"confirmado"}
        assert esquema["properties"]["confirmado"]["type"] == "boolean"

    async def test_advierte_de_la_afiliacion_inactiva(
        self, mcp: ClienteMCP, escenario: Escenario
    ) -> None:
        args = {"paciente_id": escenario.bruno_id, "slot_id": escenario.slots_general[0]}
        with como(SUJETO, ESCRITURA):
            mensaje = mcp.mensaje_de(await mcp.preguntar("agendar_cita", args))
        assert "inactiva" in mensaje
        assert "tarifa particular" in mensaje

    async def test_advierte_de_la_mora_sin_bloquear(
        self, mcp: ClienteMCP, escenario: Escenario
    ) -> None:
        args = {"paciente_id": escenario.deudor_id, "slot_id": escenario.slots_general[0]}
        with como(SUJETO, ESCRITURA):
            pregunta = await mcp.preguntar("agendar_cita", args)
        mensaje = mcp.mensaje_de(pregunta)
        assert "mora" in mensaje
        assert "No impide agendar" in mensaje
        assert pregunta["requestState"], "la operación sigue disponible para aprobar"

    async def test_cancelar_pregunta_sin_cancelar(
        self, mcp: ClienteMCP, sesion_backend: Session, cita_existente: int
    ) -> None:
        args = {"cita_id": cita_existente, "motivo": "El paciente viajó"}
        with como(SUJETO, ESCRITURA):
            await mcp.preguntar("cancelar_cita", args)
        sesion_backend.expire_all()
        assert sesion_backend.get(Cita, cita_existente).estado is EstadoCita.AGENDADA


class TestLaSegundaLlamadaEjecuta:
    async def test_el_ciclo_completo_agenda_de_verdad(
        self, mcp: ClienteMCP, sesion_backend: Session, escenario: Escenario
    ) -> None:
        args = {"paciente_id": escenario.ana_id, "slot_id": escenario.slots_general[0]}
        with como(SUJETO, ESCRITURA):
            resultado = await mcp.aprobar("agendar_cita", args)

        assert resultado["cita"]["estado"] == "agendada"
        assert contar_citas(sesion_backend) == 1

    async def test_el_actor_del_token_queda_en_la_auditoria_del_backend(
        self, mcp: ClienteMCP, escenario: Escenario
    ) -> None:
        """The audit row must name the human's subject, not "mcp-server"."""
        args = {"paciente_id": escenario.ana_id, "slot_id": escenario.slots_general[0]}
        with como("dra.ospina@clinica.test", ESCRITURA):
            resultado = await mcp.aprobar("agendar_cita", args)
        assert resultado["cita"]["historial"][0]["usuario"] == "dra.ospina@clinica.test"

    async def test_cancelar_libera_el_cupo(
        self,
        mcp: ClienteMCP,
        sesion_backend: Session,
        cita_existente: int,
        escenario: Escenario,
    ) -> None:
        args = {"cita_id": cita_existente, "motivo": "El paciente viajó"}
        with como(SUJETO, ESCRITURA):
            resultado = await mcp.aprobar("cancelar_cita", args)
        assert resultado["libero_cupo"] is True
        sesion_backend.expire_all()
        assert sesion_backend.get(AgendaSlot, escenario.slots_general[0]).estado is EstadoSlot.LIBRE

    async def test_confirmar(
        self, mcp: ClienteMCP, sesion_backend: Session, cita_existente: int
    ) -> None:
        with como(SUJETO, ESCRITURA):
            resultado = await mcp.aprobar("confirmar_cita", {"cita_id": cita_existente})
        assert resultado["estado_nuevo"] == "confirmada"
        sesion_backend.expire_all()
        assert sesion_backend.get(Cita, cita_existente).estado is EstadoCita.CONFIRMADA

    async def test_registrar_asistencia_genera_el_cargo(
        self, mcp: ClienteMCP, sesion_backend: Session, cita_existente: int
    ) -> None:
        from backend.domain.servicios import confirmar_cita, registrar_asistencia

        confirmar_cita(sesion_backend, cita_existente, usuario="setup")
        registrar_asistencia(sesion_backend, cita_existente, EstadoCita.EN_ESPERA, usuario="setup")
        sesion_backend.commit()

        with como(SUJETO, ESCRITURA):
            resultado = await mcp.aprobar(
                "registrar_asistencia", {"cita_id": cita_existente, "estado": "atendida"}
            )
        assert resultado["genero_cargo"] is True
        assert resultado["cargo"]["concepto"] == "cuota_moderadora"

    async def test_reprogramar_encadena_la_cita_nueva(
        self,
        mcp: ClienteMCP,
        sesion_backend: Session,
        cita_existente: int,
        escenario: Escenario,
    ) -> None:
        args = {"cita_id": cita_existente, "nuevo_slot_id": escenario.slots_general[2]}
        with como(SUJETO, ESCRITURA):
            resultado = await mcp.aprobar("reprogramar_cita", args)
        assert resultado["cita"]["cita_origen_id"] == cita_existente
        sesion_backend.expire_all()
        assert sesion_backend.get(AgendaSlot, escenario.slots_general[0]).estado is EstadoSlot.LIBRE

    async def test_ofrecer_cupo_contacta_sin_agendar(
        self, mcp: ClienteMCP, sesion_backend: Session, escenario: Escenario
    ) -> None:
        inscribir_en_lista_espera(
            sesion_backend,
            paciente_id=escenario.carla_id,
            especialidad=Especialidad.ORTODONCIA,
        )
        sesion_backend.commit()

        with como(SUJETO, ESCRITURA):
            pregunta = await mcp.preguntar(
                "ofrecer_cupo_lista_espera", {"slot_id": escenario.slots_orto[0]}
            )
            assert "NO se agenda" in mcp.mensaje_de(pregunta)
            oferta = await mcp.responder(
                "ofrecer_cupo_lista_espera",
                {"slot_id": escenario.slots_orto[0]},
                pregunta,
            )

        assert oferta["paciente_id"] == escenario.carla_id
        assert oferta["telefono"]
        assert contar_citas(sesion_backend) == 0


class TestCuandoLaPersonaDiceQueNo:
    async def test_un_false_explicito_aborta_sin_tocar_nada(
        self, mcp: ClienteMCP, sesion_backend: Session, escenario: Escenario
    ) -> None:
        args = {"paciente_id": escenario.ana_id, "slot_id": escenario.slots_general[0]}
        with como(SUJETO, ESCRITURA):
            pregunta = await mcp.preguntar("agendar_cita", args)
            with pytest.raises(ErrorDeHerramienta) as exc:
                await mcp.responder("agendar_cita", args, pregunta, confirmado=False)

        assert "OPERACION_NO_APROBADA" in exc.value.texto
        assert "Nothing was changed" in exc.value.texto
        assert contar_citas(sesion_backend) == 0

    async def test_el_rechazo_pide_no_reintentar(
        self, mcp: ClienteMCP, escenario: Escenario
    ) -> None:
        """Retrying an operation a person declined is how an agent nags."""
        args = {"paciente_id": escenario.ana_id, "slot_id": escenario.slots_general[0]}
        with como(SUJETO, ESCRITURA):
            pregunta = await mcp.preguntar("agendar_cita", args)
            with pytest.raises(ErrorDeHerramienta) as exc:
                await mcp.responder("agendar_cita", args, pregunta, confirmado=False)
        assert "Do not retry" in exc.value.texto

    async def test_declinar_la_elicitacion_tambien_aborta(
        self, mcp: ClienteMCP, sesion_backend: Session, escenario: Escenario
    ) -> None:
        """The client can decline instead of answering. The call must stop."""
        args = {"paciente_id": escenario.ana_id, "slot_id": escenario.slots_general[0]}
        with como(SUJETO, ESCRITURA):
            pregunta = await mcp.preguntar("agendar_cita", args)
            with pytest.raises(ErrorDeHerramienta):
                await mcp.responder("agendar_cita", args, pregunta, accion="decline")
        assert contar_citas(sesion_backend) == 0


class TestValidaAntesDePreguntar:
    """A question a human cannot act on is worse than an error.

    Every check here is repeated on the second round, because the resolver runs
    again. These exist so nobody is asked to approve something that will fail.
    """

    async def test_no_pregunta_por_un_cupo_ya_ocupado(
        self, mcp: ClienteMCP, escenario: Escenario, cita_existente: int
    ) -> None:
        with como(SUJETO, ESCRITURA), pytest.raises(ErrorDeHerramienta) as exc:
            await mcp.preguntar(
                "agendar_cita",
                {"paciente_id": escenario.carla_id, "slot_id": escenario.slots_general[0]},
            )
        assert "SLOT_NO_DISPONIBLE" in exc.value.texto
        assert "closest free slots" in exc.value.texto

    async def test_no_pregunta_por_un_cupo_en_el_pasado(
        self, mcp: ClienteMCP, escenario: Escenario
    ) -> None:
        with como(SUJETO, ESCRITURA), pytest.raises(ErrorDeHerramienta) as exc:
            await mcp.preguntar(
                "agendar_cita",
                {"paciente_id": escenario.ana_id, "slot_id": escenario.slot_pasado_id},
            )
        assert "SLOT_EN_EL_PASADO" in exc.value.texto

    async def test_no_pregunta_por_un_cruce_de_horario(
        self, mcp: ClienteMCP, sesion_backend: Session, escenario: Escenario
    ) -> None:
        agendar_cita(
            sesion_backend,
            paciente_id=escenario.ana_id,
            slot_id=escenario.slots_general[0],
            usuario="setup",
        )
        sesion_backend.commit()
        with como(SUJETO, ESCRITURA), pytest.raises(ErrorDeHerramienta) as exc:
            await mcp.preguntar(
                "agendar_cita",
                {"paciente_id": escenario.ana_id, "slot_id": escenario.slots_orto[0]},
            )
        assert "PACIENTE_YA_TIENE_CITA" in exc.value.texto

    async def test_no_pregunta_por_una_transicion_imposible(
        self, mcp: ClienteMCP, cita_existente: int
    ) -> None:
        with como(SUJETO, ESCRITURA), pytest.raises(ErrorDeHerramienta) as exc:
            await mcp.preguntar(
                "registrar_asistencia", {"cita_id": cita_existente, "estado": "atendida"}
            )
        assert "TRANSICION_INVALIDA" in exc.value.texto
        assert "confirmada" in exc.value.texto

    async def test_un_estado_de_asistencia_inventado_se_rechaza(
        self, mcp: ClienteMCP, cita_existente: int
    ) -> None:
        with como(SUJETO, ESCRITURA), pytest.raises(ErrorDeHerramienta) as exc:
            await mcp.preguntar(
                "registrar_asistencia", {"cita_id": cita_existente, "estado": "cancelada"}
            )
        assert "is not an attendance state" in exc.value.texto

    async def test_una_cita_inexistente_falla_antes_de_preguntar(
        self, mcp: ClienteMCP, escenario: Escenario
    ) -> None:
        with como(SUJETO, ESCRITURA), pytest.raises(ErrorDeHerramienta) as exc:
            await mcp.preguntar("confirmar_cita", {"cita_id": 424242})
        assert "CITA_NO_ENCONTRADA" in exc.value.texto


class TestLaValidacionSeRepiteAlEjecutar:
    async def test_la_aprobacion_no_legaliza_lo_que_dejo_de_ser_legal(
        self, mcp: ClienteMCP, sesion_backend: Session, cita_existente: int
    ) -> None:
        """The state can change between the two rounds.

        The resolver runs again on the retry, so the domain refuses even though
        the human approved. Approval authorises an action; it does not freeze the
        world it saw.
        """
        from backend.domain.servicios import cancelar_cita

        with como(SUJETO, ESCRITURA):
            pregunta = await mcp.preguntar("confirmar_cita", {"cita_id": cita_existente})

            cancelar_cita(sesion_backend, cita_existente, motivo="urgencia", usuario="otro")
            sesion_backend.commit()

            with pytest.raises(ErrorDeHerramienta) as exc:
                await mcp.responder("confirmar_cita", {"cita_id": cita_existente}, pregunta)
        assert "TRANSICION_INVALIDA" in exc.value.texto or "estado final" in exc.value.texto

    async def test_el_cupo_tomado_en_medio_se_detecta(
        self, mcp: ClienteMCP, sesion_backend: Session, escenario: Escenario
    ) -> None:
        args = {"paciente_id": escenario.ana_id, "slot_id": escenario.slots_general[0]}
        with como(SUJETO, ESCRITURA):
            pregunta = await mcp.preguntar("agendar_cita", args)

            agendar_cita(
                sesion_backend,
                paciente_id=escenario.carla_id,
                slot_id=escenario.slots_general[0],
                usuario="otro",
            )
            sesion_backend.commit()

            with pytest.raises(ErrorDeHerramienta) as exc:
                await mcp.responder("agendar_cita", args, pregunta)
        assert "SLOT_NO_DISPONIBLE" in exc.value.texto


class TestAuditoria:
    async def test_la_pregunta_y_la_ejecucion_se_registran(
        self, mcp: ClienteMCP, ctx: Any, escenario: Escenario
    ) -> None:
        args = {"paciente_id": escenario.ana_id, "slot_id": escenario.slots_general[0]}
        with como(SUJETO, ESCRITURA):
            await mcp.aprobar("agendar_cita", args)

        resultados = [
            e["resultado"] for e in ctx.auditor.eventos if e["evento"] == "tool.invocacion"
        ]
        # MRTR means two calls arrive per mutation, and the log records calls.
        assert resultados.count("input_required") == 2
        assert resultados[-1] == "ok"

    async def test_la_ejecucion_queda_marcada_como_aprobada(
        self, mcp: ClienteMCP, ctx: Any, escenario: Escenario
    ) -> None:
        args = {"paciente_id": escenario.ana_id, "slot_id": escenario.slots_general[0]}
        with como(SUJETO, ESCRITURA):
            await mcp.aprobar("agendar_cita", args)
        assert ctx.auditor.eventos[-1]["con_aprobacion_humana"] is True

    async def test_un_rechazo_por_validacion_queda_en_el_log(
        self, mcp: ClienteMCP, ctx: Any, cita_existente: int
    ) -> None:
        """A log that records only what succeeded cannot tell you an agent spent
        an hour asking for something impossible."""
        with como(SUJETO, ESCRITURA), pytest.raises(ErrorDeHerramienta):
            await mcp.preguntar(
                "registrar_asistencia", {"cita_id": cita_existente, "estado": "atendida"}
            )
        evento = ctx.auditor.eventos[-1]
        assert evento["resultado"] == "error"
        assert evento["codigo_error"] == "TRANSICION_INVALIDA"

    async def test_el_motivo_no_se_copia_al_log(
        self, mcp: ClienteMCP, ctx: Any, cita_existente: int
    ) -> None:
        secreto = "sangrado persistente desde el martes"
        with como(SUJETO, ESCRITURA):
            await mcp.preguntar("cancelar_cita", {"cita_id": cita_existente, "motivo": secreto})
        assert secreto not in str(ctx.auditor.eventos)
        assert ctx.auditor.eventos[-1]["argumentos"]["motivo"] == "«redactado»"

    async def test_el_request_state_no_se_copia_al_log(
        self, mcp: ClienteMCP, ctx: Any, escenario: Escenario
    ) -> None:
        """A logged request state is a redeemable approval sitting in a log."""
        args = {"paciente_id": escenario.ana_id, "slot_id": escenario.slots_general[0]}
        with como(SUJETO, ESCRITURA):
            pregunta = await mcp.preguntar("agendar_cita", args)
        assert pregunta["requestState"] not in str(ctx.auditor.eventos)


class TestUnClienteQueNoPuedeConfirmar:
    """Not every client speaks 2026-07-28 yet, and one that does not deserves to
    be told which half of this server it can still use."""

    async def test_las_escrituras_se_rechazan_con_un_mensaje_util(
        self, mcp_sin_elicitacion: ClienteMCP, escenario: Escenario
    ) -> None:
        args = {"paciente_id": escenario.ana_id, "slot_id": escenario.slots_general[0]}
        with como(SUJETO, ESCRITURA), pytest.raises(ErrorDeHerramienta) as exc:
            await mcp_sin_elicitacion.llamar("agendar_cita", args)

        mensaje = exc.value.texto
        assert "CLIENTE_SIN_CONFIRMACION" in mensaje
        # Not a transport error the reader cannot act on.
        assert "back-channel" not in mensaje
        assert "elicitation" in mensaje
        assert "Read tools work" in mensaje

    async def test_las_lecturas_siguen_funcionando(
        self, mcp_sin_elicitacion: ClienteMCP, escenario: Escenario
    ) -> None:
        with como(SUJETO, ESCRITURA):
            pacientes = await mcp_sin_elicitacion.llamar(
                "buscar_paciente", {"documento": escenario.ana_documento}
            )
        assert [p["id"] for p in pacientes] == [escenario.ana_id]

    async def test_la_clinica_tambien_se_rechaza(
        self, mcp_sin_elicitacion: ClienteMCP, escenario: Escenario
    ) -> None:
        with como(SUJETO, [*ESCRITURA, "clinical"]), pytest.raises(ErrorDeHerramienta) as exc:
            await mcp_sin_elicitacion.llamar(
                "registrar_motivo_consulta", {"cita_id": 1, "motivo": "dolor"}
            )
        assert "CLIENTE_SIN_CONFIRMACION" in exc.value.texto

    async def test_el_rechazo_nombra_el_protocolo_negociado(
        self, mcp_sin_elicitacion: ClienteMCP, escenario: Escenario
    ) -> None:
        """So the reader can tell an old client from a misconfigured one."""
        args = {"paciente_id": escenario.ana_id, "slot_id": escenario.slots_general[0]}
        with como(SUJETO, ESCRITURA), pytest.raises(ErrorDeHerramienta) as exc:
            await mcp_sin_elicitacion.llamar("agendar_cita", args)
        assert "protocolo_negociado" in exc.value.texto
