"""Security layer 2: least privilege, proved exhaustively over the wire.

The whole tool by scope matrix is enumerated. Every tool is called with each of
the three scopes in isolation, and each of the 39 combinations is asserted to
allow or deny. A permission suite written by example always grows a hole; this
one cannot, because there is no combination it does not cover.

The scopes deliberately do **not** nest. `write` does not imply `read` and
`clinical` does not imply `write`, because administrative and clinical are
different kinds of authority, not different amounts of it (§2.5).
"""

import itertools
from typing import Any

import pytest
from sqlalchemy.orm import Session

from backend.domain.services import book_appointment
from mcp_server.auth import Scope
from tests.conftest import SUBJECT, MCPTestClient, Scenario, ToolCallError, as_caller

pytestmark = [pytest.mark.integration, pytest.mark.security]

#: Tool → the single scope it requires.
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
#: exist, which is deliberate: the scope check must happen before any lookup, so
#: a denial cannot depend on the data, and cannot leak whether it exists.
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


async def error_from(mcp: MCPTestClient, nombre: str, arguments: dict[str, Any]) -> str:
    """Call over the wire and return the error text, or "" if it got through.

    A write tool that gets through pauses for a human rather than mutating, so
    "got through" here means it reached the point of asking.
    """
    try:
        result = await mcp._rpc("tools/call", {"name": nombre, "arguments": arguments})
    except ToolCallError as fallo:
        return fallo.text_of
    if result.get("isError"):
        return "\n".join(c.get("text", "") for c in result.get("content", []))
    return ""


class TestMatrizDeScopes:
    @pytest.mark.parametrize(("tool_name", "scope"), MATRIZ, ids=lambda v: str(v))
    async def test_cada_combinacion_de_tool_y_scope(
        self, mcp: MCPTestClient, scenario: Scenario, tool_name: str, scope: Scope
    ) -> None:
        requerido = SCOPE_REQUERIDO[tool_name]
        with as_caller(SUBJECT, [str(scope)]):
            mensaje = await error_from(mcp, tool_name, ARGUMENTOS[tool_name])

        if scope is requerido:
            # It may still fail on the data, since the ids do not exist, but
            # never on permission. That is the distinction being asserted.
            assert "SCOPE_INSUFICIENTE" not in mensaje
        else:
            assert "SCOPE_INSUFICIENTE" in mensaje, (
                f"{tool_name} aceptó un token con scope '{scope}' cuando exige '{requerido}'"
            )

    def test_la_matriz_cubre_todas_las_combinaciones(self) -> None:
        assert len(MATRIZ) == 13 * 3 == 39

    async def test_la_matriz_no_deja_ninguna_tool_fuera(self, server_: Any) -> None:
        """A tool added later without a scope decision fails this test rather
        than shipping ungated."""
        declaradas = {t.name for t in await server_.list_tools()}
        assert declaradas == set(SCOPE_REQUERIDO)


class TestLosScopesNoAnidan:
    async def test_write_no_da_acceso_de_lectura(
        self, mcp: MCPTestClient, scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["write"]):
            mensaje = await error_from(mcp, "buscar_paciente", {"documento": "11111111"})
        assert "SCOPE_INSUFICIENTE" in mensaje

    async def test_clinical_no_da_acceso_de_escritura(
        self, mcp: MCPTestClient, scenario: Scenario
    ) -> None:
        """Authority to record a symptom is not authority to cancel a visit."""
        with as_caller(SUBJECT, ["clinical"]):
            mensaje = await error_from(mcp, "cancelar_cita", {"cita_id": 1, "motivo": "prueba"})
        assert "SCOPE_INSUFICIENTE" in mensaje

    async def test_write_no_da_acceso_clinico(self, mcp: MCPTestClient, scenario: Scenario) -> None:
        """The SaaStr shape: a valid token with more reach than it needed."""
        with as_caller(SUBJECT, ["read", "write"]):
            mensaje = await error_from(
                mcp, "registrar_motivo_consulta", {"cita_id": 1, "motivo": "dolor"}
            )
        assert "SCOPE_INSUFICIENTE" in mensaje
        assert "clinical" in mensaje


class TestMensajeDeDenegacion:
    async def test_dice_que_falta_y_que_hacer(self, mcp: MCPTestClient, scenario: Scenario) -> None:
        with as_caller(SUBJECT, ["read"]):
            mensaje = await error_from(mcp, "cancelar_cita", {"cita_id": 1, "motivo": "prueba"})
        assert "SCOPE_INSUFICIENTE" in mensaje
        assert "'write'" in mensaje
        assert "Action required" in mensaje
        # It must tell the model not to loop: retrying with the same token is
        # the single most common wasted-token pattern.
        assert "do not call this tool again" in mensaje.lower()

    async def test_no_revela_datos_del_paciente_al_denegar(
        self, mcp: MCPTestClient, scenario: Scenario
    ) -> None:
        """Denial happens before any lookup, so nothing about the record leaks."""
        with as_caller(SUBJECT, ["read"]):
            mensaje = await error_from(
                mcp,
                "registrar_motivo_consulta",
                {"cita_id": 1, "motivo": "dolor severo en molar"},
            )
        assert "dolor severo" not in mensaje


