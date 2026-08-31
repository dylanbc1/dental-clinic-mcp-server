"""The clinical tool (scope: ``clinical``), the most restricted surface here.

Booking an appointment is administrative. Recording *why* the patient is coming
is clinical data, and in Colombia that pulls in Resolución 2654/2019, informed
consent, and RNBD registration with the SIC. That boundary, §2.5 of the spec, is
why this tool has its own scope rather than being another write.

Three gates stack, and each is necessary because the others do not cover it:

1. **Scope** ``clinical``. A `write` token cannot reach it. Authority to move an
   appointment is not authority to read or write a diagnosis.
2. **Human approval**, like every mutation.
3. **Recorded consent for this specific patient**, enforced by the backend. The
   caller can hold every scope and a fresh approval and still be refused,
   because consent belongs to the patient, not to the operator.
"""

# No `from __future__ import annotations` here on purpose: the SDK evaluates a
# tool's annotations to find its `Resolve(...)` markers, and a string annotation
# naming a closure-local resolver cannot be resolved from module globals.
from typing import Annotated, Any

from mcp.server.mcpserver import Context, Elicit, MCPServer, Resolve
from pydantic import Field

from mcp_server.auth import Scope
from mcp_server.confirmacion import (
    Confirmacion,
    exigir_cliente_que_confirma,
    redactar_propuesta,
)
from mcp_server.contexto import Contexto
from mcp_server.tools.write import exigir_aprobacion

SCOPE = Scope.CLINICAL


def registrar(servidor: MCPServer[Any], ctx: Contexto) -> None:
    async def _confirmar_motivo(
        contexto: Context, cita_id: int, motivo: str
    ) -> Elicit[Confirmacion]:
        exigir_cliente_que_confirma(contexto)
        argumentos = {"cita_id": cita_id, "motivo": motivo}
        identidad = ctx.autorizar_auditando("registrar_motivo_consulta", SCOPE, argumentos)
        async with ctx.auditar_fallo("registrar_motivo_consulta", SCOPE, argumentos, identidad):
            cita = await ctx.cliente.obtener(f"/citas/{cita_id}")

        ctx.auditor.acceso_clinico(
            sujeto=identidad.sujeto, cita_id=cita_id, resultado="input_required"
        )
        ctx.auditor.invocacion(
            "registrar_motivo_consulta",
            sujeto=identidad.sujeto,
            scope=str(SCOPE),
            argumentos=argumentos,
            resultado="input_required",
        )
        return Elicit(
            redactar_propuesta(
                f"Registrar el motivo de consulta en la cita {cita_id} de "
                f"{cita['paciente']} ({cita['inicio_local']}).",
                [
                    "Se guardará el motivo de consulta asociado a la cita.",
                    "Quedará registrado quién lo anotó y cuándo, en el log de auditoría.",
                    "Se rechazará si el paciente no tiene consentimiento informado registrado.",
                ],
                [
                    "Esto es información clínica sujeta a la Resolución 2654/2019 y a la "
                    "Ley 1581 de protección de datos. Confirma solo si el paciente "
                    "autorizó el tratamiento de sus datos clínicos."
                ],
            ),
            Confirmacion,
        )

    @servidor.tool(
        name="registrar_motivo_consulta",
        title="Registrar el motivo de consulta (dato clínico)",
        description=(
            "Anota el motivo de consulta o un antecedente en una cita. Esto es DATO "
            "CLÍNICO y está regulado (Resolución 2654/2019): exige el permiso 'clinical', "
            "confirmación de una persona y consentimiento informado registrado del "
            "paciente. Si el paciente no tiene consentimiento, la operación será "
            "rechazada y NO debes insistir: pide que se registre el consentimiento "
            "primero. Transcribe lo que dice el paciente; nunca interpretes, "
            "diagnostiques ni sugieras tratamiento."
        ),
    )
    async def registrar_motivo_consulta(
        cita_id: Annotated[int, Field(gt=0)],
        motivo: Annotated[
            str,
            Field(
                min_length=3,
                max_length=500,
                description=(
                    "Lo que el paciente reporta, en sus términos. Sin diagnóstico ni "
                    "interpretación clínica."
                ),
            ),
        ],
        confirmacion: Annotated[Confirmacion, Resolve(_confirmar_motivo)],
    ) -> dict[str, Any]:
        exigir_aprobacion(confirmacion, "registrar_motivo_consulta")
        identidad = ctx.identidad()
        argumentos = {"cita_id": cita_id, "motivo": motivo}
        try:
            resultado = await ctx.cliente.enviar(
                f"/citas/{cita_id}/motivo",
                actor=identidad.sujeto,
                cuerpo={"motivo": motivo},
            )
        except Exception as error:
            codigo = getattr(error, "codigo", "ERROR_INTERNO")
            ctx.auditor.acceso_clinico(
                sujeto=identidad.sujeto, cita_id=cita_id, resultado=f"rechazado:{codigo}"
            )
            ctx.auditor.invocacion(
                "registrar_motivo_consulta",
                sujeto=identidad.sujeto,
                scope=str(SCOPE),
                argumentos=argumentos,
                resultado="error",
                codigo_error=str(codigo),
                aprobada=True,
            )
            raise

        ctx.auditor.acceso_clinico(sujeto=identidad.sujeto, cita_id=cita_id, resultado="registrado")
        ctx.auditor.invocacion(
            "registrar_motivo_consulta",
            sujeto=identidad.sujeto,
            scope=str(SCOPE),
            argumentos=argumentos,
            resultado="ok",
            aprobada=True,
        )
        return resultado
