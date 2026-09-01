"""The shape of the MCP surface itself.

What a client sees before it calls anything: how many tools there are, what they
are named, and whether their descriptions actually tell the model how to use
them. A tool with a vague description is a tool that gets called wrongly, which
is a correctness problem, not a documentation one.
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer

pytestmark = pytest.mark.integration

READ_TOOLS = {
    "search_patients",
    "check_availability",
    "get_appointment",
    "list_patient_appointments",
    "check_cartera",
    "validate_affiliation",
}
WRITE_TOOLS = {
    "book_appointment",
    "confirm_appointment",
    "cancel_appointment",
    "reschedule_appointment",
    "record_attendance",
    "offer_slot_to_waiting_list",
}
CLINICAL_TOOLS = {"record_visit_reason"}
#: Tools that pause for a human's answer over MRTR.
TOOLS_WITH_CONFIRMATION = WRITE_TOOLS | CLINICAL_TOOLS
TODAS = READ_TOOLS | WRITE_TOOLS | CLINICAL_TOOLS


class TestCatalogue:
    async def test_exposes_exactly_the_intended_catalogue(self, server_: MCPServer[Any]) -> None:
        assert {t.name for t in await server_.list_tools()} == TODAS

    async def test_it_stays_under_the_limit_of_15_tools(self, server_: MCPServer[Any]) -> None:
        """Model accuracy degrades past ~25-30 tools. Thirteen precise ones beat
        thirty mediocre ones, and the ceiling is a design constraint, not luck."""
        tools = await server_.list_tools()
        assert len(tools) == 13
        assert len(tools) <= 15

    async def test_every_tool_has_a_title_and_a_description(self, server_: MCPServer[Any]) -> None:
        for tool in await server_.list_tools():
            assert tool.title, f"{tool.name} has no title"
            assert tool.description and len(tool.description) > 80, (
                f"{tool.name} tiene una descripción demasiado escueta para que el "
                "modelo sepa cuándo usarla"
            )

    async def test_every_tool_declares_its_input_schema(self, server_: MCPServer[Any]) -> None:
        for tool in await server_.list_tools():
            schema = tool.input_schema
            assert schema["type"] == "object"
            assert "properties" in schema

    async def test_gated_tools_announce_it(self, server_: MCPServer[Any]) -> None:
        """A model that thinks it already booked the appointment will tell the
        patient it did. The description has to prevent that."""
        by_name = {t.name: t for t in await server_.list_tools()}
        for name in TOOLS_WITH_CONFIRMATION:
            description = (by_name[name].description or "").lower()
            assert "confirmation" in description, f"{name} does not announce the gate"

    async def test_the_confirmation_parameter_is_not_exposed_to_the_model(
        self, server_: MCPServer[Any]
    ) -> None:
        """The confirmation is filled by the client over MRTR, not by the model.

        If it appeared in the schema the model could supply it itself, which
        would turn human approval into a field the agent fills in.
        """
        for tool in await server_.list_tools():
            properties = tool.input_schema.get("properties", {})
            assert "confirmation" not in properties, tool.name

    async def test_no_tool_asks_for_a_confirmation_token(self, server_: MCPServer[Any]) -> None:
        """The paused operation rides `requestState`, sealed by the SDK. No tool
        should be asking the model to carry an approval by hand."""
        names = {t.name for t in await server_.list_tools()}
        assert "confirm_operation" not in names
        for tool in await server_.list_tools():
            assert "confirmation_token" not in tool.input_schema.get("properties", {})

    async def test_the_clinical_tool_warns_about_the_regulation(
        self, server_: MCPServer[Any]
    ) -> None:
        by_name = {t.name: t for t in await server_.list_tools()}
        description = by_name["record_visit_reason"].description or ""
        assert "2654" in description
        assert "consent" in description.lower()
        assert "never interpret" in description.lower()

    async def test_read_tools_do_not_announce_confirmation(self, server_: MCPServer[Any]) -> None:
        by_name = {t.name: t for t in await server_.list_tools()}
        for name in READ_TOOLS:
            assert "confirmation" not in (by_name[name].description or "").lower()

    async def test_the_mora_does_not_block_rule_is_in_the_description(
        self, server_: MCPServer[Any]
    ) -> None:
        """The rule most likely to be got wrong is stated where the model reads it."""
        by_name = {t.name: t for t in await server_.list_tools()}
        cartera = (by_name["check_cartera"].description or "").lower()
        assert "does not prevent" in cartera.lower()

    async def test_the_required_parameters_are_marked(self, server_: MCPServer[Any]) -> None:
        by_name = {t.name: t for t in await server_.list_tools()}
        assert "reason" in by_name["cancel_appointment"].input_schema["required"]
        assert "appointment_id" in by_name["cancel_appointment"].input_schema["required"]
        assert "confirmation" not in by_name["cancel_appointment"].input_schema["required"]

    async def test_no_optional_parameter_is_required(self, server_: MCPServer[Any]) -> None:
        by_name = {t.name: t for t in await server_.list_tools()}
        assert "document_number" not in by_name["search_patients"].input_schema.get("required", [])
        assert "name" not in by_name["search_patients"].input_schema.get("required", [])

    async def test_the_server_instructions_explain_the_gate(self, server_: MCPServer[Any]) -> None:
        instructions = (server_.instructions or "").lower()
        assert "confirmación" in instructions
        assert "retries the same call" in instructions
        assert "clinical" in instructions
