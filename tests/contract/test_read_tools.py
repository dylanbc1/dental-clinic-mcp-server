"""Read tools, exercised end to end: MCP → REST → domain → PostgreSQL."""

from __future__ import annotations

from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer

from tests.conftest import SUBJECT, Scenario, as_caller, error_from, payload

pytestmark = pytest.mark.integration


class TestBuscarPaciente:
    async def test_encuentra_por_documento(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            result = await server_.call_tool("search_patients", {"documento": "11111111"})
        encontrados = payload(result)
        assert [p["id"] for p in encontrados] == [scenario.ana_id]

    async def test_encuentra_por_nombre_parcial(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            result = await server_.call_tool("search_patients", {"nombre": "bruno"})
        assert payload(result)[0]["regimen"] == "subsidiado"

    async def test_sin_criterio_devuelve_un_error_accionable(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            mensaje = await error_from(server_, "search_patients", {})
        assert "PACIENTE_NO_ENCONTRADO" in mensaje
        assert "Suggestion:" in mensaje

    async def test_un_limite_fuera_de_rango_lo_rechaza_el_esquema(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        """Typed input validation is layer 4's cheapest half: the bad call never
        reaches the domain."""
        with as_caller(SUBJECT, ["read"]):
            mensaje = await error_from(
                server_, "search_patients", {"documento": "11111111", "limite": 500}
            )
        assert "limite" in mensaje


class TestDisponibilidad:
    async def test_devuelve_cupos_con_hora_local(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            result = await server_.call_tool("check_availability", {})
        cupos = payload(result)
        assert cupos
        assert cupos[0]["inicio_local"].startswith(str(scenario.fecha_futura))

    async def test_filtra_por_especialidad(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            result = await server_.call_tool("check_availability", {"especialidad": "orthodontics"})
        assert all(c["especialidad"] == "orthodontics" for c in payload(result))

    async def test_una_especialidad_inventada_da_error_estructurado(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            mensaje = await error_from(
                server_, "check_availability", {"especialidad": "astrologia_dental"}
            )
        assert "ENTRADA_INVALIDA" in mensaje
        assert "especialidad" in mensaje


class TestCarteraYAfiliacion:
    async def test_cartera_al_dia(self, server_: MCPServer[Any], scenario: Scenario) -> None:
        with as_caller(SUBJECT, ["read"]):
            result = await server_.call_tool("check_cartera", {"paciente_id": scenario.ana_id})
        assert payload(result)["estado"] == "al_dia"

    async def test_cartera_en_mora_reporta_el_detalle(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            result = await server_.call_tool("check_cartera", {"paciente_id": scenario.deudor_id})
        body = payload(result)
        assert body["estado"] == "en_mora"
        assert body["supera_umbral_alerta"] is True

    async def test_afiliacion_inactiva_explica_la_consecuencia(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            result = await server_.call_tool(
                "validate_afiliacion", {"paciente_id": scenario.bruno_id}
            )
        body = payload(result)
        assert body["regimen_efectivo"] == "particular"
        assert body["bloquea_agendamiento"] is False

    async def test_un_paciente_inexistente_da_404_traducido(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            mensaje = await error_from(server_, "check_cartera", {"paciente_id": 999999})
        assert "PACIENTE_NO_ENCONTRADO" in mensaje


class TestCitas:
    async def test_consultar_una_cita_inexistente(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            mensaje = await error_from(server_, "get_appointment", {"cita_id": 999999})
        assert "CITA_NO_ENCONTRADA" in mensaje

    async def test_listar_citas_de_un_paciente_sin_citas(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            result = await server_.call_tool(
                "list_patient_appointments", {"paciente_id": scenario.ana_id}
            )
        assert payload(result) == []


class TestAuditoriaDeLectura:
    async def test_toda_lectura_queda_registrada(
        self, server_: MCPServer[Any], ctx: Any, scenario: Scenario
    ) -> None:
        with as_caller("auditor@clinica.test", ["read"]):
            await server_.call_tool("search_patients", {"documento": "11111111"})
        evento = ctx.auditor.events[-1]
        assert evento["event"] == "tool.invocation"
        assert evento["tool"] == "search_patients"
        assert evento["subject"] == "auditor@clinica.test"
        assert evento["result"] == "ok"

    async def test_las_lecturas_fallidas_tambien_se_registran(
        self, server_: MCPServer[Any], ctx: Any, scenario: Scenario
    ) -> None:
        """A log that only records successes cannot tell you an agent spent an
        hour failing."""
        with as_caller(SUBJECT, ["read"]):
            await error_from(server_, "get_appointment", {"cita_id": 999999})
        evento = ctx.auditor.events[-1]
        assert evento["result"] == "error"
        assert evento["error_code"] == "CITA_NO_ENCONTRADA"

    async def test_el_documento_no_se_copia_al_log(
        self, server_: MCPServer[Any], ctx: Any, scenario: Scenario
    ) -> None:
        """An audit log is not an excuse to duplicate identifiers somewhere less
        protected."""
        with as_caller(SUBJECT, ["read"]):
            await server_.call_tool("search_patients", {"documento": "11111111"})
        evento = ctx.auditor.events[-1]
        assert evento["arguments"]["documento"] == "«redacted»"
        assert "11111111" not in str(evento)
