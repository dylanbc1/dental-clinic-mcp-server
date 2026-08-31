"""Write tools: the two-phase gate, end to end.

The property under test throughout: **calling a write tool changes nothing**.
Only `confirmar_operacion`, with a valid token, does. Every test here checks the
database afterwards rather than trusting the tool's own word for it.
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.domain.servicios import agendar_cita, inscribir_en_lista_espera
from backend.enums import Especialidad, EstadoCita, EstadoSlot
from backend.models import AgendaSlot, Cita
from tests.conftest import SUJETO, Escenario, como, error_de, llamar

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


class TestLaPropuestaNoEjecuta:
    async def test_agendar_devuelve_una_propuesta_y_no_crea_nada(
        self, servidor: MCPServer[Any], sesion_backend: Session, escenario: Escenario
    ) -> None:
        with como(SUJETO, ESCRITURA):
            propuesta = await llamar(
                servidor,
                "agendar_cita",
                {"paciente_id": escenario.ana_id, "slot_id": escenario.slots_general[0]},
            )

        assert propuesta["requiere_confirmacion"] is True
        assert propuesta["token_confirmacion"]
        assert propuesta["esto_va_a_pasar"]
        assert contar_citas(sesion_backend) == 0
        assert sesion_backend.get(AgendaSlot, escenario.slots_general[0]).estado is EstadoSlot.LIBRE

    async def test_la_propuesta_explica_el_siguiente_paso(
        self, servidor: MCPServer[Any], escenario: Escenario
    ) -> None:
        """The model must not tell the patient the appointment is booked."""
        with como(SUJETO, ESCRITURA):
            propuesta = await llamar(
                servidor,
                "agendar_cita",
                {"paciente_id": escenario.ana_id, "slot_id": escenario.slots_general[0]},
            )
        siguiente = propuesta["siguiente_paso"]
        assert "confirmar_operacion" in siguiente
        assert "no se ha modificado" in siguiente

    async def test_la_propuesta_advierte_de_la_afiliacion_inactiva(
        self, servidor: MCPServer[Any], escenario: Escenario
    ) -> None:
        with como(SUJETO, ESCRITURA):
            propuesta = await llamar(
                servidor,
                "agendar_cita",
                {"paciente_id": escenario.bruno_id, "slot_id": escenario.slots_general[0]},
            )
        assert any("inactiva" in a for a in propuesta["advertencias"])

    async def test_la_propuesta_advierte_de_la_mora_sin_bloquear(
        self, servidor: MCPServer[Any], escenario: Escenario
    ) -> None:
        with como(SUJETO, ESCRITURA):
            propuesta = await llamar(
                servidor,
                "agendar_cita",
                {"paciente_id": escenario.deudor_id, "slot_id": escenario.slots_general[0]},
            )
        assert any("mora" in a for a in propuesta["advertencias"])
        assert any("No impide agendar" in a for a in propuesta["advertencias"])
        assert propuesta["token_confirmacion"]

    async def test_cancelar_propone_sin_cancelar(
        self, servidor: MCPServer[Any], sesion_backend: Session, cita_existente: int
    ) -> None:
        with como(SUJETO, ESCRITURA):
            propuesta = await llamar(
                servidor,
                "cancelar_cita",
                {"cita_id": cita_existente, "motivo": "El paciente viajó"},
            )
        assert propuesta["requiere_confirmacion"] is True
        sesion_backend.expire_all()
        assert sesion_backend.get(Cita, cita_existente).estado is EstadoCita.AGENDADA

    async def test_cancelar_sin_motivo_lo_rechaza_el_esquema(
        self, servidor: MCPServer[Any], cita_existente: int
    ) -> None:
        with como(SUJETO, ESCRITURA):
            mensaje = await error_de(servidor, "cancelar_cita", {"cita_id": cita_existente})
        assert "motivo" in mensaje

    async def test_un_estado_de_asistencia_invalido_se_rechaza_antes_de_proponer(
        self, servidor: MCPServer[Any], cita_existente: int
    ) -> None:
        with como(SUJETO, ESCRITURA):
            mensaje = await error_de(
                servidor,
                "registrar_asistencia",
                {"cita_id": cita_existente, "estado": "cancelada"},
            )
        assert "no es un estado de asistencia" in mensaje
        assert "en_espera" in mensaje


class TestConfirmacion:
    async def test_el_ciclo_completo_agenda_de_verdad(
        self, servidor: MCPServer[Any], sesion_backend: Session, escenario: Escenario
    ) -> None:
        with como(SUJETO, ESCRITURA):
            propuesta = await llamar(
                servidor,
                "agendar_cita",
                {"paciente_id": escenario.ana_id, "slot_id": escenario.slots_general[0]},
            )
            confirmado = await llamar(
                servidor,
                "confirmar_operacion",
                {"token_confirmacion": propuesta["token_confirmacion"]},
            )

        assert confirmado["ejecutada"] is True
        assert confirmado["accion"] == "agendar_cita"
        assert confirmado["resultado"]["cita"]["estado"] == "agendada"
        assert contar_citas(sesion_backend) == 1

    async def test_el_actor_del_token_queda_en_la_auditoria_del_backend(
        self, servidor: MCPServer[Any], sesion_backend: Session, escenario: Escenario
    ) -> None:
        """The audit row must name the human's subject, not "mcp-server"."""
        with como("dra.ospina@clinica.test", ESCRITURA):
            propuesta = await llamar(
                servidor,
                "agendar_cita",
                {"paciente_id": escenario.ana_id, "slot_id": escenario.slots_general[0]},
            )
            confirmado = await llamar(
                servidor,
                "confirmar_operacion",
                {"token_confirmacion": propuesta["token_confirmacion"]},
            )
        historial = confirmado["resultado"]["cita"]["historial"]
        assert historial[0]["usuario"] == "dra.ospina@clinica.test"

    async def test_confirmar_una_cancelacion_libera_el_cupo(
        self,
        servidor: MCPServer[Any],
        sesion_backend: Session,
        cita_existente: int,
        escenario: Escenario,
    ) -> None:
        with como(SUJETO, ESCRITURA):
            propuesta = await llamar(
                servidor,
                "cancelar_cita",
                {"cita_id": cita_existente, "motivo": "El paciente viajó"},
            )
            resultado = await llamar(
                servidor,
                "confirmar_operacion",
                {"token_confirmacion": propuesta["token_confirmacion"]},
            )
        assert resultado["resultado"]["libero_cupo"] is True
        sesion_backend.expire_all()
        assert sesion_backend.get(AgendaSlot, escenario.slots_general[0]).estado is EstadoSlot.LIBRE

    async def test_la_aprobacion_no_legaliza_lo_que_deja_de_ser_legal(
        self, servidor: MCPServer[Any], sesion_backend: Session, cita_existente: int
    ) -> None:
        """The state can change between proposing and confirming.

        The tool checks the transition before proposing, so the human is not
        shown an impossible action. That check cannot be the only one: here the
        appointment is cancelled behind the proposal's back, and the domain must
        still refuse at the moment of effect. Approval authorises an action, it
        does not make an illegal one legal.
        """
        from backend.domain.servicios import cancelar_cita

        with como(SUJETO, ESCRITURA):
            propuesta = await llamar(servidor, "confirmar_cita", {"cita_id": cita_existente})

            cancelar_cita(sesion_backend, cita_existente, motivo="urgencia", usuario="otro")
            sesion_backend.commit()

            mensaje = await error_de(
                servidor,
                "confirmar_operacion",
                {"token_confirmacion": propuesta["token_confirmacion"]},
            )
        assert "TRANSICION_INVALIDA" in mensaje or "estado final" in mensaje

    async def test_el_flujo_de_lista_de_espera_completo(
        self, servidor: MCPServer[Any], sesion_backend: Session, escenario: Escenario
    ) -> None:
        inscribir_en_lista_espera(
            sesion_backend,
            paciente_id=escenario.carla_id,
            especialidad=Especialidad.ORTODONCIA,
        )
        sesion_backend.commit()

        with como(SUJETO, ESCRITURA):
            propuesta = await llamar(
                servidor, "ofrecer_cupo_lista_espera", {"slot_id": escenario.slots_orto[0]}
            )
            assert "NO se agenda" in " ".join(propuesta["esto_va_a_pasar"])
            resultado = await llamar(
                servidor,
                "confirmar_operacion",
                {"token_confirmacion": propuesta["token_confirmacion"]},
            )

        oferta = resultado["resultado"]
        assert oferta["paciente_id"] == escenario.carla_id
        assert oferta["telefono"]
        # Offering contacts someone; it does not book.
        assert contar_citas(sesion_backend) == 0


