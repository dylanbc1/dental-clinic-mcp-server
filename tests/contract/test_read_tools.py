"""Read tools, exercised end to end: MCP → REST → domain → PostgreSQL."""

from __future__ import annotations

from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer

from tests.conftest import SUBJECT, Scenario, as_caller, error_from, payload

pytestmark = pytest.mark.integration


class TestSearchPatient:
    async def test_finds_by_document(self, server_: MCPServer[Any], scenario: Scenario) -> None:
        with as_caller(SUBJECT, ["read"]):
            result = await server_.call_tool("search_patients", {"document_number": "11111111"})
        found = payload(result)
        assert [p["id"] for p in found] == [scenario.ana_id]

    async def test_finds_by_partial_name(self, server_: MCPServer[Any], scenario: Scenario) -> None:
        with as_caller(SUBJECT, ["read"]):
            result = await server_.call_tool("search_patients", {"name": "bruno"})
        assert payload(result)[0]["regimen"] == "subsidiado"

    async def test_with_no_criterion_it_returns_an_actionable_error(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            message = await error_from(server_, "search_patients", {})
        assert "PATIENT_NOT_FOUND" in message
        assert "Suggestion:" in message

    async def test_a_limit_out_of_range_is_refused_by_the_schema(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        """Typed input validation is layer 4's cheapest half: the bad call never
        reaches the domain."""
        with as_caller(SUBJECT, ["read"]):
            message = await error_from(
                server_, "search_patients", {"document_number": "11111111", "limit": 500}
            )
        assert "limit" in message


class TestAvailability:
    async def test_returns_slots_with_local_time(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            result = await server_.call_tool("check_availability", {})
        slots = payload(result)
        assert slots
        assert slots[0]["start_local"].startswith(str(scenario.future_date))

    async def test_filters_by_specialty(self, server_: MCPServer[Any], scenario: Scenario) -> None:
        with as_caller(SUBJECT, ["read"]):
            result = await server_.call_tool("check_availability", {"specialty": "orthodontics"})
        assert all(c["specialty"] == "orthodontics" for c in payload(result))

    async def test_an_invented_specialty_gives_a_structured_error(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            message = await error_from(
                server_, "check_availability", {"specialty": "astrologia_dental"}
            )
        assert "INVALID_INPUT" in message
        assert "specialty" in message


class TestCarteraAndAffiliation:
    async def test_cartera_al_dia(self, server_: MCPServer[Any], scenario: Scenario) -> None:
        with as_caller(SUBJECT, ["read"]):
            result = await server_.call_tool("check_cartera", {"patient_id": scenario.ana_id})
        assert payload(result)["status"] == "al_dia"

    async def test_cartera_en_mora_reports_the_detail(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            result = await server_.call_tool("check_cartera", {"patient_id": scenario.debtor_id})
        body = payload(result)
        assert body["status"] == "en_mora"
        assert body["above_alert_threshold"] is True

    async def test_inactive_affiliation_explains_the_consequence(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            result = await server_.call_tool(
                "validate_affiliation", {"patient_id": scenario.bruno_id}
            )
        body = payload(result)
        assert body["effective_regimen"] == "particular"
        assert body["blocks_booking"] is False

    async def test_a_nonexistent_patient_gives_a_translated_404(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            message = await error_from(server_, "check_cartera", {"patient_id": 999999})
        assert "PATIENT_NOT_FOUND" in message


class TestAppointments:
    async def test_reading_a_nonexistent_appointment(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            message = await error_from(server_, "get_appointment", {"appointment_id": 999999})
        assert "APPOINTMENT_NOT_FOUND" in message

    async def test_listing_appointments_for_a_patient_with_none(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            result = await server_.call_tool(
                "list_patient_appointments", {"patient_id": scenario.ana_id}
            )
        assert payload(result) == []


class TestReadAudit:
    async def test_every_read_is_recorded(
        self, server_: MCPServer[Any], ctx: Any, scenario: Scenario
    ) -> None:
        with as_caller("auditor@clinica.test", ["read"]):
            await server_.call_tool("search_patients", {"document_number": "11111111"})
        event = ctx.auditor.events[-1]
        assert event["event"] == "tool.invocation"
        assert event["tool"] == "search_patients"
        assert event["subject"] == "auditor@clinica.test"
        assert event["result"] == "ok"

    async def test_failed_reads_are_recorded_too(
        self, server_: MCPServer[Any], ctx: Any, scenario: Scenario
    ) -> None:
        """A log that only records successes cannot tell you an agent spent an
        hour failing."""
        with as_caller(SUBJECT, ["read"]):
            await error_from(server_, "get_appointment", {"appointment_id": 999999})
        event = ctx.auditor.events[-1]
        assert event["result"] == "error"
        assert event["error_code"] == "APPOINTMENT_NOT_FOUND"

    async def test_the_document_is_not_copied_into_the_log(
        self, server_: MCPServer[Any], ctx: Any, scenario: Scenario
    ) -> None:
        """An audit log is not an excuse to duplicate identifiers somewhere less
        protected."""
        with as_caller(SUBJECT, ["read"]):
            await server_.call_tool("search_patients", {"document_number": "11111111"})
        event = ctx.auditor.events[-1]
        assert event["arguments"]["document_number"] == "«redacted»"
        assert "11111111" not in str(event)
