"""Resources and the receptionist prompt.

Resources exist so the model reads the clinic's *actual* policy instead of
inventing one. These tests check that the policy is really there and really
comes from the database.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer

from backend.domain.services import book_appointment
from backend.domain.time import now_at_clinic
from backend.models import AgendaSlot
from tests.conftest import SUBJECT, Scenario, as_caller

pytestmark = pytest.mark.integration


async def leer(server_: MCPServer[Any], uri: str) -> Any:
    contenidos = list(await server_.read_resource(uri))
    assert contenidos, f"{uri} returned no content"
    return json.loads(contenidos[0].content)


class TestResources:
    async def test_the_three_resources_are_declared(self, server_: MCPServer[Any]) -> None:
        uris = {str(r.uri) for r in await server_.list_resources()}
        assert uris == {"clinica://info", "politicas://cartera", "agenda://hoy"}

    async def test_every_resource_has_a_name_and_a_description(
        self, server_: MCPServer[Any]
    ) -> None:
        for recurso in await server_.list_resources():
            assert recurso.name
            assert recurso.description and len(recurso.description) > 40
            assert recurso.mime_type == "application/json"

    async def test_clinic_info_carries_the_real_professionals(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            info = await leer(server_, "clinica://info")
        assert info["name"] == "Clínica Escenario"
        assert info["timezone_name"] == "America/Bogota"
        assert {p["specialty"] for p in info["professionals"]} == {
            "general_dentistry",
            "orthodontics",
        }

    async def test_policies_carry_the_tariffs_so_nobody_invents_them(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            politicas = await leer(server_, "politicas://cartera")
        assert politicas["particular_tariffs"]["endodontics"] == "350000"
        assert politicas["no_show_amount"] == "40000"
        assert "never a block" in politicas["note"]

    async def test_todays_agenda_answers_even_with_no_appointments(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            agenda = await leer(server_, "agenda://hoy")
        assert agenda["total"] == 0
        assert agenda["appointments"] == []

    async def test_todays_agenda_reflects_the_days_appointments(
        self, server_: MCPServer[Any], backend_session: Any, scenario: Scenario
    ) -> None:
        # "Today" means today at the clinic, not on whatever machine is running
        # this. Using the system date passes in Bogotá and fails on a UTC runner
        # for the five hours a day the two disagree.
        slot = backend_session.get(AgendaSlot, scenario.slots_general[0])
        slot.day = now_at_clinic().date()
        backend_session.commit()
        book_appointment(
            backend_session,
            patient_id=scenario.ana_id,
            slot_id=slot.id,
            user="setup",
        )
        backend_session.commit()

        with as_caller(SUBJECT, ["read"]):
            agenda = await leer(server_, "agenda://hoy")
        assert agenda["day"] == now_at_clinic().date().isoformat()
        assert agenda["total"] == 1
        assert agenda["by_status"] == {"scheduled": 1}

    async def test_today_is_today_at_the_clinic_not_on_the_server(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        """America/Bogota is UTC-5, so for five hours a day the two disagree.
        A server that answered with its own date would show the wrong agenda
        every evening."""
        with as_caller(SUBJECT, ["read"]):
            agenda = await leer(server_, "agenda://hoy")
        assert agenda["day"] == now_at_clinic().date().isoformat()


class TestPrompt:
    async def test_the_prompt_is_declared(self, server_: MCPServer[Any]) -> None:
        prompts = await server_.list_prompts()
        assert [p.name for p in prompts] == ["recepcionista_odontologia"]

    async def test_it_is_personalised_with_the_clinics_data(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            result = await server_.get_prompt("recepcionista_odontologia")
        text_of = "\n".join(getattr(m.content, "text", "") for m in result.messages)
        assert "Clínica Escenario" in text_of
        assert "Bogotá" in text_of

    @pytest.mark.parametrize(
        "regla",
        [
            "NUNCA le digas al paciente",
            "No das consejo clínico",
            "No niegas una cita por deuda",
            "No niegas atención por afiliación inactiva",
            "No inventas precios",
            "No registras motivo de consulta sin consentimiento",
        ],
    )
    async def test_the_prompt_states_the_agents_limits(
        self, server_: MCPServer[Any], scenario: Scenario, regla: str
    ) -> None:
        """Every rule the domain enforces is also stated in the prompt, so the
        agent does not have to discover them by hitting errors."""
        with as_caller(SUBJECT, ["read"]):
            result = await server_.get_prompt("recepcionista_odontologia")
        text_of = "\n".join(getattr(m.content, "text", "") for m in result.messages)
        assert regla in text_of

    async def test_the_prompt_says_when_to_escalate(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            result = await server_.get_prompt("recepcionista_odontologia")
        text_of = "\n".join(getattr(m.content, "text", "") for m in result.messages)
        assert "urgencia" in text_of.lower()
        assert "escala" in text_of.lower()