class TestAuditoriaDeEscritura:
    async def test_la_propuesta_y_la_confirmacion_se_registran_por_separado(
        self, servidor: MCPServer[Any], ctx: Any, escenario: Escenario
    ) -> None:
        with como(SUJETO, ESCRITURA):
            propuesta = await llamar(
                servidor,
                "agendar_cita",
                {"paciente_id": escenario.ana_id, "slot_id": escenario.slots_general[0]},
            )
            await llamar(
                servidor,
                "confirmar_operacion",
                {"token_confirmacion": propuesta["token_confirmacion"]},
            )

        eventos = [e["evento"] for e in ctx.auditor.eventos]
        assert "aprobacion.propuesta" in eventos
        assert "aprobacion.confirmada" in eventos

    async def test_la_ejecucion_queda_marcada_como_aprobada(
        self, servidor: MCPServer[Any], ctx: Any, escenario: Escenario
    ) -> None:
        with como(SUJETO, ESCRITURA):
            propuesta = await llamar(
                servidor,
                "agendar_cita",
                {"paciente_id": escenario.ana_id, "slot_id": escenario.slots_general[0]},
            )
            await llamar(
                servidor,
                "confirmar_operacion",
                {"token_confirmacion": propuesta["token_confirmacion"]},
            )
        ejecuciones = [
            e
            for e in ctx.auditor.eventos
            if e["evento"] == "tool.invocacion" and e.get("con_aprobacion_humana")
        ]
        assert ejecuciones
        assert ejecuciones[-1]["resultado"] == "ok"

    async def test_el_token_no_se_copia_al_log(
        self, servidor: MCPServer[Any], ctx: Any, escenario: Escenario
    ) -> None:
        with como(SUJETO, ESCRITURA):
            propuesta = await llamar(
                servidor,
                "agendar_cita",
                {"paciente_id": escenario.ana_id, "slot_id": escenario.slots_general[0]},
            )
            await llamar(
                servidor,
                "confirmar_operacion",
                {"token_confirmacion": propuesta["token_confirmacion"]},
            )
        assert propuesta["token_confirmacion"] not in str(ctx.auditor.eventos)


