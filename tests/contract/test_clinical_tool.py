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

CLINICAL = ["read", "write", "clinical"]


@pytest.fixture
def appointment_with_consent(backend_session: Session, scenario: Scenario) -> int:
    """Ana has consent on file."""
    appointment = book_appointment(
        backend_session,
        patient_id=scenario.ana_id,
        slot_id=scenario.slots_general[0],
        user="setup",
    ).appointment
    backend_session.commit()
    return appointment.id


@pytest.fixture
def appointment_without_consent(backend_session: Session, scenario: Scenario) -> int:
    """Carla does not."""
    appointment = book_appointment(
        backend_session,
        patient_id=scenario.carla_id,
        slot_id=scenario.slots_general[1],
        user="setup",
    ).appointment
    backend_session.commit()
    return appointment.id


class TestWithConsent:
    async def test_the_full_cycle_records_the_reason(
        self, mcp: MCPTestClient, backend_session: Session, appointment_with_consent: int
    ) -> None:
        args = {"appointment_id": appointment_with_consent, "reason": "Dolor en molar inferior"}
        with as_caller(SUBJECT, CLINICAL):
            result = await mcp.approve("record_visit_reason", args)

        assert result["reason"] == "Dolor en molar inferior"
        backend_session.expire_all()
        assert (
            get_appointment(backend_session, appointment_with_consent).reason_recorded_by == SUBJECT
        )

    async def test_the_question_warns_about_the_regulation(
        self, mcp: MCPTestClient, appointment_with_consent: int
    ) -> None:
        args = {"appointment_id": appointment_with_consent, "reason": "Dolor"}
        with as_caller(SUBJECT, CLINICAL):
            message = mcp.question_text(await mcp.ask("record_visit_reason", args))
        assert "2654" in message
        assert "1581" in message

    async def test_asking_writes_nothing_yet(
        self, mcp: MCPTestClient, backend_session: Session, appointment_with_consent: int
    ) -> None:
        args = {"appointment_id": appointment_with_consent, "reason": "Dolor agudo"}
        with as_caller(SUBJECT, CLINICAL):
            await mcp.ask("record_visit_reason", args)
        backend_session.expire_all()
        assert get_appointment(backend_session, appointment_with_consent).reason is None


class TestWithoutConsent:
    async def test_neither_the_scope_nor_the_approval_is_enough(
        self, mcp: MCPTestClient, appointment_without_consent: int
    ) -> None:
        """Every gate open except the patient's own authorisation, and that is
        the one that must still stop it."""
        args = {"appointment_id": appointment_without_consent, "reason": "Dolor"}
        with as_caller(SUBJECT, CLINICAL), pytest.raises(ToolCallError) as exc:
            await mcp.approve("record_visit_reason", args)
        assert "CONSENT_REQUIRED" in exc.value.text_of
        assert "2654" in exc.value.text_of
        assert "Action required" in exc.value.text_of

    async def test_the_refusal_leaves_no_reason_written(
        self, mcp: MCPTestClient, backend_session: Session, appointment_without_consent: int
    ) -> None:
        args = {"appointment_id": appointment_without_consent, "reason": "Dolor severo"}
        with as_caller(SUBJECT, CLINICAL), pytest.raises(ToolCallError):
            await mcp.approve("record_visit_reason", args)
        backend_session.expire_all()
        assert get_appointment(backend_session, appointment_without_consent).reason is None


class TestClinicalAudit:
    async def test_clinical_access_has_its_own_event(
        self, mcp: MCPTestClient, ctx: Any, appointment_with_consent: int
    ) -> None:
        """Res. 2654 asks who touched clinical data. Burying that in the generic
        invocation stream makes it unanswerable at audit time."""
        args = {"appointment_id": appointment_with_consent, "reason": "Control"}
        with as_caller("odontologa@clinica.test", CLINICAL):
            await mcp.approve("record_visit_reason", args)

        clinical_tools = [e for e in ctx.auditor.events if e["event"] == "clinical.access"]
        assert clinical_tools[-1]["result"] == "recorded"
        assert all(e["subject"] == "odontologa@clinica.test" for e in clinical_tools)
        assert all(e["appointment_id"] == appointment_with_consent for e in clinical_tools)

    async def test_a_refusal_is_audited_too(
        self, mcp: MCPTestClient, ctx: Any, appointment_without_consent: int
    ) -> None:
        args = {"appointment_id": appointment_without_consent, "reason": "Dolor"}
        with as_caller(SUBJECT, CLINICAL), pytest.raises(ToolCallError):
            await mcp.approve("record_visit_reason", args)
        clinical_tools = [e for e in ctx.auditor.events if e["event"] == "clinical.access"]
        assert clinical_tools[-1]["result"] == "refused:CONSENT_REQUIRED"

    async def test_the_reason_is_not_copied_into_the_log(
        self, mcp: MCPTestClient, ctx: Any, appointment_with_consent: int
    ) -> None:
        """The reason for consultation is the clinical datum itself. Auditing
        the access must not duplicate it somewhere less protected."""
        secreto = "sangrado gingival persistente hace tres semanas"
        with as_caller(SUBJECT, CLINICAL):
            await mcp.approve(
                "record_visit_reason",
                {"appointment_id": appointment_with_consent, "reason": secreto},
            )
        assert secreto not in str(ctx.auditor.events)
        assert any(
            e.get("arguments", {}).get("reason") == "«redacted»"
            for e in ctx.auditor.events
            if e["event"] == "tool.invocation"
        )

    async def test_the_clinical_reason_does_not_travel_in_the_human_message(
        self, mcp: MCPTestClient, appointment_with_consent: int
    ) -> None:
        """The person approving needs to know *that* a reason is being recorded,
        and for whom. The reason itself is the patient's, and echoing it back
        through the client would put clinical data in one more place."""
        secreto = "absceso periapical según el paciente"
        with as_caller(SUBJECT, CLINICAL):
            message = mcp.question_text(
                await mcp.ask(
                    "record_visit_reason",
                    {"appointment_id": appointment_with_consent, "reason": secreto},
                )
            )
        assert secreto not in message
        assert "motivo de consulta" in message
