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
from backend.models import Cita
from tests.conftest import (
    SUJETO,
    ClienteMCP,
    Contexto,
    ErrorDeHerramienta,
    Escenario,
    como,
    servidor_http,
)

pytestmark = [pytest.mark.integration, pytest.mark.security]

ESCRITURA = ["read", "write"]


def contar_citas(sesion: Session) -> int:
    return sesion.scalar(select(func.count()).select_from(Cita)) or 0


@pytest.fixture
def args(escenario: Escenario) -> dict[str, Any]:
    return {"paciente_id": escenario.ana_id, "slot_id": escenario.slots_general[0]}


class TestConfidencialidad:
    async def test_el_estado_no_revela_la_operacion(
        self, mcp: ClienteMCP, args: dict[str, Any], escenario: Escenario
    ) -> None:
        """Sealed, not merely signed.

        A signed token would carry the arguments in the clear, which puts the
        patient id and the operation in whatever the client logs. This one is
        encrypted, so it says nothing to anyone holding it.
        """
        with como(SUJETO, ESCRITURA):
            estado = (await mcp.preguntar("agendar_cita", args))["requestState"]

        # Only distinctive strings are worth asserting on: a bare id like "1"
        # occurs in any base64 blob by chance and would prove nothing.
        assert "agendar_cita" not in estado
        assert "paciente_id" not in estado
        assert "slot_id" not in estado
        assert SUJETO not in estado
        assert escenario.ana_documento not in estado

    async def test_el_estado_esta_versionado(self, mcp: ClienteMCP, args: dict[str, Any]) -> None:
        """A version prefix is what makes the format changeable later."""
        with como(SUJETO, ESCRITURA):
            estado = (await mcp.preguntar("agendar_cita", args))["requestState"]
        assert estado.startswith("v1.")

    async def test_cada_pregunta_produce_un_estado_distinto(
        self, mcp: ClienteMCP, args: dict[str, Any]
    ) -> None:
        with como(SUJETO, ESCRITURA):
            primero = (await mcp.preguntar("agendar_cita", args))["requestState"]
            segundo = (await mcp.preguntar("agendar_cita", args))["requestState"]
        assert primero != segundo


class TestIntegridad:
    async def test_un_estado_alterado_se_rechaza(
        self, mcp: ClienteMCP, sesion_backend: Session, args: dict[str, Any]
    ) -> None:
        with como(SUJETO, ESCRITURA):
            pregunta = await mcp.preguntar("agendar_cita", args)
            alterado = {**pregunta, "requestState": pregunta["requestState"][:-4] + "AAAA"}
            with pytest.raises(ErrorDeHerramienta):
                await mcp.responder("agendar_cita", args, alterado)
        assert contar_citas(sesion_backend) == 0

    async def test_un_estado_inventado_se_rechaza(
        self, mcp: ClienteMCP, args: dict[str, Any]
    ) -> None:
        with como(SUJETO, ESCRITURA):
            pregunta = await mcp.preguntar("agendar_cita", args)
            falso = {**pregunta, "requestState": "v1." + "A" * 300}
            with pytest.raises(ErrorDeHerramienta):
                await mcp.responder("agendar_cita", args, falso)

    @pytest.mark.parametrize("basura", ["", "no-es-un-estado", "v1.", "v9.abc"])
    async def test_un_estado_malformado_no_revienta_el_servidor(
        self, mcp: ClienteMCP, args: dict[str, Any], basura: str
    ) -> None:
        with como(SUJETO, ESCRITURA):
            pregunta = await mcp.preguntar("agendar_cita", args)
            with pytest.raises(ErrorDeHerramienta):
                await mcp.responder("agendar_cita", args, {**pregunta, "requestState": basura})


class TestAtadoALaOperacion:
    async def test_un_estado_de_otra_tool_no_sirve(
        self, mcp: ClienteMCP, sesion_backend: Session, args: dict[str, Any]
    ) -> None:
        """The attack this defeats: get an approval for something harmless, then
        redeem it against something else."""
        from backend.domain.services import agendar_cita as agendar

        cita = agendar(
            sesion_backend,
            paciente_id=args["paciente_id"],
            slot_id=args["slot_id"],
            usuario="setup",
        ).cita
        sesion_backend.commit()

        with como(SUJETO, ESCRITURA):
            inocua = await mcp.preguntar("confirmar_cita", {"cita_id": cita.id})
            with pytest.raises(ErrorDeHerramienta):
                await mcp.responder(
                    "cancelar_cita",
                    {"cita_id": cita.id, "motivo": "usando otra aprobación"},
                    inocua,
                )

    async def test_un_estado_no_sirve_con_otros_argumentos(
        self, mcp: ClienteMCP, escenario: Escenario, args: dict[str, Any]
    ) -> None:
        """Approving a booking for one patient must not book another."""
        with como(SUJETO, ESCRITURA):
            pregunta = await mcp.preguntar("agendar_cita", args)
            otros = {**args, "paciente_id": escenario.carla_id}
            with pytest.raises(ErrorDeHerramienta):
                await mcp.responder("agendar_cita", otros, pregunta)