class TestElRestoDelCicloDeVida:
    """The remaining write actions, each through propose → approve → execute."""

    async def test_confirmar(
        self, servidor: MCPServer[Any], sesion_backend: Session, cita_existente: int
    ) -> None:
        with como(SUJETO, ESCRITURA):
            propuesta = await llamar(servidor, "confirmar_cita", {"cita_id": cita_existente})
            resultado = await llamar(
                servidor,
                "confirmar_operacion",
                {"token_confirmacion": propuesta["token_confirmacion"]},
            )
        assert resultado["resultado"]["estado_nuevo"] == "confirmada"
        sesion_backend.expire_all()
        assert sesion_backend.get(Cita, cita_existente).estado is EstadoCita.CONFIRMADA

    async def test_registrar_asistencia_genera_el_cargo(
        self, servidor: MCPServer[Any], sesion_backend: Session, cita_existente: int
    ) -> None:
        with como(SUJETO, ESCRITURA):
            for estado in ("en_espera", "atendida"):
                if estado == "en_espera":
                    confirmar = await llamar(
                        servidor, "confirmar_cita", {"cita_id": cita_existente}
                    )
                    await llamar(
                        servidor,
                        "confirmar_operacion",
                        {"token_confirmacion": confirmar["token_confirmacion"]},
                    )
                propuesta = await llamar(
                    servidor,
                    "registrar_asistencia",
                    {"cita_id": cita_existente, "estado": estado},
                )
                resultado = await llamar(
                    servidor,
                    "confirmar_operacion",
                    {"token_confirmacion": propuesta["token_confirmacion"]},
                )
        assert resultado["resultado"]["genero_cargo"] is True
        assert resultado["resultado"]["cargo"]["concepto"] == "cuota_moderadora"

    async def test_la_propuesta_de_asistencia_explica_el_cargo(
        self, servidor: MCPServer[Any], sesion_backend: Session, cita_existente: int
    ) -> None:
        from backend.domain.servicios import confirmar_cita, registrar_asistencia
        from backend.enums import EstadoCita as E

        confirmar_cita(sesion_backend, cita_existente, usuario="setup")
        registrar_asistencia(sesion_backend, cita_existente, E.EN_ESPERA, usuario="setup")
        sesion_backend.commit()

        with como(SUJETO, ESCRITURA):
            propuesta = await llamar(
                servidor,
                "registrar_asistencia",
                {"cita_id": cita_existente, "estado": "atendida"},
            )
        assert any("cargo" in e for e in propuesta["esto_va_a_pasar"])

    async def test_la_propuesta_de_no_show_explica_la_penalizacion(
        self, servidor: MCPServer[Any], cita_existente: int
    ) -> None:
        with como(SUJETO, ESCRITURA):
            propuesta = await llamar(
                servidor,
                "registrar_asistencia",
                {"cita_id": cita_existente, "estado": "no_asistio"},
            )
        efectos = " ".join(propuesta["esto_va_a_pasar"])
        assert "cupo quedará libre" in efectos
        assert "penalización" in efectos

    async def test_reprogramar(
        self,
        servidor: MCPServer[Any],
        sesion_backend: Session,
        cita_existente: int,
        escenario: Escenario,
    ) -> None:
        with como(SUJETO, ESCRITURA):
            propuesta = await llamar(
                servidor,
                "reprogramar_cita",
                {
                    "cita_id": cita_existente,
                    "nuevo_slot_id": escenario.slots_general[2],
                    "motivo": "Choque de agenda",
                },
            )
            resultado = await llamar(
                servidor,
                "confirmar_operacion",
                {"token_confirmacion": propuesta["token_confirmacion"]},
            )
        nueva = resultado["resultado"]["cita"]
        assert nueva["cita_origen_id"] == cita_existente
        sesion_backend.expire_all()
        assert sesion_backend.get(AgendaSlot, escenario.slots_general[0]).estado is EstadoSlot.LIBRE

    async def test_la_propuesta_de_reprogramar_nombra_el_doble_efecto(
        self, servidor: MCPServer[Any], cita_existente: int, escenario: Escenario
    ) -> None:
        with como(SUJETO, ESCRITURA):
            propuesta = await llamar(
                servidor,
                "reprogramar_cita",
                {"cita_id": cita_existente, "nuevo_slot_id": escenario.slots_general[2]},
            )
        efectos = " ".join(propuesta["esto_va_a_pasar"])
        assert "quedará libre" in efectos
        assert "quedará ocupado" in efectos


