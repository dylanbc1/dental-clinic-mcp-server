"""Security layer 3: the sealed request state, attacked directly.

MRTR splits a mutation into two calls, and everything that makes the second one
safe lives in `requestState`: the paused operation, sealed by the SDK with
AES-256-GCM and bound to the request, the audience and the authenticated
principal.

That is the piece an attacker would go for. These tests are the ways in.
"""

import asyncio
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.config import Settings
from backend.models import Appointment
from tests.conftest import (
    SUBJECT,
    MCPTestClient,
    Scenario,
    ToolCallError,
    ToolContext,
    as_caller,
    http_server,
)

pytestmark = [pytest.mark.integration, pytest.mark.security]

ESCRITURA = ["read", "write"]


def contar_citas(session_: Session) -> int:
    return session_.scalar(select(func.count()).select_from(Appointment)) or 0


@pytest.fixture
def args(scenario: Scenario) -> dict[str, Any]:
    return {"patient_id": scenario.ana_id, "slot_id": scenario.slots_general[0]}


class TestConfidencialidad:
    async def test_el_estado_no_revela_la_operacion(
        self, mcp: MCPTestClient, args: dict[str, Any], scenario: Scenario
    ) -> None:
        """Sealed, not merely signed.

        A signed token would carry the arguments in the clear, which puts the
        patient id and the operation in whatever the client logs. This one is
        encrypted, so it says nothing to anyone holding it.
        """
        with as_caller(SUBJECT, ESCRITURA):
            status = (await mcp.ask("book_appointment", args))["requestState"]

        # Only distinctive strings are worth asserting on: a bare id like "1"
        # occurs in any base64 blob by chance and would prove nothing.
        assert "book_appointment" not in status
        assert "patient_id" not in status
        assert "slot_id" not in status
        assert SUBJECT not in status
        assert scenario.ana_documento not in status

    async def test_el_estado_esta_versionado(
        self, mcp: MCPTestClient, args: dict[str, Any]
    ) -> None:
        """A version prefix is what makes the format changeable later."""
        with as_caller(SUBJECT, ESCRITURA):
            status = (await mcp.ask("book_appointment", args))["requestState"]
        assert status.startswith("v1.")

    async def test_cada_pregunta_produce_un_estado_distinto(
        self, mcp: MCPTestClient, args: dict[str, Any]
    ) -> None:
        with as_caller(SUBJECT, ESCRITURA):
            primero = (await mcp.ask("book_appointment", args))["requestState"]
            segundo = (await mcp.ask("book_appointment", args))["requestState"]
        assert primero != segundo


class TestIntegridad:
    async def test_un_estado_alterado_se_rechaza(
        self, mcp: MCPTestClient, backend_session: Session, args: dict[str, Any]
    ) -> None:
        with as_caller(SUBJECT, ESCRITURA):
            question = await mcp.ask("book_appointment", args)
            tampered = {**question, "requestState": question["requestState"][:-4] + "AAAA"}
            with pytest.raises(ToolCallError):
                await mcp.respond("book_appointment", args, tampered)
        assert contar_citas(backend_session) == 0

    async def test_un_estado_inventado_se_rechaza(
        self, mcp: MCPTestClient, args: dict[str, Any]
    ) -> None:
        with as_caller(SUBJECT, ESCRITURA):
            question = await mcp.ask("book_appointment", args)
            falso = {**question, "requestState": "v1." + "A" * 300}
            with pytest.raises(ToolCallError):
                await mcp.respond("book_appointment", args, falso)

    @pytest.mark.parametrize("basura", ["", "no-es-un-estado", "v1.", "v9.abc"])
    async def test_un_estado_malformado_no_revienta_el_servidor(
        self, mcp: MCPTestClient, args: dict[str, Any], basura: str
    ) -> None:
        with as_caller(SUBJECT, ESCRITURA):
            question = await mcp.ask("book_appointment", args)
            with pytest.raises(ToolCallError):
                await mcp.respond("book_appointment", args, {**question, "requestState": basura})


class TestAtadoALaOperacion:
    async def test_un_estado_de_otra_tool_no_sirve(
        self, mcp: MCPTestClient, backend_session: Session, args: dict[str, Any]
    ) -> None:
        """The attack this defeats: get an approval for something harmless, then
        redeem it against something else."""
        from backend.domain.services import book_appointment as book_route

        appointment = book_route(
            backend_session,
            patient_id=args["patient_id"],
            slot_id=args["slot_id"],
            user="setup",
        ).appointment
        backend_session.commit()

        with as_caller(SUBJECT, ESCRITURA):
            inocua = await mcp.ask("confirm_appointment", {"appointment_id": appointment.id})
            with pytest.raises(ToolCallError):
                await mcp.respond(
                    "cancel_appointment",
                    {"appointment_id": appointment.id, "reason": "usando otra aprobación"},
                    inocua,
                )

    async def test_un_estado_no_sirve_con_otros_argumentos(
        self, mcp: MCPTestClient, scenario: Scenario, args: dict[str, Any]
    ) -> None:
        """Approving a booking for one patient must not book another."""
        with as_caller(SUBJECT, ESCRITURA):
            question = await mcp.ask("book_appointment", args)
            otros = {**args, "patient_id": scenario.carla_id}
            with pytest.raises(ToolCallError):
                await mcp.respond("book_appointment", otros, question)


