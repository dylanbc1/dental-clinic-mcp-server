"""Read tools, exercised end to end: MCP → REST → domain → PostgreSQL."""

from __future__ import annotations

from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer

from tests.conftest import SUJETO, Escenario, como, datos, error_de

pytestmark = pytest.mark.integration


class TestBuscarPaciente:
    async def test_encuentra_por_documento(
        self, servidor: MCPServer[Any], escenario: Escenario
    ) -> None:
        with como(SUJETO, ["read"]):
            resultado = await servidor.call_tool("buscar_paciente", {"documento": "11111111"})
        encontrados = datos(resultado)
        assert [p["id"] for p in encontrados] == [escenario.ana_id]

    async def test_encuentra_por_nombre_parcial(
        self, servidor: MCPServer[Any], escenario: Escenario
    ) -> None:
        with como(SUJETO, ["read"]):
            resultado = await servidor.call_tool("buscar_paciente", {"nombre": "bruno"})
        assert datos(resultado)[0]["regimen"] == "subsidiado"

    async def test_sin_criterio_devuelve_un_error_accionable(
        self, servidor: MCPServer[Any], escenario: Escenario
    ) -> None:
        with como(SUJETO, ["read"]):
            mensaje = await error_de(servidor, "buscar_paciente", {})
        assert "PACIENTE_NO_ENCONTRADO" in mensaje
        assert "Suggestion:" in mensaje

    async def test_un_limite_fuera_de_rango_lo_rechaza_el_esquema(
        self, servidor: MCPServer[Any], escenario: Escenario
    ) -> None:
        """Typed input validation is layer 4's cheapest half: the bad call never
        reaches the domain."""
        with como(SUJETO, ["read"]):
            mensaje = await error_de(
                servidor, "buscar_paciente", {"documento": "11111111", "limite": 500}
            )
        assert "limite" in mensaje


class TestDisponibilidad:
    async def test_devuelve_cupos_con_hora_local(
        self, servidor: MCPServer[Any], escenario: Escenario
    ) -> None:
        with como(SUJETO, ["read"]):
            resultado = await servidor.call_tool("consultar_disponibilidad", {})
        cupos = datos(resultado)
        assert cupos
        assert cupos[0]["inicio_local"].startswith(str(escenario.fecha_futura))

    async def test_filtra_por_especialidad(
        self, servidor: MCPServer[Any], escenario: Escenario
    ) -> None:
        with como(SUJETO, ["read"]):
            resultado = await servidor.call_tool(
                "consultar_disponibilidad", {"especialidad": "ortodoncia"}
            )
        assert all(c["especialidad"] == "ortodoncia" for c in datos(resultado))

    async def test_una_especialidad_inventada_da_error_estructurado(
        self, servidor: MCPServer[Any], escenario: Escenario
    ) -> None:
        with como(SUJETO, ["read"]):
            mensaje = await error_de(
                servidor, "consultar_disponibilidad", {"especialidad": "astrologia_dental"}
            )
        assert "ENTRADA_INVALIDA" in mensaje
        assert "especialidad" in mensaje


class TestCarteraYAfiliacion:
    async def test_cartera_al_dia(self, servidor: MCPServer[Any], escenario: Escenario) -> None:
        with como(SUJETO, ["read"]):
            resultado = await servidor.call_tool(
                "consultar_cartera", {"paciente_id": escenario.ana_id}
            )
        assert datos(resultado)["estado"] == "al_dia"

    async def test_cartera_en_mora_reporta_el_detalle(
        self, servidor: MCPServer[Any], escenario: Escenario
    ) -> None:
        with como(SUJETO, ["read"]):
            resultado = await servidor.call_tool(
                "consultar_cartera", {"paciente_id": escenario.deudor_id}
            )
        cuerpo = datos(resultado)
        assert cuerpo["estado"] == "en_mora"
        assert cuerpo["supera_umbral_alerta"] is True

    async def test_afiliacion_inactiva_explica_la_consecuencia(
        self, servidor: MCPServer[Any], escenario: Escenario
    ) -> None:
        with como(SUJETO, ["read"]):
            resultado = await servidor.call_tool(
                "validar_afiliacion", {"paciente_id": escenario.bruno_id}
            )
        cuerpo = datos(resultado)
        assert cuerpo["regimen_efectivo"] == "particular"
        assert cuerpo["bloquea_agendamiento"] is False

    async def test_un_paciente_inexistente_da_404_traducido(
        self, servidor: MCPServer[Any], escenario: Escenario
    ) -> None:
        with como(SUJETO, ["read"]):
            mensaje = await error_de(servidor, "consultar_cartera", {"paciente_id": 999999})
        assert "PACIENTE_NO_ENCONTRADO" in mensaje


class TestCitas:
    async def test_consultar_una_cita_inexistente(
        self, servidor: MCPServer[Any], escenario: Escenario
    ) -> None:
        with como(SUJETO, ["read"]):
            mensaje = await error_de(servidor, "consultar_cita", {"cita_id": 999999})
        assert "CITA_NO_ENCONTRADA" in mensaje

    async def test_listar_citas_de_un_paciente_sin_citas(
        self, servidor: MCPServer[Any], escenario: Escenario
    ) -> None:
        with como(SUJETO, ["read"]):
            resultado = await servidor.call_tool(
                "listar_citas_paciente", {"paciente_id": escenario.ana_id}
            )
        assert datos(resultado) == []


class TestAuditoriaDeLectura:
    async def test_toda_lectura_queda_registrada(
        self, servidor: MCPServer[Any], ctx: Any, escenario: Escenario
    ) -> None:
        with como("auditor@clinica.test", ["read"]):
            await servidor.call_tool("buscar_paciente", {"documento": "11111111"})
        evento = ctx.auditor.eventos[-1]
        assert evento["evento"] == "tool.invocacion"
        assert evento["herramienta"] == "buscar_paciente"
        assert evento["sujeto"] == "auditor@clinica.test"
        assert evento["resultado"] == "ok"

    async def test_las_lecturas_fallidas_tambien_se_registran(
        self, servidor: MCPServer[Any], ctx: Any, escenario: Escenario
    ) -> None:
        """A log that only records successes cannot tell you an agent spent an
        hour failing."""
        with como(SUJETO, ["read"]):
            await error_de(servidor, "consultar_cita", {"cita_id": 999999})
        evento = ctx.auditor.eventos[-1]
        assert evento["resultado"] == "error"
        assert evento["codigo_error"] == "CITA_NO_ENCONTRADA"

    async def test_el_documento_no_se_copia_al_log(
        self, servidor: MCPServer[Any], ctx: Any, escenario: Escenario
    ) -> None:
        """An audit log is not an excuse to duplicate identifiers somewhere less
        protected."""
        with como(SUJETO, ["read"]):
            await servidor.call_tool("buscar_paciente", {"documento": "11111111"})
        evento = ctx.auditor.eventos[-1]
        assert evento["argumentos"]["documento"] == "«redactado»"
        assert "11111111" not in str(evento)