class TestLaPropuestaValidaAntesDeProponer:
    """A proposal a human cannot act on is worse than an error.

    Every check here is repeated in the domain at execution time, because the
    state can change between proposing and confirming. These exist so the person
    reading the proposal is not asked to approve something that will fail.
    """

    async def test_no_propone_agendar_en_un_cupo_ya_ocupado(
        self, servidor: MCPServer[Any], escenario: Escenario, cita_existente: int
    ) -> None:
        with como(SUJETO, ESCRITURA):
            mensaje = await error_de(
                servidor,
                "agendar_cita",
                {"paciente_id": escenario.carla_id, "slot_id": escenario.slots_general[0]},
            )
        assert "SLOT_NO_DISPONIBLE" in mensaje
        assert "cupos libres más cercanos" in mensaje

    async def test_no_propone_agendar_en_el_pasado(
        self, servidor: MCPServer[Any], escenario: Escenario
    ) -> None:
        with como(SUJETO, ESCRITURA):
            mensaje = await error_de(
                servidor,
                "agendar_cita",
                {"paciente_id": escenario.ana_id, "slot_id": escenario.slot_pasado_id},
            )
        assert "SLOT_EN_EL_PASADO" in mensaje

    async def test_la_propuesta_nombra_la_hora_y_el_profesional_reales(
        self, servidor: MCPServer[Any], escenario: Escenario
    ) -> None:
        """Read aloud to a receptionist, 'slot 412' means nothing."""
        with como(SUJETO, ESCRITURA):
            propuesta = await llamar(
                servidor,
                "agendar_cita",
                {"paciente_id": escenario.ana_id, "slot_id": escenario.slots_general[0]},
            )
        assert str(escenario.fecha_futura) in propuesta["resumen"]
        assert "Dra. General" in propuesta["resumen"]

    async def test_no_propone_confirmar_una_cita_ya_confirmada(
        self, servidor: MCPServer[Any], sesion_backend: Session, cita_existente: int
    ) -> None:
        from backend.domain.servicios import confirmar_cita

        confirmar_cita(sesion_backend, cita_existente, usuario="setup")
        sesion_backend.commit()

        with como(SUJETO, ESCRITURA):
            mensaje = await error_de(servidor, "confirmar_cita", {"cita_id": cita_existente})
        assert "TRANSICION_INVALIDA" in mensaje
        assert "en_espera" in mensaje

    async def test_no_propone_atender_saltandose_la_sala_de_espera(
        self, servidor: MCPServer[Any], cita_existente: int
    ) -> None:
        with como(SUJETO, ESCRITURA):
            mensaje = await error_de(
                servidor,
                "registrar_asistencia",
                {"cita_id": cita_existente, "estado": "atendida"},
            )
        assert "TRANSICION_INVALIDA" in mensaje
        assert "confirmada" in mensaje

    async def test_no_propone_cancelar_una_cita_ya_cancelada(
        self, servidor: MCPServer[Any], sesion_backend: Session, cita_existente: int
    ) -> None:
        from backend.domain.servicios import cancelar_cita

        cancelar_cita(sesion_backend, cita_existente, motivo="ya estaba", usuario="setup")
        sesion_backend.commit()

        with como(SUJETO, ESCRITURA):
            mensaje = await error_de(
                servidor,
                "cancelar_cita",
                {"cita_id": cita_existente, "motivo": "otra vez"},
            )
        assert "TRANSICION_INVALIDA" in mensaje
        assert "estado final" in mensaje

    async def test_no_propone_reprogramar_a_un_cupo_ocupado(
        self,
        servidor: MCPServer[Any],
        sesion_backend: Session,
        cita_existente: int,
        escenario: Escenario,
    ) -> None:
        agendar_cita(
            sesion_backend,
            paciente_id=escenario.carla_id,
            slot_id=escenario.slots_general[3],
            usuario="setup",
        )
        sesion_backend.commit()

        with como(SUJETO, ESCRITURA):
            mensaje = await error_de(
                servidor,
                "reprogramar_cita",
                {"cita_id": cita_existente, "nuevo_slot_id": escenario.slots_general[3]},
            )
        assert "SLOT_NO_DISPONIBLE" in mensaje

    async def test_una_cita_inexistente_falla_al_proponer_no_al_confirmar(
        self, servidor: MCPServer[Any], escenario: Escenario
    ) -> None:
        with como(SUJETO, ESCRITURA):
            mensaje = await error_de(servidor, "confirmar_cita", {"cita_id": 424242})
        assert "CITA_NO_ENCONTRADA" in mensaje


