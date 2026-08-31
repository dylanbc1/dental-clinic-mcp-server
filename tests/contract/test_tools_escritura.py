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

    async def test_un_error_de_dominio_sobrevive_a_la_confirmacion(
        self, servidor: MCPServer[Any], sesion_backend: Session, cita_existente: int
    ) -> None:
        """Approval authorises an action; it does not make an illegal one legal."""
        with como(SUJETO, ESCRITURA):
            propuesta = await llamar(
                servidor,
                "registrar_asistencia",
                {"cita_id": cita_existente, "estado": "atendida"},
            )
            mensaje = await error_de(
                servidor,
                "confirmar_operacion",
                {"token_confirmacion": propuesta["token_confirmacion"]},
            )
        assert "TRANSICION_INVALIDA" in mensaje
        assert "confirmada" in mensaje

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
        self, servidor: MCPServer[Any], cita_existente: int
    ) -> None:
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
