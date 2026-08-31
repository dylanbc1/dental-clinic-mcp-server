"""``confirmar_operacion``, the second half of the human-in-the-loop.

This is the only tool that mutates anything. It takes a token minted by a write
or clinical tool and, if the token is genuine, unspent, unexpired and issued to
the caller, runs the action it names with the arguments it carries.

The scope is re-checked here, against the action inside the token rather than
against the tool itself. That matters: a token minted while the caller held
`clinical` must not still execute if that scope is gone by the time it is
redeemed. Approval and authority are checked at the moment of effect, not at the
moment of intent.
"""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from mcp_server.auth import Scope, exigir_scope
from mcp_server.contexto import Contexto
from mcp_server.errores import ErrorHerramienta
from mcp_server.tools.write import EJECUTORES, SCOPE_DE_ACCION


def registrar(servidor: MCPServer[Any], ctx: Contexto) -> None:
    @servidor.tool(
        name="confirmar_operacion",
        title="Ejecutar una operación aprobada por un humano",
        description=(
            "Ejecuta una operación que ya fue propuesta y aprobada por una persona. Pasa "
            "el token_confirmacion EXACTAMENTE como lo devolvió la herramienta que hizo la "
            "propuesta. No lo modifiques, no lo construyas tú y no lo reutilices: cada "
            "token sirve una sola vez y caduca. Llama esta herramienta ÚNICAMENTE después "
            "de que la persona responsable haya visto el resumen y lo haya aprobado."
        ),
    )
    async def confirmar_operacion(
        token_confirmacion: Annotated[
            str,
            Field(
                min_length=16,
                max_length=4096,
                description="El valor devuelto en 'token_confirmacion' por la propuesta.",
            ),
        ],
    ) -> dict[str, Any]:
        # No scope of its own: the token names the action, and that action's
        # scope is what gets checked below. A tool that required, say, `write`
        # would silently let a `write` token execute a clinical proposal.
        identidad = ctx.identidad()
        aprobada = ctx.aprobaciones.verificar(token_confirmacion, sujeto=identidad.sujeto)

        ejecutor = EJECUTORES.get(aprobada.accion)
        scope_requerido = SCOPE_DE_ACCION.get(aprobada.accion)
        if ejecutor is None or scope_requerido is None:  # pragma: no cover - signed payload
            raise ErrorHerramienta(
                "APROBACION_INVALIDA",
                f"La operación '{aprobada.accion}' no existe en este servidor.",
                sugerencia="Genera una propuesta nueva con la herramienta correspondiente.",
            )

        exigir_scope(identidad, Scope(scope_requerido), herramienta=aprobada.accion)

        ctx.auditor.propuesta_confirmada(
            aprobada.accion, sujeto=identidad.sujeto, nonce=aprobada.nonce
        )
        try:
            resultado = await ejecutor(ctx, identidad.sujeto, aprobada.argumentos)
        except ErrorHerramienta as error:
            ctx.auditor.invocacion(
                aprobada.accion,
                sujeto=identidad.sujeto,
                scope=str(scope_requerido),
                argumentos=aprobada.argumentos,
                resultado="error",
                codigo_error=error.codigo,
                aprobada=True,
            )
            if scope_requerido is Scope.CLINICAL:
                ctx.auditor.acceso_clinico(
                    sujeto=identidad.sujeto,
                    cita_id=int(aprobada.argumentos.get("cita_id", 0)),
                    resultado=f"rechazado:{error.codigo}",
                )
            raise

        ctx.auditor.invocacion(
            aprobada.accion,
            sujeto=identidad.sujeto,
            scope=str(scope_requerido),
            argumentos=aprobada.argumentos,
            resultado="ok",
            aprobada=True,
        )
        if scope_requerido is Scope.CLINICAL:
            ctx.auditor.acceso_clinico(
                sujeto=identidad.sujeto,
                cita_id=int(aprobada.argumentos.get("cita_id", 0)),
                resultado="registrado",
            )

        return {
            "ejecutada": True,
            "accion": aprobada.accion,
            "resultado": resultado,
        }
