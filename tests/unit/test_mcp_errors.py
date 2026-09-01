"""How a failure is rendered for the model.

The message a tool returns *is* the recovery instruction. These tests pin the
rendering so a refactor cannot quietly turn an actionable error back into
"something went wrong".
"""

from __future__ import annotations

import pytest

from backend.domain.errors import ErrorCode, SlotUnavailable
from mcp_server.errors import (
    StructuredToolError,
    backend_down_error,
    scope_error,
    unauthenticated_error,
)

pytestmark = pytest.mark.security


class TestRendering:
    def test_the_minimum_is_a_code_and_a_message(self) -> None:
        error = StructuredToolError("X", "algo pasó")
        assert error.render() == "[X] algo pasó"

    def test_the_suggestion_is_labelled_as_such(self) -> None:
        error = StructuredToolError("X", "algo pasó", suggestion="haz esto otro")
        assert "Suggestion: haz esto otro" in error.render()

    def test_permission_errors_ask_to_escalate_not_to_retry(self) -> None:
        """ "Sugerencia" invites a retry. A missing permission never resolves by
        retrying, so those get a stronger lead-in."""
        error = StructuredToolError(
            str(ErrorCode.INSUFFICIENT_SCOPE), "falta permiso", suggestion="pide write"
        )
        assert "Action required: pide write" in error.render()

    def test_the_details_travel_as_readable_json(self) -> None:
        error = StructuredToolError("X", "y", details={"slot_id": 4, "libres": [1, 2]})
        assert '"slot_id": 4' in error.render()

    def test_the_details_do_not_blow_up_on_odd_types(self) -> None:
        from datetime import date

        error = StructuredToolError("X", "y", details={"cuando": date(2026, 9, 1)})
        assert "2026-09-01" in error.render()

    def test_the_exception_message_is_the_rendered_question(self) -> None:
        error = StructuredToolError("X", "y", suggestion="z")
        assert str(error) == error.render()

    def test_to_dict_omits_what_is_empty(self) -> None:
        assert StructuredToolError("X", "y").to_dict() == {
            "error": True,
            "code": "X",
            "message": "y",
        }


class TestConversions:
    def test_it_rebuilds_from_the_backend_envelope(self) -> None:
        error = StructuredToolError.from_envelope(
            {
                "error": True,
                "code": "SLOT_UNAVAILABLE",
                "message": "ocupado",
                "suggestion": "prueba 09:00",
                "details": {"slot_id": 3},
            }
        )
        assert error.code == "SLOT_UNAVAILABLE"
        assert error.details == {"slot_id": 3}

    def test_an_incomplete_envelope_does_not_blow_up(self) -> None:
        error = StructuredToolError.from_envelope({})
        assert error.code == "INTERNAL_ERROR"
        assert error.message

    def test_converts_a_domain_error(self) -> None:
        as_entries = SlotUnavailable("ocupado", suggestion="prueba otro", details={"a": 1})
        error = StructuredToolError.from_domain(as_entries)
        assert error.code == "SLOT_UNAVAILABLE"
        assert error.suggestion == "prueba otro"


class TestPrebuiltErrors:
    def test_unauthenticated_points_at_discovery(self) -> None:
        error = unauthenticated_error("http://localhost:8080")
        assert error.code == "NOT_AUTHENTICATED"
        assert "oauth-protected-resource" in (error.suggestion or "")

    def test_the_scope_error_names_what_is_missing_and_what_is_held(self) -> None:
        error = scope_error("cancel_appointment", "write", ["read"])
        assert error.details["required_scope"] == "write"
        assert error.details["token_scopes"] == ["read"]
        assert "do not call this tool again" in (error.suggestion or "").lower()

    def test_the_scope_error_with_no_scopes_says_so_explicitly(self) -> None:
        assert "no scopes" in (scope_error("x", "read", []).suggestion or "")

    def test_the_backend_down_error_absolves_the_caller(self) -> None:
        """The model must not conclude its own arguments were wrong."""
        error = backend_down_error("connection refused")
        assert "not a problem with your request" in (error.suggestion or "")
        assert "do not retry in a loop" in (error.suggestion or "")