class TestAtadoAlPrincipal:
    async def test_otro_usuario_no_puede_canjear_mi_aprobacion(
        self, mcp: ClienteMCP, sesion_backend: Session, args: dict[str, Any]
    ) -> None:
        """`bind_principal` ties the sealed state to the authenticated subject,
        so an approval is not transferable."""
        with como(SUJETO, ESCRITURA):
            pregunta = await mcp.preguntar("agendar_cita", args)

        with como("intruso@clinica.test", ESCRITURA), pytest.raises(ErrorDeHerramienta):
            await mcp.responder("agendar_cita", args, pregunta)
        assert contar_citas(sesion_backend) == 0


class TestExpiracion:
    async def test_un_estado_vencido_se_rechaza(
        self,
        ctx: Contexto,
        ajustes_mcp: Settings,
        args: dict[str, Any],
        sesion_backend: Session,
    ) -> None:
        """An approval granted a while ago does not authorise an action taken
        now. The TTL is what bounds "a while"."""
        breve = ajustes_mcp.model_copy(update={"request_state_ttl_seconds": 0.5})
        async with servidor_http(ctx, breve) as corto:
            with como(SUJETO, ESCRITURA):
                pregunta = await corto.preguntar("agendar_cita", args)
                await asyncio.sleep(0.8)
                with pytest.raises(ErrorDeHerramienta):
                    await corto.responder("agendar_cita", args, pregunta)
        assert contar_citas(sesion_backend) == 0

    async def test_dentro_del_plazo_todavia_sirve(
        self, ctx: Contexto, ajustes_mcp: Settings, args: dict[str, Any]
    ) -> None:
        breve = ajustes_mcp.model_copy(update={"request_state_ttl_seconds": 30.0})
        async with servidor_http(ctx, breve) as corto:
            with como(SUJETO, ESCRITURA):
                pregunta = await corto.preguntar("agendar_cita", args)
                resultado = await corto.responder("agendar_cita", args, pregunta)
        assert resultado["cita"]["estado"] == "agendada"


class TestRotacionDeClaves:
    """`keys[0]` seals, every key unseals. That is what makes rotation
    zero-downtime: ship [old, new], then [new, old], then [new] after one TTL."""

    async def test_una_clave_nueva_sigue_abriendo_lo_sellado_con_la_vieja(
        self, ctx: Contexto, ajustes_mcp: Settings, args: dict[str, Any]
    ) -> None:
        vieja = "clave-vieja-de-treinta-y-dos-bytes-ok"
        nueva = "clave-nueva-de-treinta-y-dos-bytes-ok"

        async with servidor_http(
            ctx, ajustes_mcp.model_copy(update={"request_state_keys": [vieja]})
        ) as antes:
            with como(SUJETO, ESCRITURA):
                pregunta = await antes.preguntar("agendar_cita", args)

        # Mid-rotation: the new key seals, the old one still unseals.
        async with servidor_http(
            ctx, ajustes_mcp.model_copy(update={"request_state_keys": [nueva, vieja]})
        ) as durante:
            with como(SUJETO, ESCRITURA):
                resultado = await durante.responder("agendar_cita", args, pregunta)
        assert resultado["cita"]["estado"] == "agendada"

    async def test_retirada_la_clave_vieja_su_estado_deja_de_valer(
        self,
        ctx: Contexto,
        ajustes_mcp: Settings,
        args: dict[str, Any],
        sesion_backend: Session,
    ) -> None:
        vieja = "clave-vieja-de-treinta-y-dos-bytes-ok"
        nueva = "clave-nueva-de-treinta-y-dos-bytes-ok"

        async with servidor_http(
            ctx, ajustes_mcp.model_copy(update={"request_state_keys": [vieja]})
        ) as antes:
            with como(SUJETO, ESCRITURA):
                pregunta = await antes.preguntar("agendar_cita", args)

        async with servidor_http(
            ctx, ajustes_mcp.model_copy(update={"request_state_keys": [nueva]})
        ) as despues:
            with como(SUJETO, ESCRITURA), pytest.raises(ErrorDeHerramienta):
                await despues.responder("agendar_cita", args, pregunta)
        assert contar_citas(sesion_backend) == 0


class TestSinEstadoEnElServidor:
    async def test_otro_proceso_puede_atender_la_segunda_ronda(
        self, ctx: Contexto, ajustes_mcp: Settings, args: dict[str, Any]
    ) -> None:
        """The whole point of MRTR: the pending operation lives in the client's
        hands, sealed, so the replica that asked need not be the one that
        executes. Two independent server instances stand in for two replicas.
        """
        async with servidor_http(ctx, ajustes_mcp) as replica_a:
            with como(SUJETO, ESCRITURA):
                pregunta = await replica_a.preguntar("agendar_cita", args)

        async with servidor_http(ctx, ajustes_mcp) as replica_b:
            with como(SUJETO, ESCRITURA):
                resultado = await replica_b.responder("agendar_cita", args, pregunta)

        assert resultado["cita"]["estado"] == "agendada"
