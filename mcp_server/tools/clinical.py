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

Gate 3 is the only precondition in this server checked *after* the human
approves rather than before, which is a deliberate departure from the rule that
an operation destined to fail never reaches a person. Moving it earlier would
mean answering "has this patient consented?" on a read path, and consent status
is sensitive metadata in its own right: a pre-check that fails fast would also
enumerate, for anyone holding `clinical`, which patients have and have not
signed. A late refusal is the cheaper leak. The confirmation question warns the
approver that consent may still refuse the write, so nobody is asked to approve
something whose outcome was hidden from them. `docs/security.md`, layer 3,
carries the full argument.
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
    async def _ask_reason(
        context: Context, appointment_id: int, reason: str
    ) -> Elicit[Confirmation]:
        arguments = {"appointment_id": appointment_id, "reason": reason}
        identity = ctx.authorize_audited("record_visit_reason", SCOPE, arguments)
        require_client_that_can_confirm(context)
        async with ctx.audit_failure("record_visit_reason", SCOPE, arguments, identity):
            appointment = await ctx.client.get_object(f"/appointments/{appointment_id}")

        ctx.auditor.clinical_access(
            subject=identity.subject, appointment_id=appointment_id, result="input_required"
        )
        ctx.auditor.tool_call(
            "record_visit_reason",
            subject=identity.subject,
            scope=str(SCOPE),
            arguments=arguments,
            result="input_required",
        )
        return Elicit(
            render_question(
                f"Registrar el motivo de consulta en la cita {appointment_id} de "
                f"{appointment['patient']} ({appointment['start_local']}).",
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
        name="record_visit_reason",
        title="Record the reason for consultation (clinical data)",
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
        appointment_id: Annotated[int, Field(gt=0)],
        reason: Annotated[
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
        confirmation: Annotated[Confirmation, Resolve(_ask_reason)],
    ) -> dict[str, Any]:
        require_approval(confirmation, "record_visit_reason")
        identity = ctx.identity()
        arguments = {"appointment_id": appointment_id, "reason": reason}
        try:
            result = await ctx.client.post(
                f"/appointments/{appointment_id}/reason",
                actor=identity.subject,
                body={"reason": reason},
            )
        except Exception as error:
            code = getattr(error, "code", "INTERNAL_ERROR")
            ctx.auditor.clinical_access(
                subject=identity.subject, appointment_id=appointment_id, result=f"refused:{code}"
            )
            ctx.auditor.tool_call(
                "record_visit_reason",
                subject=identity.subject,
                scope=str(SCOPE),
                arguments=arguments,
                result="error",
                error_code=str(code),
                approved=True,
            )
            raise

        ctx.auditor.clinical_access(
            subject=identity.subject, appointment_id=appointment_id, result="recorded"
        )
        ctx.auditor.tool_call(
            "record_visit_reason",
            subject=identity.subject,
            scope=str(SCOPE),
            arguments=arguments,
            result="ok",
            approved=True,
        )
        return result
