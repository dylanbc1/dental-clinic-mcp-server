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
    assert contenidos, f"{uri} no devolvió contenido"
    return json.loads(contenidos[0].content)


class TestRecursos:
    async def test_los_tres_recursos_estan_declarados(self, server_: MCPServer[Any]) -> None:
        uris = {str(r.uri) for r in await server_.list_resources()}
        assert uris == {"clinica://info", "politicas://cartera", "agenda://hoy"}

    async def test_todo_recurso_tiene_nombre_y_descripcion(self, server_: MCPServer[Any]) -> None:
        for recurso in await server_.list_resources():
            assert recurso.name
            assert recurso.description and len(recurso.description) > 40
            assert recurso.mime_type == "application/json"

    async def test_clinica_info_trae_los_profesionales_reales(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            info = await leer(server_, "clinica://info")
        assert info["nombre"] == "Clínica Escenario"
        assert info["zona_horaria"] == "America/Bogota"
        assert {p["especialidad"] for p in info["profesionales"]} == {
            "odontologia_general",
            "ortodoncia",
        }

    async def test_politicas_trae_las_tarifas_para_no_inventarlas(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            politicas = await leer(server_, "politicas://cartera")
        assert politicas["tarifas_particular"]["endodoncia"] == "350000"
        assert politicas["monto_no_show"] == "40000"
        assert "never a block" in politicas["nota"]

    async def test_agenda_hoy_responde_aunque_no_haya_citas(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            agenda = await leer(server_, "agenda://hoy")
        assert agenda["total"] == 0
        assert agenda["citas"] == []

    async def test_agenda_hoy_refleja_las_citas_del_dia(
        self, server_: MCPServer[Any], backend_session: Any, scenario: Scenario
    ) -> None:
        # "Today" means today at the clinic, not on whatever machine is running
        # this. Using the system date passes in Bogotá and fails on a UTC runner
        # for the five hours a day the two disagree.
        slot = backend_session.get(AgendaSlot, scenario.slots_general[0])
        slot.fecha = now_at_clinic().date()
        backend_session.commit()
        book_appointment(
            backend_session,
            paciente_id=scenario.ana_id,
            slot_id=slot.id,
            usuario="setup",
        )
        backend_session.commit()

        with as_caller(SUBJECT, ["read"]):
            agenda = await leer(server_, "agenda://hoy")
        assert agenda["fecha"] == now_at_clinic().date().isoformat()
        assert agenda["total"] == 1
        assert agenda["por_estado"] == {"agendada": 1}

    async def test_hoy_es_hoy_en_la_clinica_no_en_el_servidor(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        """America/Bogota is UTC-5, so for five hours a day the two disagree.
        A server that answered with its own date would show the wrong agenda
        every evening."""
        with as_caller(SUBJECT, ["read"]):
            agenda = await leer(server_, "agenda://hoy")
        assert agenda["fecha"] == now_at_clinic().date().isoformat()


class TestPrompt:
    async def test_el_prompt_esta_declarado(self, server_: MCPServer[Any]) -> None:
        prompts = await server_.list_prompts()
        assert [p.name for p in prompts] == ["recepcionista_odontologia"]

    async def test_se_personaliza_con_los_datos_de_la_clinica(
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
    async def test_el_prompt_declara_los_limites_del_agente(
        self, server_: MCPServer[Any], scenario: Scenario, regla: str
    ) -> None:
        """Every rule the domain enforces is also stated in the prompt, so the
        agent does not have to discover them by hitting errors."""
        with as_caller(SUBJECT, ["read"]):
            result = await server_.get_prompt("recepcionista_odontologia")
        text_of = "\n".join(getattr(m.content, "text", "") for m in result.messages)
        assert regla in text_of

    async def test_el_prompt_dice_cuando_escalar(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            result = await server_.get_prompt("recepcionista_odontologia")
        text_of = "\n".join(getattr(m.content, "text", "") for m in result.messages)
        assert "urgencia" in text_of.lower()
        assert "escala" in text_of.lower()
