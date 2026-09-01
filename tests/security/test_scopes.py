"""Security layer 2: least privilege, proved exhaustively over the wire.

The whole tool by scope matrix is enumerated. Every tool is called with each of
the three scopes in isolation, and each of the 39 combinations is asserted to
allow or deny. A permission suite written by example always grows a hole; this
one cannot, because there is no combination it does not cover.

The scopes deliberately do **not** nest. `write` does not imply `read` and
`clinical` does not imply `write`, because administrative and clinical are
different kinds of authority, not different amounts of it (§2.5).
"""

import itertools
from typing import Any

import pytest
from sqlalchemy.orm import Session

from backend.domain.services import book_appointment
from mcp_server.auth import Scope
from tests.conftest import SUBJECT, MCPTestClient, Scenario, ToolCallError, as_caller

pytestmark = [pytest.mark.integration, pytest.mark.security]

#: Tool → the single scope it requires.
REQUIRED_SCOPE: dict[str, Scope] = {
    "search_patients": Scope.READ,
    "check_availability": Scope.READ,
    "get_appointment": Scope.READ,
    "list_patient_appointments": Scope.READ,
    "check_cartera": Scope.READ,
    "validate_afiliacion": Scope.READ,
    "book_appointment": Scope.WRITE,
    "confirm_appointment": Scope.WRITE,
    "cancel_appointment": Scope.WRITE,
    "reschedule_appointment": Scope.WRITE,
    "record_attendance": Scope.WRITE,
    "offer_slot_to_waiting_list": Scope.WRITE,
    "record_visit_reason": Scope.CLINICAL,
}

#: Minimal valid-shaped arguments per tool. The values point at rows that do not
#: exist, which is deliberate: the scope check must happen before any lookup, so
#: a denial cannot depend on the data, and cannot leak whether it exists.
ARGUMENTOS: dict[str, dict[str, Any]] = {
    "search_patients": {"document_number": "11111111"},
    "check_availability": {},
    "get_appointment": {"appointment_id": 424242},
    "list_patient_appointments": {"patient_id": 424242},
    "check_cartera": {"patient_id": 424242},
    "validate_afiliacion": {"patient_id": 424242},
    "book_appointment": {"patient_id": 424242, "slot_id": 424242},
    "confirm_appointment": {"appointment_id": 424242},
    "cancel_appointment": {"appointment_id": 424242, "reason": "motivo de prueba"},
    "reschedule_appointment": {"appointment_id": 424242, "new_slot_id": 424243},
    "record_attendance": {"appointment_id": 424242, "status": "attended"},
    "offer_slot_to_waiting_list": {"slot_id": 424242},
    "record_visit_reason": {"appointment_id": 424242, "reason": "dolor de muela"},
}

MATRIZ = list(itertools.product(sorted(REQUIRED_SCOPE), list(Scope)))


async def error_from(mcp: MCPTestClient, name: str, arguments: dict[str, Any]) -> str:
    """Call over the wire and return the error text, or "" if it got through.

    A write tool that gets through pauses for a human rather than mutating, so
    "got through" here means it reached the point of asking.
    """
    try:
        result = await mcp._rpc("tools/call", {"name": name, "arguments": arguments})
    except ToolCallError as fallo:
        return fallo.text_of
    if result.get("isError"):
        return "\n".join(c.get("text", "") for c in result.get("content", []))
    return ""