class TestElCruceDeHorarioSeDetectaAlProponer:
    async def test_no_propone_una_cita_que_se_cruza_con_otra(
        self, servidor: MCPServer[Any], sesion_backend: Session, escenario: Escenario
    ) -> None:
        """Same hour, different professional. The patient cannot be in two
        chairs at once, and the proposal must say so rather than the
        confirmation."""
        agendar_cita(
            sesion_backend,
            paciente_id=escenario.ana_id,
            slot_id=escenario.slots_general[0],
            usuario="setup",
        )
        sesion_backend.commit()

        with como(SUJETO, ESCRITURA):
            mensaje = await error_de(
                servidor,
                "agendar_cita",
                {"paciente_id": escenario.ana_id, "slot_id": escenario.slots_orto[0]},
            )
        assert "PACIENTE_YA_TIENE_CITA" in mensaje
        assert "Cancela o reprograma" in mensaje

    async def test_la_especialidad_esperada_se_verifica_al_proponer(
        self, servidor: MCPServer[Any], escenario: Escenario
    ) -> None:
        with como(SUJETO, ESCRITURA):
            mensaje = await error_de(
                servidor,
                "agendar_cita",
                {
                    "paciente_id": escenario.ana_id,
                    "slot_id": escenario.slots_general[0],
                    "especialidad_esperada": "ortodoncia",
                },
            )
        assert "ESPECIALIDAD_NO_COINCIDE" in mensaje

    async def test_reprogramar_no_choca_consigo_misma(
        self, servidor: MCPServer[Any], escenario: Escenario, cita_existente: int
    ) -> None:
        """The appointment being moved is excluded from the overlap check,
        otherwise every reschedule to an overlapping hour would be refused."""
        with como(SUJETO, ESCRITURA):
            propuesta = await llamar(
                servidor,
                "reprogramar_cita",
                {"cita_id": cita_existente, "nuevo_slot_id": escenario.slots_orto[0]},
            )
        assert propuesta["requiere_confirmacion"] is True


