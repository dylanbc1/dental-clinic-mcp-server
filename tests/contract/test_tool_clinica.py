"""The clinical tool, end to end, with three gates stacked.

Scope, human approval, and recorded consent. The test that matters most is the
one where the caller holds every scope and a fresh approval and is *still*
refused, because consent belongs to the patient rather than to the operator.
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer
from sqlalchemy.orm import Session

from backend.domain.servicios import agendar_cita, obtener_cita
from tests.conftest import SUJETO, Escenario, como, error_de, llamar

pytestmark = [pytest.mark.integration, pytest.mark.security]

CLINICO = ["read", "write", "clinical"]


@pytest.fixture
def cita_con_consentimiento(sesion_backend: Session, escenario: Escenario) -> int:
    """Ana has consent on file."""
    cita = agendar_cita(
        sesion_backend,
        paciente_id=escenario.ana_id,
        slot_id=escenario.slots_general[0],
        usuario="setup",
    ).cita
    sesion_backend.commit()
    return cita.id


@pytest.fixture
def cita_sin_consentimiento(sesion_backend: Session, escenario: Escenario) -> int:
    """Carla does not."""
    cita = agendar_cita(
        sesion_backend,
        paciente_id=escenario.carla_id,
        slot_id=escenario.slots_general[1],
        usuario="setup",
    ).cita
    sesion_backend.commit()
    return cita.id


class TestConConsentimiento:
    async def test_el_ciclo_completo_registra_el_motivo(
        self,
        servidor: MCPServer[Any],
        sesion_backend: Session,
        cita_con_consentimiento: int,
    ) -> None:
        with como(SUJETO, CLINICO):
            propuesta = await llamar(
                servidor,
                "registrar_motivo_consulta",
                {"cita_id": cita_con_consentimiento, "motivo": "Dolor en molar inferior"},
            )
            resultado = await llamar(
                servidor,
                "confirmar_operacion",
                {"token_confirmacion": propuesta["token_confirmacion"]},
            )

        assert resultado["resultado"]["motivo"] == "Dolor en molar inferior"
        sesion_backend.expire_all()
        cita = obtener_cita(sesion_backend, cita_con_consentimiento)
        assert cita.motivo_registrado_por == SUJETO

    async def test_la_propuesta_advierte_de_la_regulacion(
        self, servidor: MCPServer[Any], cita_con_consentimiento: int
    ) -> None:
        with como(SUJETO, CLINICO):
            propuesta = await llamar(
                servidor,
                "registrar_motivo_consulta",
                {"cita_id": cita_con_consentimiento, "motivo": "Dolor"},
            )
        advertencias = " ".join(propuesta["advertencias"])
        assert "2654" in advertencias
        assert "1581" in advertencias

    async def test_la_propuesta_no_escribe_nada_todavia(
        self,
        servidor: MCPServer[Any],
        sesion_backend: Session,
        cita_con_consentimiento: int,
    ) -> None:
        with como(SUJETO, CLINICO):
            await llamar(
                servidor,
                "registrar_motivo_consulta",
                {"cita_id": cita_con_consentimiento, "motivo": "Dolor agudo"},
            )
        sesion_backend.expire_all()
        assert obtener_cita(sesion_backend, cita_con_consentimiento).motivo is None


class TestSinConsentimiento:
    async def test_el_scope_no_alcanza_sin_consentimiento(
        self, servidor: MCPServer[Any], cita_sin_consentimiento: int
    ) -> None:
        """Every gate open except the patient's own authorisation, and that is
        the one that must still stop it."""
        with como(SUJETO, CLINICO):
            propuesta = await llamar(
                servidor,
                "registrar_motivo_consulta",
                {"cita_id": cita_sin_consentimiento, "motivo": "Dolor"},
            )
            mensaje = await error_de(
                servidor,
                "confirmar_operacion",
                {"token_confirmacion": propuesta["token_confirmacion"]},
            )
        assert "CONSENTIMIENTO_REQUERIDO" in mensaje
        assert "2654" in mensaje
        assert "Acción requerida" in mensaje

    async def test_el_rechazo_no_deja_el_motivo_escrito(
        self,
        servidor: MCPServer[Any],
        sesion_backend: Session,
        cita_sin_consentimiento: int,
    ) -> None:
        with como(SUJETO, CLINICO):
            propuesta = await llamar(
                servidor,
                "registrar_motivo_consulta",
                {"cita_id": cita_sin_consentimiento, "motivo": "Dolor severo"},
            )
            await error_de(
                servidor,
                "confirmar_operacion",
                {"token_confirmacion": propuesta["token_confirmacion"]},
            )
        sesion_backend.expire_all()
        assert obtener_cita(sesion_backend, cita_sin_consentimiento).motivo is None


class TestAuditoriaClinica:
    async def test_el_acceso_clinico_tiene_su_propio_evento(
        self, servidor: MCPServer[Any], ctx: Any, cita_con_consentimiento: int
    ) -> None:
        """Res. 2654 asks who touched clinical data. Burying that in the generic
        invocation stream makes it unanswerable at audit time."""
        with como("odontologa@clinica.test", CLINICO):
            propuesta = await llamar(
                servidor,
                "registrar_motivo_consulta",
                {"cita_id": cita_con_consentimiento, "motivo": "Control"},
            )
            await llamar(
                servidor,
                "confirmar_operacion",
                {"token_confirmacion": propuesta["token_confirmacion"]},
            )

        clinicos = [e for e in ctx.auditor.eventos if e["evento"] == "clinico.acceso"]
        assert [e["resultado"] for e in clinicos] == ["propuesta", "registrado"]
        assert all(e["sujeto"] == "odontologa@clinica.test" for e in clinicos)
        assert all(e["cita_id"] == cita_con_consentimiento for e in clinicos)

    async def test_un_rechazo_tambien_queda_auditado(
        self, servidor: MCPServer[Any], ctx: Any, cita_sin_consentimiento: int
    ) -> None:
        with como(SUJETO, CLINICO):
            propuesta = await llamar(
                servidor,
                "registrar_motivo_consulta",
                {"cita_id": cita_sin_consentimiento, "motivo": "Dolor"},
            )
            await error_de(
                servidor,
                "confirmar_operacion",
                {"token_confirmacion": propuesta["token_confirmacion"]},
            )
        clinicos = [e for e in ctx.auditor.eventos if e["evento"] == "clinico.acceso"]
        assert clinicos[-1]["resultado"] == "rechazado:CONSENTIMIENTO_REQUERIDO"

    async def test_el_motivo_no_se_copia_al_log(
        self, servidor: MCPServer[Any], ctx: Any, cita_con_consentimiento: int
    ) -> None:
        """The reason for consultation is the clinical datum itself. Auditing
        the access must not duplicate it somewhere less protected."""
        secreto = "sangrado gingival persistente hace tres semanas"
        with como(SUJETO, CLINICO):
            await llamar(
                servidor,
                "registrar_motivo_consulta",
                {"cita_id": cita_con_consentimiento, "motivo": secreto},
            )
        assert secreto not in str(ctx.auditor.eventos)
        assert any(
            e.get("argumentos", {}).get("motivo") == "«redactado»"
            for e in ctx.auditor.eventos
            if e["evento"] == "tool.invocacion"
        )
