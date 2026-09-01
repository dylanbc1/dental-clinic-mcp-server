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
from mcp_server.confirmation import (
    Confirmation,
    render_question,
    require_client_that_can_confirm,
)
from mcp_server.context import ToolContext
from mcp_server.tools.write import require_approval

SCOPE = Scope.CLINICAL


def register(server_: MCPServer[Any], ctx: ToolContext) -> None:
    async def _ask_reason(contexto: Context, cita_id: int, motivo: str) -> Elicit[Confirmation]:
        arguments = {"cita_id": cita_id, "motivo": motivo}
        identity = ctx.authorize_audited("registrar_motivo_consulta", SCOPE, arguments)
        require_client_that_can_confirm(contexto)
        async with ctx.audit_failure("registrar_motivo_consulta", SCOPE, arguments, identity):
            cita = await ctx.client.get_object(f"/citas/{cita_id}")

        ctx.auditor.clinical_access(
            subject=identity.subject, cita_id=cita_id, result="input_required"
        )
        ctx.auditor.tool_call(
            "registrar_motivo_consulta",
            subject=identity.subject,
            scope=str(SCOPE),
            arguments=arguments,
            result="input_required",
        )
        return Elicit(
            render_question(
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
            Confirmation,
        )

    @server_.tool(
        name="registrar_motivo_consulta",
        title="Registrar el motivo de consulta (dato clínico)",
        description=(
            "Records the reason for consultation, or a relevant history note, on an "
            "appointment. This is CLINICAL DATA and it is regulated (Resolución "
            "2654/2019): it requires the 'clinical' permission, a person's confirmation, "
            "and the patient's informed consent on file. If the patient has no consent "
            "recorded the operation is refused and you must NOT insist: ask for consent "
            "to be recorded first. Transcribe what the patient says; never interpret, "
            "diagnose, or suggest treatment."
        ),
    )
    async def record_visit_reason(
        cita_id: Annotated[int, Field(gt=0)],
        motivo: Annotated[
            str,
            Field(
                min_length=3,
                max_length=500,
                description=(
                    "What the patient reports, in their own words. No diagnosis and no "
                    "clinical interpretation."
                ),
            ),
        ],
        confirmacion: Annotated[Confirmation, Resolve(_ask_reason)],
    ) -> dict[str, Any]:
        require_approval(confirmacion, "registrar_motivo_consulta")
        identity = ctx.identity()
        arguments = {"cita_id": cita_id, "motivo": motivo}
        try:
            result = await ctx.client.post(
                f"/citas/{cita_id}/motivo",
                actor=identity.subject,
                body={"motivo": motivo},
            )
        except Exception as error:
            codigo = getattr(error, "codigo", "ERROR_INTERNO")
            ctx.auditor.clinical_access(
                subject=identity.subject, cita_id=cita_id, result=f"rechazado:{codigo}"
            )
            ctx.auditor.tool_call(
                "registrar_motivo_consulta",
                subject=identity.subject,
                scope=str(SCOPE),
                arguments=arguments,
                result="error",
                error_code=str(codigo),
                approved=True,
            )
            raise

        ctx.auditor.clinical_access(subject=identity.subject, cita_id=cita_id, result="registrado")
        ctx.auditor.tool_call(
            "registrar_motivo_consulta",
            subject=identity.subject,
            scope=str(SCOPE),
            arguments=arguments,
            result="ok",
            approved=True,
        )
        return result
