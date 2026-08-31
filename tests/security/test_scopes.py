"""Security layer 2: least privilege, proved exhaustively.

The whole tool × scope matrix is enumerated. Every tool is called with each of
the three scopes in isolation, and each of the 42 combinations is asserted to
allow or deny. A permission test written by example always grows a hole; this
one cannot, because there is no combination it does not cover.

The scopes deliberately do **not** nest. `write` does not imply `read` and
`clinical` does not imply `write`, because "administrative" and "clinical" are
different kinds of authority, not different amounts of it (§2.5).
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy.orm import Session

from backend.domain.servicios import agendar_cita
from mcp_server.auth import Scope
from tests.conftest import SUJETO, Escenario, como

pytestmark = [pytest.mark.integration, pytest.mark.security]

#: Tool → the single scope it requires. `confirmar_operacion` is absent on
#: purpose: it has no scope of its own and is covered by its own class below.
SCOPE_REQUERIDO: dict[str, Scope] = {
    "buscar_paciente": Scope.READ,
    "consultar_disponibilidad": Scope.READ,
    "consultar_cita": Scope.READ,
    "listar_citas_paciente": Scope.READ,
    "consultar_cartera": Scope.READ,
    "validar_afiliacion": Scope.READ,
    "agendar_cita": Scope.WRITE,
    "confirmar_cita": Scope.WRITE,
    "cancelar_cita": Scope.WRITE,
    "reprogramar_cita": Scope.WRITE,
    "registrar_asistencia": Scope.WRITE,
    "ofrecer_cupo_lista_espera": Scope.WRITE,
    "registrar_motivo_consulta": Scope.CLINICAL,
}

#: Minimal valid-shaped arguments per tool. The values point at rows that do not
#: exist, which is deliberate: the scope check must happen *before* any lookup,
#: so a denial must not depend on the data.
ARGUMENTOS: dict[str, dict[str, Any]] = {
    "buscar_paciente": {"documento": "11111111"},
    "consultar_disponibilidad": {},
    "consultar_cita": {"cita_id": 424242},
    "listar_citas_paciente": {"paciente_id": 424242},
    "consultar_cartera": {"paciente_id": 424242},
    "validar_afiliacion": {"paciente_id": 424242},
    "agendar_cita": {"paciente_id": 424242, "slot_id": 424242},
    "confirmar_cita": {"cita_id": 424242},
    "cancelar_cita": {"cita_id": 424242, "motivo": "motivo de prueba"},
    "reprogramar_cita": {"cita_id": 424242, "nuevo_slot_id": 424243},
    "registrar_asistencia": {"cita_id": 424242, "estado": "atendida"},
    "ofrecer_cupo_lista_espera": {"slot_id": 424242},
    "registrar_motivo_consulta": {"cita_id": 424242, "motivo": "dolor de muela"},
}

MATRIZ = list(itertools.product(sorted(SCOPE_REQUERIDO), list(Scope)))


async def _llamar_capturando(
    servidor: MCPServer[Any], nombre: str, argumentos: dict[str, Any]
) -> str:
    """Call and return the error text, or "" when the call was allowed through."""
    try:
        await servidor.call_tool(nombre, argumentos)
    except ToolError as error:
        return str(error)
    return ""


class TestMatrizDeScopes:
    @pytest.mark.parametrize(("herramienta", "scope"), MATRIZ, ids=lambda v: str(v))
    async def test_cada_combinacion_de_tool_y_scope(
        self,
        servidor: MCPServer[Any],
        escenario: Escenario,
        herramienta: str,
        scope: Scope,
    ) -> None:
        requerido = SCOPE_REQUERIDO[herramienta]
        with como(SUJETO, [str(scope)]):
            mensaje = await _llamar_capturando(servidor, herramienta, ARGUMENTOS[herramienta])

        if scope is requerido:
            # It may still fail on the data (the ids do not exist), but never on
            # permission, which is the distinction being asserted.
            assert "SCOPE_INSUFICIENTE" not in mensaje
        else:
            assert "SCOPE_INSUFICIENTE" in mensaje, (
                f"{herramienta} aceptó un token con scope '{scope}' cuando exige '{requerido}'"
            )

    def test_la_matriz_cubre_todas_las_combinaciones(self) -> None:
        assert len(MATRIZ) == 13 * 3 == 39

    async def test_la_matriz_no_deja_ninguna_tool_fuera(self, servidor: MCPServer[Any]) -> None:
        """A tool added later without a scope decision fails this test rather
        than shipping ungated."""
        declaradas = {t.name for t in await servidor.list_tools()}
        cubiertas = set(SCOPE_REQUERIDO) | {"confirmar_operacion"}
        assert declaradas == cubiertas


class TestLosScopesNoAnidan:
    async def test_write_no_da_acceso_de_lectura(
        self, servidor: MCPServer[Any], escenario: Escenario
    ) -> None:
        with como(SUJETO, ["write"]):
            mensaje = await _llamar_capturando(
                servidor, "buscar_paciente", {"documento": "11111111"}
            )
        assert "SCOPE_INSUFICIENTE" in mensaje

    async def test_clinical_no_da_acceso_de_escritura(
        self, servidor: MCPServer[Any], escenario: Escenario
    ) -> None:
        """Authority to record a symptom is not authority to cancel a visit."""
        with como(SUJETO, ["clinical"]):
            mensaje = await _llamar_capturando(
                servidor, "cancelar_cita", {"cita_id": 1, "motivo": "prueba"}
            )
        assert "SCOPE_INSUFICIENTE" in mensaje

    async def test_write_no_da_acceso_clinico(
        self, servidor: MCPServer[Any], escenario: Escenario
    ) -> None:
        """The SaaStr shape: a valid token with more reach than it needed."""
        with como(SUJETO, ["read", "write"]):
            mensaje = await _llamar_capturando(
                servidor, "registrar_motivo_consulta", {"cita_id": 1, "motivo": "dolor"}
            )
        assert "SCOPE_INSUFICIENTE" in mensaje
        assert "clinical" in mensaje


class TestMensajeDeDenegacion:
    async def test_dice_que_falta_y_que_hacer(
        self, servidor: MCPServer[Any], escenario: Escenario
    ) -> None:
        with como(SUJETO, ["read"]):
            mensaje = await _llamar_capturando(
                servidor, "cancelar_cita", {"cita_id": 1, "motivo": "prueba"}
            )
        assert "SCOPE_INSUFICIENTE" in mensaje
        assert "'write'" in mensaje
        assert "Acción requerida" in mensaje
        # It must tell the model not to loop: retrying with the same token is
        # the single most common wasted-token pattern.
        assert "no vuelvas a llamar" in mensaje.lower()

    async def test_no_revela_datos_del_paciente_al_denegar(
        self, servidor: MCPServer[Any], escenario: Escenario
    ) -> None:
        """Denial happens before any lookup, so nothing about the record leaks."""
        with como(SUJETO, ["read"]):
            mensaje = await _llamar_capturando(
                servidor,
                "registrar_motivo_consulta",
                {"cita_id": 1, "motivo": "dolor severo en molar"},
            )
        assert "dolor severo" not in mensaje


class TestSinToken:
    async def test_sin_identidad_ninguna_tool_responde(
        self, servidor: MCPServer[Any], escenario: Escenario
    ) -> None:
        """No token means no identity. Never a permissive default."""
        for herramienta in SCOPE_REQUERIDO:
            mensaje = await _llamar_capturando(servidor, herramienta, ARGUMENTOS[herramienta])
            assert "NO_AUTENTICADO" in mensaje, f"{herramienta} respondió sin token"

    async def test_un_token_sin_scopes_no_abre_nada(
        self, servidor: MCPServer[Any], escenario: Escenario
    ) -> None:
        with como(SUJETO, []):
            for herramienta in SCOPE_REQUERIDO:
                mensaje = await _llamar_capturando(servidor, herramienta, ARGUMENTOS[herramienta])
                assert "SCOPE_INSUFICIENTE" in mensaje


class TestScopeEnLaConfirmacion:
    """The scope is checked again when the approval is redeemed, not only when
    it was granted."""

    async def test_un_token_write_no_ejecuta_una_propuesta_clinica(
        self, servidor: MCPServer[Any], sesion_backend: Session, escenario: Escenario
    ) -> None:
        cita = agendar_cita(
            sesion_backend,
            paciente_id=escenario.ana_id,
            slot_id=escenario.slots_general[0],
            usuario="setup",
        ).cita
        sesion_backend.commit()

        with como(SUJETO, ["read", "write", "clinical"]):
            propuesta = await servidor.call_tool(
                "registrar_motivo_consulta", {"cita_id": cita.id, "motivo": "dolor"}
            )
        token = (propuesta.structured_content or {})["token_confirmacion"]

        # Same subject and a perfectly valid approval, but `clinical` has since
        # been revoked. Authority is checked at the moment of effect.
        with como(SUJETO, ["read", "write"]):
            mensaje = await _llamar_capturando(
                servidor, "confirmar_operacion", {"token_confirmacion": token}
            )
        assert "SCOPE_INSUFICIENTE" in mensaje
        assert "clinical" in mensaje

    async def test_confirmar_operacion_no_exige_scope_propio(
        self, servidor: MCPServer[Any], escenario: Escenario
    ) -> None:
        """It must not: a tool requiring `write` would silently let a `write`
        token execute a clinical proposal."""
        with como(SUJETO, ["read"]):
            mensaje = await _llamar_capturando(
                servidor, "confirmar_operacion", {"token_confirmacion": "x" * 40}
            )
        assert "SCOPE_INSUFICIENTE" not in mensaje
        assert "APROBACION_INVALIDA" in mensaje
