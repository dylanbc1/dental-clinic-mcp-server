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

from backend.domain.servicios import agendar_cita
from tests.conftest import SUJETO, Escenario, como

pytestmark = pytest.mark.integration


async def leer(servidor: MCPServer[Any], uri: str) -> Any:
    contenidos = list(await servidor.read_resource(uri))
    assert contenidos, f"{uri} no devolvió contenido"
    return json.loads(contenidos[0].content)


class TestRecursos:
    async def test_los_tres_recursos_estan_declarados(self, servidor: MCPServer[Any]) -> None:
        uris = {str(r.uri) for r in await servidor.list_resources()}
        assert uris == {"clinica://info", "politicas://cartera", "agenda://hoy"}

    async def test_todo_recurso_tiene_nombre_y_descripcion(self, servidor: MCPServer[Any]) -> None:
        for recurso in await servidor.list_resources():
            assert recurso.name
            assert recurso.description and len(recurso.description) > 40
            assert recurso.mime_type == "application/json"

    async def test_clinica_info_trae_los_profesionales_reales(
        self, servidor: MCPServer[Any], escenario: Escenario
    ) -> None:
        with como(SUJETO, ["read"]):
            info = await leer(servidor, "clinica://info")
        assert info["nombre"] == "Clínica Escenario"
        assert info["zona_horaria"] == "America/Bogota"
        assert {p["especialidad"] for p in info["profesionales"]} == {
            "odontologia_general",
            "ortodoncia",
        }

    async def test_politicas_trae_las_tarifas_para_no_inventarlas(
        self, servidor: MCPServer[Any], escenario: Escenario
    ) -> None:
        with como(SUJETO, ["read"]):
            politicas = await leer(servidor, "politicas://cartera")
        assert politicas["tarifas_particular"]["endodoncia"] == "350000"
        assert politicas["monto_no_show"] == "40000"
        assert "nunca bloqueo" in politicas["nota"]

    async def test_agenda_hoy_responde_aunque_no_haya_citas(
        self, servidor: MCPServer[Any], escenario: Escenario
    ) -> None:
        with como(SUJETO, ["read"]):
            agenda = await leer(servidor, "agenda://hoy")
        assert agenda["total"] == 0
        assert agenda["citas"] == []

    async def test_agenda_hoy_refleja_las_citas_del_dia(
        self, servidor: MCPServer[Any], sesion_backend: Any, escenario: Escenario
    ) -> None:
        # Move a slot to today so the resource has something to report.
        from datetime import date

        slot = sesion_backend.get(
            __import__("backend.models", fromlist=["AgendaSlot"]).AgendaSlot,
            escenario.slots_general[0],
        )
        slot.fecha = date.today()
        sesion_backend.commit()
        agendar_cita(
            sesion_backend,
            paciente_id=escenario.ana_id,
            slot_id=slot.id,
            usuario="setup",
        )
        sesion_backend.commit()

        with como(SUJETO, ["read"]):
            agenda = await leer(servidor, "agenda://hoy")
        assert agenda["total"] == 1
        assert agenda["por_estado"] == {"agendada": 1}


class TestPrompt:
    async def test_el_prompt_esta_declarado(self, servidor: MCPServer[Any]) -> None:
        prompts = await servidor.list_prompts()
        assert [p.name for p in prompts] == ["recepcionista_odontologia"]

    async def test_se_personaliza_con_los_datos_de_la_clinica(
        self, servidor: MCPServer[Any], escenario: Escenario
    ) -> None:
        with como(SUJETO, ["read"]):
            resultado = await servidor.get_prompt("recepcionista_odontologia")
        texto = "\n".join(getattr(m.content, "text", "") for m in resultado.messages)
        assert "Clínica Escenario" in texto
        assert "Bogotá" in texto

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
        self, servidor: MCPServer[Any], escenario: Escenario, regla: str
    ) -> None:
        """Every rule the domain enforces is also stated in the prompt, so the
        agent does not have to discover them by hitting errors."""
        with como(SUJETO, ["read"]):
            resultado = await servidor.get_prompt("recepcionista_odontologia")
        texto = "\n".join(getattr(m.content, "text", "") for m in resultado.messages)
        assert regla in texto

    async def test_el_prompt_dice_cuando_escalar(
        self, servidor: MCPServer[Any], escenario: Escenario
    ) -> None:
        with como(SUJETO, ["read"]):
            resultado = await servidor.get_prompt("recepcionista_odontologia")
        texto = "\n".join(getattr(m.content, "text", "") for m in resultado.messages)
        assert "urgencia" in texto.lower()
        assert "escala" in texto.lower()