class TestAtadoAlPrincipal:
    async def test_otro_usuario_no_puede_canjear_mi_aprobacion(
        self, mcp: MCPTestClient, backend_session: Session, args: dict[str, Any]
    ) -> None:
        """`bind_principal` ties the sealed state to the authenticated subject,
        so an approval is not transferable."""
        with as_caller(SUBJECT, ESCRITURA):
            question = await mcp.ask("book_appointment", args)

        with as_caller("intruso@clinica.test", ESCRITURA), pytest.raises(ToolCallError):
            await mcp.respond("book_appointment", args, question)
        assert contar_citas(backend_session) == 0


class TestExpiracion:
    async def test_un_estado_vencido_se_rechaza(
        self,
        ctx: ToolContext,
        mcp_settings: Settings,
        args: dict[str, Any],
        backend_session: Session,
    ) -> None:
        """An approval granted a while ago does not authorise an action taken
        now. The TTL is what bounds "a while"."""
        breve = mcp_settings.model_copy(update={"request_state_ttl_seconds": 0.5})
        async with http_server(ctx, breve) as corto:
            with as_caller(SUBJECT, ESCRITURA):
                question = await corto.ask("book_appointment", args)
                await asyncio.sleep(0.8)
                with pytest.raises(ToolCallError):
                    await corto.respond("book_appointment", args, question)
        assert contar_citas(backend_session) == 0

    async def test_dentro_del_plazo_todavia_sirve(
        self, ctx: ToolContext, mcp_settings: Settings, args: dict[str, Any]
    ) -> None:
        breve = mcp_settings.model_copy(update={"request_state_ttl_seconds": 30.0})
        async with http_server(ctx, breve) as corto:
            with as_caller(SUBJECT, ESCRITURA):
                question = await corto.ask("book_appointment", args)
                result = await corto.respond("book_appointment", args, question)
        assert result["appointment"]["status"] == "scheduled"


class TestRotacionDeClaves:
    """`keys[0]` seals, every key unseals. That is what makes rotation
    zero-downtime: ship [old, new], then [new, old], then [new] after one TTL."""

    async def test_una_clave_nueva_sigue_abriendo_lo_sellado_con_la_vieja(
        self, ctx: ToolContext, mcp_settings: Settings, args: dict[str, Any]
    ) -> None:
        vieja = "clave-vieja-de-treinta-y-dos-bytes-ok"
        nueva = "clave-nueva-de-treinta-y-dos-bytes-ok"

        async with http_server(
            ctx, mcp_settings.model_copy(update={"request_state_keys": [vieja]})
        ) as antes:
            with as_caller(SUBJECT, ESCRITURA):
                question = await antes.ask("book_appointment", args)

        # Mid-rotation: the new key seals, the old one still unseals.
        async with http_server(
            ctx, mcp_settings.model_copy(update={"request_state_keys": [nueva, vieja]})
        ) as durante:
            with as_caller(SUBJECT, ESCRITURA):
                result = await durante.respond("book_appointment", args, question)
        assert result["appointment"]["status"] == "scheduled"

    async def test_retirada_la_clave_vieja_su_estado_deja_de_valer(
        self,
        ctx: ToolContext,
        mcp_settings: Settings,
        args: dict[str, Any],
        backend_session: Session,
    ) -> None:
        vieja = "clave-vieja-de-treinta-y-dos-bytes-ok"
        nueva = "clave-nueva-de-treinta-y-dos-bytes-ok"

        async with http_server(
            ctx, mcp_settings.model_copy(update={"request_state_keys": [vieja]})
        ) as antes:
            with as_caller(SUBJECT, ESCRITURA):
                question = await antes.ask("book_appointment", args)

        async with http_server(
            ctx, mcp_settings.model_copy(update={"request_state_keys": [nueva]})
        ) as despues:
            with as_caller(SUBJECT, ESCRITURA), pytest.raises(ToolCallError):
                await despues.respond("book_appointment", args, question)
        assert contar_citas(backend_session) == 0


class TestSinEstadoEnElServidor:
    async def test_otro_proceso_puede_atender_la_segunda_ronda(
        self, ctx: ToolContext, mcp_settings: Settings, args: dict[str, Any]
    ) -> None:
        """The whole point of MRTR: the pending operation lives in the client's
        hands, sealed, so the replica that asked need not be the one that
        executes. Two independent server instances stand in for two replicas.
        """
        async with http_server(ctx, mcp_settings) as replica_a:
            with as_caller(SUBJECT, ESCRITURA):
                question = await replica_a.ask("book_appointment", args)

        async with http_server(ctx, mcp_settings) as replica_b:
            with as_caller(SUBJECT, ESCRITURA):
                result = await replica_b.respond("book_appointment", args, question)

        assert result["appointment"]["status"] == "scheduled"
