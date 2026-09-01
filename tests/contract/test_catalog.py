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

TOOLS_LECTURA = {
    "buscar_paciente",
    "consultar_disponibilidad",
    "consultar_cita",
    "listar_citas_paciente",
    "consultar_cartera",
    "validar_afiliacion",
}
TOOLS_ESCRITURA = {
    "agendar_cita",
    "confirmar_cita",
    "cancelar_cita",
    "reprogramar_cita",
    "registrar_asistencia",
    "ofrecer_cupo_lista_espera",
}
TOOLS_CLINICAS = {"registrar_motivo_consulta"}
#: Tools that pause for a human's answer over MRTR.
TOOLS_CON_CONFIRMACION = TOOLS_ESCRITURA | TOOLS_CLINICAS
TODAS = TOOLS_LECTURA | TOOLS_ESCRITURA | TOOLS_CLINICAS


class TestCatalogo:
    async def test_expone_exactamente_el_catalogo_previsto(self, server_: MCPServer[Any]) -> None:
        assert {t.name for t in await server_.list_tools()} == TODAS

    async def test_no_supera_el_limite_de_15_tools(self, server_: MCPServer[Any]) -> None:
        """Model accuracy degrades past ~25-30 tools. Thirteen precise ones beat
        thirty mediocre ones, and the ceiling is a design constraint, not luck."""
        tools = await server_.list_tools()
        assert len(tools) == 13
        assert len(tools) <= 15

    async def test_toda_tool_tiene_titulo_y_descripcion(self, server_: MCPServer[Any]) -> None:
        for tool in await server_.list_tools():
            assert tool.title, f"{tool.name} sin título"
            assert tool.description and len(tool.description) > 80, (
                f"{tool.name} tiene una descripción demasiado escueta para que el "
                "modelo sepa cuándo usarla"
            )

    async def test_toda_tool_declara_su_esquema_de_entrada(self, server_: MCPServer[Any]) -> None:
        for tool in await server_.list_tools():
            esquema = tool.input_schema
            assert esquema["type"] == "object"
            assert "properties" in esquema

    async def test_las_tools_con_gate_lo_anuncian(self, server_: MCPServer[Any]) -> None:
        """A model that thinks it already booked the appointment will tell the
        patient it did. The description has to prevent that."""
        por_nombre = {t.name: t for t in await server_.list_tools()}
        for nombre in TOOLS_CON_CONFIRMACION:
            descripcion = (por_nombre[nombre].description or "").lower()
            assert "confirmation" in descripcion, f"{nombre} no anuncia el gate"

    async def test_el_parametro_de_confirmacion_no_se_expone_al_modelo(
        self, server_: MCPServer[Any]
    ) -> None:
        """The confirmation is filled by the client over MRTR, not by the model.

        If it appeared in the schema the model could supply it itself, which
        would turn human approval into a field the agent fills in.
        """
        for tool in await server_.list_tools():
            propiedades = tool.input_schema.get("properties", {})
            assert "confirmacion" not in propiedades, tool.name

    async def test_ninguna_tool_pide_un_token_de_confirmacion(
        self, server_: MCPServer[Any]
    ) -> None:
        """The paused operation rides `requestState`, sealed by the SDK. No tool
        should be asking the model to carry an approval by hand."""
        names = {t.name for t in await server_.list_tools()}
        assert "confirmar_operacion" not in names
        for tool in await server_.list_tools():
            assert "token_confirmacion" not in tool.input_schema.get("properties", {})

    async def test_la_tool_clinica_advierte_de_la_regulacion(self, server_: MCPServer[Any]) -> None:
        por_nombre = {t.name: t for t in await server_.list_tools()}
        descripcion = por_nombre["registrar_motivo_consulta"].description or ""
        assert "2654" in descripcion
        assert "consent" in descripcion.lower()
        assert "never interpret" in descripcion.lower()

    async def test_las_tools_de_lectura_no_anuncian_confirmacion(
        self, server_: MCPServer[Any]
    ) -> None:
        por_nombre = {t.name: t for t in await server_.list_tools()}
        for nombre in TOOLS_LECTURA:
            assert "confirmation" not in (por_nombre[nombre].description or "").lower()

    async def test_la_regla_de_no_bloqueo_por_mora_esta_en_la_descripcion(
        self, server_: MCPServer[Any]
    ) -> None:
        """The rule most likely to be got wrong is stated where the model reads it."""
        por_nombre = {t.name: t for t in await server_.list_tools()}
        cartera = (por_nombre["consultar_cartera"].description or "").lower()
        assert "does not prevent" in cartera.lower()

    async def test_los_parametros_obligatorios_estan_marcados(
        self, server_: MCPServer[Any]
    ) -> None:
        por_nombre = {t.name: t for t in await server_.list_tools()}
        assert "motivo" in por_nombre["cancelar_cita"].input_schema["required"]
        assert "cita_id" in por_nombre["cancelar_cita"].input_schema["required"]
        assert "confirmacion" not in por_nombre["cancelar_cita"].input_schema["required"]

    async def test_ningun_parametro_opcional_es_obligatorio(self, server_: MCPServer[Any]) -> None:
        por_nombre = {t.name: t for t in await server_.list_tools()}
        assert "documento" not in por_nombre["buscar_paciente"].input_schema.get("required", [])
        assert "nombre" not in por_nombre["buscar_paciente"].input_schema.get("required", [])

    async def test_las_instrucciones_del_servidor_explican_el_gate(
        self, server_: MCPServer[Any]
    ) -> None:
        instrucciones = (server_.instructions or "").lower()
        assert "confirmación" in instrucciones
        assert "retries the same call" in instrucciones
        assert "clinical" in instrucciones
