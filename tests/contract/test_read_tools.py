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
            result = await server_.call_tool("search_patients", {"document_number": "11111111"})
        encontrados = payload(result)
        assert [p["id"] for p in encontrados] == [scenario.ana_id]

    async def test_encuentra_por_nombre_parcial(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            result = await server_.call_tool("search_patients", {"name": "bruno"})
        assert payload(result)[0]["regimen"] == "subsidiado"

    async def test_sin_criterio_devuelve_un_error_accionable(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            message = await error_from(server_, "search_patients", {})
        assert "PATIENT_NOT_FOUND" in message
        assert "Suggestion:" in message

    async def test_un_limite_fuera_de_rango_lo_rechaza_el_esquema(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        """Typed input validation is layer 4's cheapest half: the bad call never
        reaches the domain."""
        with as_caller(SUBJECT, ["read"]):
            message = await error_from(
                server_, "search_patients", {"document_number": "11111111", "limit": 500}
            )
        assert "limit" in message


class TestDisponibilidad:
    async def test_devuelve_cupos_con_hora_local(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            result = await server_.call_tool("check_availability", {})
        cupos = payload(result)
        assert cupos
        assert cupos[0]["start_local"].startswith(str(scenario.fecha_futura))

    async def test_filtra_por_especialidad(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            result = await server_.call_tool("check_availability", {"specialty": "orthodontics"})
        assert all(c["specialty"] == "orthodontics" for c in payload(result))

    async def test_una_especialidad_inventada_da_error_estructurado(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            message = await error_from(
                server_, "check_availability", {"specialty": "astrologia_dental"}
            )
        assert "INVALID_INPUT" in message
        assert "specialty" in message


class TestCarteraYAfiliacion:
    async def test_cartera_al_dia(self, server_: MCPServer[Any], scenario: Scenario) -> None:
        with as_caller(SUBJECT, ["read"]):
            result = await server_.call_tool("check_cartera", {"patient_id": scenario.ana_id})
        assert payload(result)["status"] == "al_dia"

    async def test_cartera_en_mora_reporta_el_detalle(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            result = await server_.call_tool("check_cartera", {"patient_id": scenario.deudor_id})
        body = payload(result)
        assert body["status"] == "en_mora"
        assert body["above_alert_threshold"] is True

    async def test_afiliacion_inactiva_explica_la_consecuencia(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            result = await server_.call_tool(
                "validate_afiliacion", {"patient_id": scenario.bruno_id}
            )
        body = payload(result)
        assert body["effective_regimen"] == "particular"
        assert body["blocks_booking"] is False

    async def test_un_paciente_inexistente_da_404_traducido(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            message = await error_from(server_, "check_cartera", {"patient_id": 999999})
        assert "PATIENT_NOT_FOUND" in message


class TestCitas:
    async def test_consultar_una_cita_inexistente(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            message = await error_from(server_, "get_appointment", {"appointment_id": 999999})
        assert "APPOINTMENT_NOT_FOUND" in message

    async def test_listar_citas_de_un_paciente_sin_citas(
        self, server_: MCPServer[Any], scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            result = await server_.call_tool(
                "list_patient_appointments", {"patient_id": scenario.ana_id}
            )
        assert payload(result) == []


class TestAuditoriaDeLectura:
    async def test_toda_lectura_queda_registrada(
        self, server_: MCPServer[Any], ctx: Any, scenario: Scenario
    ) -> None:
        with as_caller("auditor@clinica.test", ["read"]):
            await server_.call_tool("search_patients", {"document_number": "11111111"})
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
            await error_from(server_, "get_appointment", {"appointment_id": 999999})
        evento = ctx.auditor.events[-1]
        assert evento["result"] == "error"
        assert evento["error_code"] == "APPOINTMENT_NOT_FOUND"

    async def test_el_documento_no_se_copia_al_log(
        self, server_: MCPServer[Any], ctx: Any, scenario: Scenario
    ) -> None:
        """An audit log is not an excuse to duplicate identifiers somewhere less
        protected."""
        with as_caller(SUBJECT, ["read"]):
            await server_.call_tool("search_patients", {"document_number": "11111111"})
        evento = ctx.auditor.events[-1]
        assert evento["arguments"]["document_number"] == "«redacted»"
        assert "11111111" not in str(evento)