class TestScopeMatrix:
    @pytest.mark.parametrize(("tool_name", "scope"), MATRIZ, ids=lambda v: str(v))
    async def test_every_tool_and_scope_combination(
        self, mcp: MCPTestClient, scenario: Scenario, tool_name: str, scope: Scope
    ) -> None:
        required = REQUIRED_SCOPE[tool_name]
        with as_caller(SUBJECT, [str(scope)]):
            message = await error_from(mcp, tool_name, ARGUMENTOS[tool_name])

        if scope is required:
            # It may still fail on the data, since the ids do not exist, but
            # never on permission. That is the distinction being asserted.
            assert "INSUFFICIENT_SCOPE" not in message
        else:
            assert "INSUFFICIENT_SCOPE" in message, (
                f"{tool_name} aceptó un token con scope '{scope}' cuando exige '{required}'"
            )

    def test_the_matrix_covers_every_combination(self) -> None:
        assert len(MATRIZ) == 13 * 3 == 39

    async def test_the_matrix_leaves_no_tool_out(self, server_: Any) -> None:
        """A tool added later without a scope decision fails this test rather
        than shipping ungated."""
        declaradas = {t.name for t in await server_.list_tools()}
        assert declaradas == set(REQUIRED_SCOPE)


class TestScopesDoNotNest:
    async def test_write_does_not_grant_read_access(
        self, mcp: MCPTestClient, scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["write"]):
            message = await error_from(mcp, "search_patients", {"document_number": "11111111"})
        assert "INSUFFICIENT_SCOPE" in message

    async def test_clinical_does_not_grant_write_access(
        self, mcp: MCPTestClient, scenario: Scenario
    ) -> None:
        """Authority to record a symptom is not authority to cancel a visit."""
        with as_caller(SUBJECT, ["clinical"]):
            message = await error_from(
                mcp, "cancel_appointment", {"appointment_id": 1, "reason": "prueba manual"}
            )
        assert "INSUFFICIENT_SCOPE" in message

    async def test_write_does_not_grant_clinical_access(
        self, mcp: MCPTestClient, scenario: Scenario
    ) -> None:
        """The SaaStr shape: a valid token with more reach than it needed."""
        with as_caller(SUBJECT, ["read", "write"]):
            message = await error_from(
                mcp, "record_visit_reason", {"appointment_id": 1, "reason": "dolor"}
            )
        assert "INSUFFICIENT_SCOPE" in message
        assert "clinical" in message


