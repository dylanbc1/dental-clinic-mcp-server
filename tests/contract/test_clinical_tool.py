"""The clinical tool over MRTR, with three gates stacked.

Scope, human approval, and recorded consent. The test that matters most is the
one where the caller holds every scope and a person approved, and it is *still*
refused, because consent belongs to the patient rather than to the operator.
"""

from typing import Any

import pytest
from sqlalchemy.orm import Session

from backend.domain.services import book_appointment, get_appointment
from tests.conftest import SUBJECT, MCPTestClient, Scenario, ToolCallError, as_caller

pytestmark = [pytest.mark.integration, pytest.mark.security]

CLINICO = ["read", "write", "clinical"]


@pytest.fixture
def appointment_with_consent(backend_session: Session, scenario: Scenario) -> int:
    """Ana has consent on file."""
    cita = book_appointment(
        backend_session,
        paciente_id=scenario.ana_id,
        slot_id=scenario.slots_general[0],
        usuario="setup",
    ).cita
    backend_session.commit()
    return cita.id


@pytest.fixture
def appointment_without_consent(backend_session: Session, scenario: Scenario) -> int:
    """Carla does not."""
    cita = book_appointment(
        backend_session,
        paciente_id=scenario.carla_id,
        slot_id=scenario.slots_general[1],
        usuario="setup",
    ).cita
    backend_session.commit()
    return cita.id


class TestConConsentimiento:
    async def test_el_ciclo_completo_registra_el_motivo(
        self, mcp: MCPTestClient, backend_session: Session, appointment_with_consent: int
    ) -> None:
        args = {"cita_id": appointment_with_consent, "motivo": "Dolor en molar inferior"}
        with as_caller(SUBJECT, CLINICO):
            result = await mcp.aprobar("record_visit_reason", args)

        assert result["motivo"] == "Dolor en molar inferior"
        backend_session.expire_all()
        assert (
            get_appointment(backend_session, appointment_with_consent).motivo_registrado_por
            == SUBJECT
        )

    async def test_la_pregunta_advierte_de_la_regulacion(
        self, mcp: MCPTestClient, appointment_with_consent: int
    ) -> None:
        args = {"cita_id": appointment_with_consent, "motivo": "Dolor"}
        with as_caller(SUBJECT, CLINICO):
            mensaje = mcp.question_text(await mcp.ask("record_visit_reason", args))
        assert "2654" in mensaje
        assert "1581" in mensaje

    async def test_preguntar_no_escribe_nada_todavia(
        self, mcp: MCPTestClient, backend_session: Session, appointment_with_consent: int
    ) -> None:
        args = {"cita_id": appointment_with_consent, "motivo": "Dolor agudo"}
        with as_caller(SUBJECT, CLINICO):
            await mcp.ask("record_visit_reason", args)
        backend_session.expire_all()
        assert get_appointment(backend_session, appointment_with_consent).motivo is None


class TestSinConsentimiento:
    async def test_ni_el_scope_ni_la_aprobacion_alcanzan(
        self, mcp: MCPTestClient, appointment_without_consent: int
    ) -> None:
        """Every gate open except the patient's own authorisation, and that is
        the one that must still stop it."""
        args = {"cita_id": appointment_without_consent, "motivo": "Dolor"}
        with as_caller(SUBJECT, CLINICO), pytest.raises(ToolCallError) as exc:
            await mcp.aprobar("record_visit_reason", args)
        assert "CONSENTIMIENTO_REQUERIDO" in exc.value.text_of
        assert "2654" in exc.value.text_of
        assert "Action required" in exc.value.text_of

    async def test_el_rechazo_no_deja_el_motivo_escrito(
        self, mcp: MCPTestClient, backend_session: Session, appointment_without_consent: int
    ) -> None:
        args = {"cita_id": appointment_without_consent, "motivo": "Dolor severo"}
        with as_caller(SUBJECT, CLINICO), pytest.raises(ToolCallError):
            await mcp.aprobar("record_visit_reason", args)
        backend_session.expire_all()
        assert get_appointment(backend_session, appointment_without_consent).motivo is None


class TestAuditoriaClinica:
    async def test_el_acceso_clinico_tiene_su_propio_evento(
        self, mcp: MCPTestClient, ctx: Any, appointment_with_consent: int
    ) -> None:
        """Res. 2654 asks who touched clinical data. Burying that in the generic
        invocation stream makes it unanswerable at audit time."""
        args = {"cita_id": appointment_with_consent, "motivo": "Control"}
        with as_caller("odontologa@clinica.test", CLINICO):
            await mcp.aprobar("record_visit_reason", args)

        clinicos = [e for e in ctx.auditor.events if e["event"] == "clinical.access"]
        assert clinicos[-1]["result"] == "registrado"
        assert all(e["subject"] == "odontologa@clinica.test" for e in clinicos)
        assert all(e["cita_id"] == appointment_with_consent for e in clinicos)

    async def test_un_rechazo_tambien_queda_auditado(
        self, mcp: MCPTestClient, ctx: Any, appointment_without_consent: int
    ) -> None:
        args = {"cita_id": appointment_without_consent, "motivo": "Dolor"}
        with as_caller(SUBJECT, CLINICO), pytest.raises(ToolCallError):
            await mcp.aprobar("record_visit_reason", args)
        clinicos = [e for e in ctx.auditor.events if e["event"] == "clinical.access"]
        assert clinicos[-1]["result"] == "rechazado:CONSENTIMIENTO_REQUERIDO"

    async def test_el_motivo_no_se_copia_al_log(
        self, mcp: MCPTestClient, ctx: Any, appointment_with_consent: int
    ) -> None:
        """The reason for consultation is the clinical datum itself. Auditing
        the access must not duplicate it somewhere less protected."""
        secreto = "sangrado gingival persistente hace tres semanas"
        with as_caller(SUBJECT, CLINICO):
            await mcp.aprobar(
                "record_visit_reason",
                {"cita_id": appointment_with_consent, "motivo": secreto},
            )
        assert secreto not in str(ctx.auditor.events)
        assert any(
            e.get("arguments", {}).get("motivo") == "«redacted»"
            for e in ctx.auditor.events
            if e["event"] == "tool.invocation"
        )

    async def test_el_motivo_clinico_no_viaja_en_el_mensaje_al_humano(
        self, mcp: MCPTestClient, appointment_with_consent: int
    ) -> None:
        """The person approving needs to know *that* a reason is being recorded,
        and for whom. The reason itself is the patient's, and echoing it back
        through the client would put clinical data in one more place."""
        secreto = "absceso periapical según el paciente"
        with as_caller(SUBJECT, CLINICO):
            mensaje = mcp.question_text(
                await mcp.ask(
                    "record_visit_reason",
                    {"cita_id": appointment_with_consent, "motivo": secreto},
                )
            )
        assert secreto not in mensaje
        assert "motivo de consulta" in mensaje
