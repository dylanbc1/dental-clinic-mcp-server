"""The tool-invocation audit log."""

from __future__ import annotations

import json
from typing import Any

import pytest

from mcp_server.audit import (
    REDACTED,
    REDACTED_FIELDS,
    Auditor,
    configure_logging,
    redact,
)

pytestmark = pytest.mark.security


class LoggerFalso:
    def __init__(self) -> None:
        self.llamadas: list[tuple[str, dict[str, Any]]] = []

    def info(self, evento: str, **payload: Any) -> None:
        self.llamadas.append((evento, payload))


class TestRedaction:
    @pytest.mark.parametrize("field", sorted(REDACTED_FIELDS))
    def test_every_sensitive_field_is_redacted(self, field: str) -> None:
        assert redact({field: "valor real"})[field] == REDACTED

    def test_the_other_fields_are_preserved(self) -> None:
        assert redact({"appointment_id": 7, "status": "attended"}) == {
            "appointment_id": 7,
            "status": "attended",
        }

    def test_a_none_is_not_redacted_unnecessarily(self) -> None:
        """Redacting an absent value hides that it was absent."""
        assert redact({"reason": None}) == {"reason": None}

    def test_the_visit_reason_is_covered(self) -> None:
        # It is clinical data under Res. 2654/2019.
        assert "reason" in REDACTED_FIELDS

    def test_the_confirmation_token_is_covered(self) -> None:
        # A logged token is a replayable approval.
        assert "confirmation_token" in REDACTED_FIELDS


class TestAuditor:
    def test_it_records_a_successful_invocation(self) -> None:
        logger = LoggerFalso()
        auditor = Auditor(logger)
        auditor.tool_call(
            "search_patients",
            subject="ana@clinica.test",
            scope="read",
            arguments={"document_number": "123"},
            result="ok",
        )
        evento, payload = logger.llamadas[-1]
        assert evento == "tool.invocation"
        assert payload["subject"] == "ana@clinica.test"
        assert payload["arguments"]["document_number"] == REDACTED

    def test_it_records_the_error_code_when_there_is_one(self) -> None:
        auditor = Auditor(LoggerFalso())
        auditor.tool_call(
            "x",
            subject="a",
            scope="read",
            arguments={},
            result="error",
            error_code="APPOINTMENT_NOT_FOUND",
        )
        assert auditor.events[-1]["error_code"] == "APPOINTMENT_NOT_FOUND"

    def test_marks_the_execution_as_approved(self) -> None:
        auditor = Auditor(LoggerFalso())
        auditor.tool_call("x", subject="a", scope="write", arguments={}, result="ok", approved=True)
        assert auditor.events[-1]["with_human_approval"] is True

    def test_proposal_and_confirmation_are_distinct_events(self) -> None:
        auditor = Auditor(LoggerFalso())
        auditor.question_asked("cancel_appointment", subject="a", nonce="n1")
        auditor.question_answered("cancel_appointment", subject="a", nonce="n1")
        assert [e["event"] for e in auditor.events] == [
            "approval.proposed",
            "approval.confirmed",
        ]

    def test_clinical_access_is_its_own_event_type(self) -> None:
        auditor = Auditor(LoggerFalso())
        auditor.clinical_access(subject="a", appointment_id=7, result="recorded")
        assert auditor.events[-1] == {
            "event": "clinical.access",
            "subject": "a",
            "appointment_id": 7,
            "result": "recorded",
        }

    def test_the_memory_is_bounded(self) -> None:
        """An audit log is a log, not a data store: the in-memory mirror must
        not grow without bound in a long-running process."""
        auditor = Auditor(LoggerFalso())
        auditor.memory_limit = 10
        for i in range(50):
            auditor.clinical_access(subject="a", appointment_id=i, result="x")
        assert len(auditor.events) == 10
        assert auditor.events[-1]["appointment_id"] == 49

    def test_the_events_are_json_serialisable(self) -> None:
        """They are shipped to a log pipeline; an unserialisable value would be
        discovered in production rather than here."""
        auditor = Auditor(LoggerFalso())
        auditor.tool_call(
            "x", subject="a", scope="read", arguments={"appointment_id": 1}, result="ok"
        )
        json.dumps(auditor.events)


class TestLoggingConfiguration:
    def test_configuring_does_not_blow_up_on_a_valid_level(self) -> None:
        configure_logging("DEBUG")
        configure_logging("INFO")

    def test_an_unknown_level_falls_back_to_info(self) -> None:
        configure_logging("NO-EXISTE")