class TestDenialMessage:
    async def test_says_what_is_missing_and_what_to_do(
        self, mcp: MCPTestClient, scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            message = await error_from(
                mcp, "cancel_appointment", {"appointment_id": 1, "reason": "prueba manual"}
            )
        assert "INSUFFICIENT_SCOPE" in message
        assert "'write'" in message
        assert "Action required" in message
        # It must tell the model not to loop: retrying with the same token is
        # the single most common wasted-token pattern.
        assert "do not call this tool again" in message.lower()

    async def test_it_reveals_no_patient_data_when_denying(
        self, mcp: MCPTestClient, scenario: Scenario
    ) -> None:
        """Denial happens before any lookup, so nothing about the record leaks."""
        with as_caller(SUBJECT, ["read"]):
            message = await error_from(
                mcp,
                "record_visit_reason",
                {"appointment_id": 1, "reason": "dolor severo en molar"},
            )
        assert "dolor severo" not in message


class TestWithoutToken:
    async def test_without_an_identity_no_tool_answers(
        self, mcp: MCPTestClient, scenario: Scenario
    ) -> None:
        """No token means no identity. Never a permissive default."""
        for tool_name in REQUIRED_SCOPE:
            message = await error_from(mcp, tool_name, ARGUMENTOS[tool_name])
            assert "NOT_AUTHENTICATED" in message, f"{tool_name} answered without a token"

    async def test_a_token_without_scopes_opens_nothing(
        self, mcp: MCPTestClient, scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, []):
            for tool_name in REQUIRED_SCOPE:
                message = await error_from(mcp, tool_name, ARGUMENTOS[tool_name])
                assert "INSUFFICIENT_SCOPE" in message


class TestScopeIsCheckedOnBothRounds:
    """MRTR splits a mutation across two calls, and authority is checked on both.

    The resolver runs again when the client retries, so a token that has lost a
    scope between the question and the answer cannot execute the operation it
    was allowed to ask about. Authority is verified at the moment of effect, not
    only at the moment of intent.
    """

    async def test_a_token_that_loses_clinical_does_not_execute(
        self, mcp: MCPTestClient, backend_session: Session, scenario: Scenario
    ) -> None:
        appointment = book_appointment(
            backend_session,
            patient_id=scenario.ana_id,
            slot_id=scenario.slots_general[0],
            user="setup",
        ).appointment
        backend_session.commit()
        args = {"appointment_id": appointment.id, "reason": "dolor"}

        with as_caller(SUBJECT, ["read", "write", "clinical"]):
            question = await mcp.ask("record_visit_reason", args)

        # Same subject and a perfectly valid approval, but `clinical` is gone.
        with as_caller(SUBJECT, ["read", "write"]), pytest.raises(ToolCallError) as exc:
            await mcp.respond("record_visit_reason", args, question)
        assert "INSUFFICIENT_SCOPE" in exc.value.text_of
        assert "clinical" in exc.value.text_of

    async def test_a_token_that_loses_write_does_not_execute(
        self, mcp: MCPTestClient, scenario: Scenario, backend_session: Session
    ) -> None:
        appointment = book_appointment(
            backend_session,
            patient_id=scenario.ana_id,
            slot_id=scenario.slots_general[0],
            user="setup",
        ).appointment
        backend_session.commit()

        with as_caller(SUBJECT, ["read", "write"]):
            question = await mcp.ask("confirm_appointment", {"appointment_id": appointment.id})

        with as_caller(SUBJECT, ["read"]), pytest.raises(ToolCallError) as exc:
            await mcp.respond("confirm_appointment", {"appointment_id": appointment.id}, question)
        assert "INSUFFICIENT_SCOPE" in exc.value.text_of


class TestTheOrderOfTheChecks:
    """Authorisation runs before anything else the tool might complain about.

    A caller with no permission should be told that, not something about their
    client's capabilities: the first refusal a request meets should be the one
    that is actually their problem, and it is the one that must be audited.
    """

    async def test_without_the_scope_the_scope_error_wins_not_the_client_one(
        self, mcp_without_elicitation: MCPTestClient, scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            message = await error_from(
                mcp_without_elicitation,
                "cancel_appointment",
                {"appointment_id": 1, "reason": "prueba manual"},
            )
        assert "INSUFFICIENT_SCOPE" in message
        assert "CLIENT_CANNOT_CONFIRM" not in message

    async def test_without_a_token_the_authentication_error_wins(
        self, mcp_without_elicitation: MCPTestClient, scenario: Scenario
    ) -> None:
        message = await error_from(
            mcp_without_elicitation,
            "cancel_appointment",
            {"appointment_id": 1, "reason": "prueba manual"},
        )
        assert "NOT_AUTHENTICATED" in message
        assert "CLIENT_CANNOT_CONFIRM" not in message

    async def test_a_scope_denial_is_audited_even_when_the_client_cannot_confirm(
        self, mcp_without_elicitation: MCPTestClient, ctx: Any, scenario: Scenario
    ) -> None:
        """Checking the capability first would have skipped the audit entirely."""
        with as_caller(SUBJECT, ["read"]):
            await error_from(
                mcp_without_elicitation,
                "cancel_appointment",
                {"appointment_id": 1, "reason": "prueba manual"},
            )
        evento = ctx.auditor.events[-1]
        assert evento["result"] == "error"
        assert evento["error_code"] == "INSUFFICIENT_SCOPE"

    async def test_with_the_right_scope_it_does_warn_about_the_client(
        self, mcp_without_elicitation: MCPTestClient, scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read", "write"]):
            message = await error_from(
                mcp_without_elicitation,
                "cancel_appointment",
                {"appointment_id": 1, "reason": "prueba manual"},
            )
        assert "CLIENT_CANNOT_CONFIRM" in message