class TestLosRechazosSeAuditan:
    """A log that records only what succeeded cannot tell you an agent spent an
    hour proposing something impossible."""

    async def test_un_rechazo_por_transicion_queda_en_el_log(
        self, servidor: MCPServer[Any], ctx: Any, cita_existente: int
    ) -> None:
        with como(SUJETO, ESCRITURA):
            await error_de(
                servidor,
                "registrar_asistencia",
                {"cita_id": cita_existente, "estado": "atendida"},
            )
        evento = ctx.auditor.eventos[-1]
        assert evento["herramienta"] == "registrar_asistencia"
        assert evento["resultado"] == "error"
        assert evento["codigo_error"] == "TRANSICION_INVALIDA"

    async def test_un_rechazo_por_cupo_ocupado_queda_en_el_log(
        self, servidor: MCPServer[Any], ctx: Any, escenario: Escenario, cita_existente: int
    ) -> None:
        with como(SUJETO, ESCRITURA):
            await error_de(
                servidor,
                "agendar_cita",
                {"paciente_id": escenario.carla_id, "slot_id": escenario.slots_general[0]},
            )
        evento = ctx.auditor.eventos[-1]
        assert evento["resultado"] == "error"
        assert evento["codigo_error"] == "SLOT_NO_DISPONIBLE"

    async def test_el_motivo_sigue_redactado_al_rechazar(
        self, servidor: MCPServer[Any], ctx: Any, sesion_backend: Session, cita_existente: int
    ) -> None:
        """A refusal must not become a loophole that copies clinical data into
        the log."""
        from backend.domain.servicios import cancelar_cita

        cancelar_cita(sesion_backend, cita_existente, motivo="ya estaba", usuario="setup")
        sesion_backend.commit()

        secreto = "sangrado persistente desde el martes"
        with como(SUJETO, ESCRITURA):
            await error_de(
                servidor, "cancelar_cita", {"cita_id": cita_existente, "motivo": secreto}
            )
        assert secreto not in str(ctx.auditor.eventos)
        assert ctx.auditor.eventos[-1]["argumentos"]["motivo"] == "«redactado»"

    async def test_una_propuesta_exitosa_se_marca_como_propuesta(
        self, servidor: MCPServer[Any], ctx: Any, cita_existente: int
    ) -> None:
        with como(SUJETO, ESCRITURA):
            await llamar(servidor, "confirmar_cita", {"cita_id": cita_existente})
        eventos = [e for e in ctx.auditor.eventos if e["evento"] == "tool.invocacion"]
        assert eventos[-1]["resultado"] == "propuesta"