class TestSinToken:
    async def test_sin_identidad_ninguna_tool_responde(
        self, mcp: MCPTestClient, scenario: Scenario
    ) -> None:
        """No token means no identity. Never a permissive default."""
        for tool_name in SCOPE_REQUERIDO:
            mensaje = await error_from(mcp, tool_name, ARGUMENTOS[tool_name])
            assert "NO_AUTENTICADO" in mensaje, f"{tool_name} respondió sin token"

    async def test_un_token_sin_scopes_no_abre_nada(
        self, mcp: MCPTestClient, scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, []):
            for tool_name in SCOPE_REQUERIDO:
                mensaje = await error_from(mcp, tool_name, ARGUMENTOS[tool_name])
                assert "SCOPE_INSUFICIENTE" in mensaje


class TestElScopeSeRevisaEnAmbasRondas:
    """MRTR splits a mutation across two calls, and authority is checked on both.

    The resolver runs again when the client retries, so a token that has lost a
    scope between the question and the answer cannot execute the operation it
    was allowed to ask about. Authority is verified at the moment of effect, not
    only at the moment of intent.
    """

    async def test_un_token_que_pierde_clinical_no_ejecuta(
        self, mcp: MCPTestClient, backend_session: Session, scenario: Scenario
    ) -> None:
        cita = book_appointment(
            backend_session,
            paciente_id=scenario.ana_id,
            slot_id=scenario.slots_general[0],
            usuario="setup",
        ).cita
        backend_session.commit()
        args = {"cita_id": cita.id, "motivo": "dolor"}

        with as_caller(SUBJECT, ["read", "write", "clinical"]):
            question = await mcp.ask("registrar_motivo_consulta", args)

        # Same subject and a perfectly valid approval, but `clinical` is gone.
        with as_caller(SUBJECT, ["read", "write"]), pytest.raises(ToolCallError) as exc:
            await mcp.respond("registrar_motivo_consulta", args, question)
        assert "SCOPE_INSUFICIENTE" in exc.value.text_of
        assert "clinical" in exc.value.text_of

    async def test_un_token_que_pierde_write_no_ejecuta(
        self, mcp: MCPTestClient, scenario: Scenario, backend_session: Session
    ) -> None:
        cita = book_appointment(
            backend_session,
            paciente_id=scenario.ana_id,
            slot_id=scenario.slots_general[0],
            usuario="setup",
        ).cita
        backend_session.commit()

        with as_caller(SUBJECT, ["read", "write"]):
            question = await mcp.ask("confirmar_cita", {"cita_id": cita.id})

        with as_caller(SUBJECT, ["read"]), pytest.raises(ToolCallError) as exc:
            await mcp.respond("confirmar_cita", {"cita_id": cita.id}, question)
        assert "SCOPE_INSUFICIENTE" in exc.value.text_of


class TestElOrdenDeLosChequeos:
    """Authorisation runs before anything else the tool might complain about.

    A caller with no permission should be told that, not something about their
    client's capabilities: the first refusal a request meets should be the one
    that is actually their problem, and it is the one that must be audited.
    """

    async def test_sin_scope_gana_el_error_de_scope_no_el_de_cliente(
        self, mcp_without_elicitation: MCPTestClient, scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read"]):
            mensaje = await error_from(
                mcp_without_elicitation, "cancelar_cita", {"cita_id": 1, "motivo": "prueba"}
            )
        assert "SCOPE_INSUFICIENTE" in mensaje
        assert "CLIENTE_SIN_CONFIRMACION" not in mensaje

    async def test_sin_token_gana_el_error_de_autenticacion(
        self, mcp_without_elicitation: MCPTestClient, scenario: Scenario
    ) -> None:
        mensaje = await error_from(
            mcp_without_elicitation, "cancelar_cita", {"cita_id": 1, "motivo": "prueba"}
        )
        assert "NO_AUTENTICADO" in mensaje
        assert "CLIENTE_SIN_CONFIRMACION" not in mensaje

    async def test_la_denegacion_por_scope_se_audita_aunque_el_cliente_no_confirme(
        self, mcp_without_elicitation: MCPTestClient, ctx: Any, scenario: Scenario
    ) -> None:
        """Checking the capability first would have skipped the audit entirely."""
        with as_caller(SUBJECT, ["read"]):
            await error_from(
                mcp_without_elicitation, "cancelar_cita", {"cita_id": 1, "motivo": "prueba"}
            )
        evento = ctx.auditor.events[-1]
        assert evento["result"] == "error"
        assert evento["error_code"] == "SCOPE_INSUFICIENTE"

    async def test_con_scope_correcto_si_avisa_del_cliente(
        self, mcp_without_elicitation: MCPTestClient, scenario: Scenario
    ) -> None:
        with as_caller(SUBJECT, ["read", "write"]):
            mensaje = await error_from(
                mcp_without_elicitation, "cancelar_cita", {"cita_id": 1, "motivo": "prueba"}
            )
        assert "CLIENTE_SIN_CONFIRMACION" in mensaje
