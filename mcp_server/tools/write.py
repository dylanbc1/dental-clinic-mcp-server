"""Write tools (scope: ``write``), each gated by human approval over MRTR.

Every tool here is one tool, not two. Calling it returns `input_required` with a
plain-language description of what would happen; the client obtains a human's
answer and retries the same call carrying it. Nothing mutates until that second
round, and the descriptions say so, because a model that believes it already
booked the appointment will tell the patient it did.

The work splits the same way in every tool:

* a **resolver** authorises, validates against live data, and asks. It runs on
  both rounds, so the checks are re-applied at the moment of effect;
* the **body** runs only once a person has approved, and does the mutation.

A refusal in the resolver never reaches the human: an unauthorised caller, or an
operation that cannot succeed, is turned away before anyone is asked to approve
it.
"""

# No `from __future__ import annotations` here on purpose: the SDK evaluates a
# tool's annotations to find its `Resolve(...)` markers, and a string annotation
# naming a closure-local resolver cannot be resolved from module globals.
from typing import Annotated, Any

from mcp.server.mcpserver import Context, Elicit, MCPServer, Resolve
from pydantic import Field

from backend.domain.errors import ErrorCode
from backend.domain.labels import specialty_label, state_label
from mcp_server.auth import Identity, Scope
from mcp_server.confirmation import (
    Confirmation,
    render_question,
    require_client_that_can_confirm,
)
from mcp_server.context import ToolContext
from mcp_server.errors import StructuredToolError

SCOPE = Scope.WRITE

ATTENDANCE_STATES = ("waiting", "attended", "no_show")


def require_approval(confirmation: Confirmation, action: str) -> None:
    """Refuse politely when the person said no.

    A declined elicitation aborts the call on its own; this covers the explicit
    `confirmado: false`, which is the same decision expressed differently.
    """
    if not confirmation.confirmed:
        raise StructuredToolError(
            ErrorCode.NOT_APPROVED,
            f"The responsible person did not approve '{action}'. Nothing was changed.",
            suggestion=(
                "Do not retry the same operation. Ask what needs to change, or tell the "
                "patient it could not be done."
            ),
        )


async def require_valid_transition(
    ctx: ToolContext, appointment_id: int, target: str
) -> dict[str, Any]:
    """Fetch the appointment and refuse a transition that would fail.

    Without this a human is asked to approve something impossible, and only
    finds out when they say yes. The domain re-checks at the moment of effect,
    because the state can change between the two rounds.
    """
    appointment = await ctx.client.get_object(f"/appointments/{appointment_id}")
    valid = appointment.get("valid_transitions", [])
    if target not in valid:
        raise StructuredToolError(
            "INVALID_TRANSITION",
            f"Appointment {appointment_id} is in '{appointment['status']}' "
            f"and cannot move to '{target}'.",
            suggestion=(
                "From this state only these are valid: " + ", ".join(valid) + "."
                if valid
                else "The appointment is in a final state and accepts no more changes. "
                "If the patient needs another visit, book a new appointment."
            ),
            details={
                "appointment_id": appointment_id,
                "current_state": appointment["status"],
                "valid_transitions": valid,
            },
        )
    return appointment


def register(server_: MCPServer[Any], ctx: ToolContext) -> None:
    def ask(
        tool_name: str,
        identity: Identity,
        arguments: dict[str, Any],
        *,
        summary: str,
        effects: list[str],
        warnings: list[str] | None = None,
    ) -> Elicit[Confirmation]:
        """Record that a person was asked, then ask."""
        ctx.auditor.tool_call(
            tool_name,
            subject=identity.subject,
            scope=str(SCOPE),
            arguments=arguments,
            result="input_required",
        )
        return Elicit(render_question(summary, effects, warnings), Confirmation)

    def executed(
        tool_name: str, arguments: dict[str, Any], result: dict[str, Any]
    ) -> dict[str, Any]:
        ctx.auditor.tool_call(
            tool_name,
            subject=ctx.identity().subject,
            scope=str(SCOPE),
            arguments=arguments,
            result="ok",
            approved=True,
        )
        return result

    # --- book_appointment ---------------------------------------------------- #

    async def _ask_book(
        context: Context,
        patient_id: int,
        slot_id: int,
        expected_specialty: str | None = None,
        idempotency_key: str | None = None,
    ) -> Elicit[Confirmation]:
        arguments = {
            "patient_id": patient_id,
            "slot_id": slot_id,
            "expected_specialty": expected_specialty,
            "idempotency_key": idempotency_key,
        }
        identity = ctx.authorize_audited("book_appointment", SCOPE, arguments)
        require_client_that_can_confirm(context)
        async with ctx.audit_failure("book_appointment", SCOPE, arguments, identity):
            # Built from live data, so the person approves what will actually
            # happen. The slot is checked first: proposing to book a taken slot
            # asks someone to approve an operation that cannot succeed.
            slot = await ctx.client.get_object(
                f"/availability/{slot_id}",
                patient_id=patient_id,
                expected_specialty=expected_specialty,
            )
            affiliation = await ctx.client.get_object(f"/patients/{patient_id}/affiliation")
            cartera = await ctx.client.get_object(f"/patients/{patient_id}/cartera")

        warnings: list[str] = []
        if not affiliation["active"]:
            warnings.append(
                f"La afiliación al régimen {affiliation['regimen']} está inactiva: "
                "se liquidará a tarifa particular."
            )
        if cartera["status"] == "en_mora":
            warnings.append(
                f"El paciente registra ${float(cartera['overdue_total']):,.0f} COP en mora "
                f"({cartera['max_overdue_days']} días). No impide agendar."
            )
        return ask(
            "book_appointment",
            identity,
            arguments,
            summary=(
                f"Agendar el {slot['start_local']} con {slot['professional']} "
                f"({specialty_label(slot['specialty'])}) para el paciente {patient_id}."
            ),
            effects=[
                "Se creará la cita, pendiente de confirmar.",
                f"El cupo del {slot['start_local']} quedará ocupado.",
                f"El cobro aplicable será: {affiliation['charge_concept']}.",
            ],
            warnings=warnings,
        )

    @server_.tool(
        name="book_appointment",
        title="Book an appointment",
        description=(
            "Books an appointment in a free slot. It does NOT book straight away: it "
            "first asks a person for confirmation, describing what will happen. Until "
            "that person approves, do not tell the patient the appointment is booked. "
            "Use the slot_id from check_availability. If the patient has an "
            "overdue cartera the confirmation warns about it, but the appointment CAN "
            "still be booked."
        ),
    )
    async def book_appointment(
        patient_id: Annotated[int, Field(gt=0)],
        slot_id: Annotated[int, Field(gt=0)],
        confirmation: Annotated[Confirmation, Resolve(_ask_book)],
        expected_specialty: Annotated[
            str | None,
            Field(description="Optional check that the slot is for the expected specialty."),
        ] = None,
        idempotency_key: Annotated[
            str | None,
            Field(
                max_length=80,
                description=(
                    "Your own identifier for this request. If you retry, reuse it: it "
                    "prevents creating a duplicate appointment."
                ),
            ),
        ] = None,
    ) -> dict[str, Any]:
        require_approval(confirmation, "book_appointment")
        arguments = {
            "patient_id": patient_id,
            "slot_id": slot_id,
            "expected_specialty": expected_specialty,
            "idempotency_key": idempotency_key,
        }
        return executed(
            "book_appointment",
            arguments,
            await ctx.client.post("/appointments", actor=ctx.identity().subject, body=arguments),
        )

    # --- confirm_appointment -------------------------------------------------- #

    async def _ask_confirm(context: Context, appointment_id: int) -> Elicit[Confirmation]:
        arguments = {"appointment_id": appointment_id}
        identity = ctx.authorize_audited("confirm_appointment", SCOPE, arguments)
        require_client_that_can_confirm(context)
        async with ctx.audit_failure("confirm_appointment", SCOPE, arguments, identity):
            appointment = await require_valid_transition(ctx, appointment_id, "confirmed")
        return ask(
            "confirm_appointment",
            identity,
            arguments,
            summary=(
                f"Confirmar la cita {appointment_id} de {appointment['patient']} "
                f"del {appointment['start_local']}."
            ),
            effects=["La cita quedará confirmada."],
        )

    @server_.tool(
        name="confirm_appointment",
        title="Confirm an appointment",
        description=(
            "Marks an appointment as confirmed by the patient, ideally 48 hours ahead. "
            "Asks a person for confirmation before applying it. Confirming protects the "
            "slot: unconfirmed appointments are the ones eligible to be released."
        ),
    )
    async def confirm_appointment(
        appointment_id: Annotated[int, Field(gt=0)],
        confirmation: Annotated[Confirmation, Resolve(_ask_confirm)],
    ) -> dict[str, Any]:
        require_approval(confirmation, "confirm_appointment")
        return executed(
            "confirm_appointment",
            {"appointment_id": appointment_id},
            await ctx.client.post(
                f"/appointments/{appointment_id}/confirm", actor=ctx.identity().subject
            ),
        )

    # --- cancel_appointment --------------------------------------------------- #

    async def _ask_cancel(
        context: Context, appointment_id: int, reason: str
    ) -> Elicit[Confirmation]:
        arguments = {"appointment_id": appointment_id, "reason": reason}
        identity = ctx.authorize_audited("cancel_appointment", SCOPE, arguments)
        require_client_that_can_confirm(context)
        async with ctx.audit_failure("cancel_appointment", SCOPE, arguments, identity):
            appointment = await require_valid_transition(ctx, appointment_id, "cancelled")
        return ask(
            "cancel_appointment",
            identity,
            arguments,
            summary=(
                f"Cancelar la cita {appointment_id} de {appointment['patient']} "
                f"del {appointment['start_local']}. Motivo: {reason}"
            ),
            effects=[
                "La cita quedará cancelada.",
                "El cupo quedará libre en la agenda.",
                "El motivo quedará registrado en el historial de la cita.",
                "Si hay lista de espera para esa especialidad, se informará al siguiente.",
            ],
        )

    @server_.tool(
        name="cancel_appointment",
        title="Cancel an appointment",
        description=(
            "Cancels an appointment. The motivo is MANDATORY: without it the clinic "
            "cannot audit its own cancellations. Asks a person for confirmation before "
            "applying it. Once it runs the slot is released and, if someone is on the "
            "waiting list for that specialty, it says so, so they can be offered it."
        ),
    )
    async def cancel_appointment(
        appointment_id: Annotated[int, Field(gt=0)],
        reason: Annotated[
            str,
            Field(
                min_length=3,
                max_length=500,
                description="The reason the patient gave. Stored in the history.",
            ),
        ],
        confirmation: Annotated[Confirmation, Resolve(_ask_cancel)],
    ) -> dict[str, Any]:
        require_approval(confirmation, "cancel_appointment")
        return executed(
            "cancel_appointment",
            {"appointment_id": appointment_id, "reason": reason},
            await ctx.client.post(
                f"/appointments/{appointment_id}/cancel",
                actor=ctx.identity().subject,
                body={"reason": reason},
            ),
        )

    # --- reschedule_appointment ------------------------------------------------ #

    async def _ask_reschedule(
        context: Context, appointment_id: int, new_slot_id: int, reason: str | None = None
    ) -> Elicit[Confirmation]:
        arguments = {"appointment_id": appointment_id, "new_slot_id": new_slot_id, "reason": reason}
        identity = ctx.authorize_audited("reschedule_appointment", SCOPE, arguments)
        require_client_that_can_confirm(context)
        async with ctx.audit_failure("reschedule_appointment", SCOPE, arguments, identity):
            appointment = await require_valid_transition(ctx, appointment_id, "rescheduled")
            slot = await ctx.client.get_object(
                f"/availability/{new_slot_id}",
                patient_id=appointment["patient_id"],
                # The visit being moved must not conflict with itself.
                exclude_appointment_id=appointment_id,
            )
        return ask(
            "reschedule_appointment",
            identity,
            arguments,
            summary=(
                f"Mover la cita {appointment_id} de {appointment['patient']} "
                f"({appointment['start_local']}) "
                f"al {slot['start_local']} con {slot['professional']}."
            ),
            effects=[
                f"El cupo del {appointment['start_local']} quedará libre.",
                f"El cupo del {slot['start_local']} quedará ocupado.",
                "Se creará una cita nueva enlazada a la original.",
            ],
        )

    @server_.tool(
        name="reschedule_appointment",
        title="Reschedule an appointment",
        description=(
            "Moves an appointment to a different slot. It has two effects at once, "
            "freeing the current slot and taking the new one, which is why it asks a "
            "person for confirmation. The original appointment ends in state "
            "'rescheduled' and a new one is created, linked back to it."
        ),
    )
    async def reschedule_appointment(
        appointment_id: Annotated[int, Field(gt=0)],
        new_slot_id: Annotated[int, Field(gt=0)],
        confirmation: Annotated[Confirmation, Resolve(_ask_reschedule)],
        reason: Annotated[str | None, Field(max_length=500)] = None,
    ) -> dict[str, Any]:
        require_approval(confirmation, "reschedule_appointment")
        return executed(
            "reschedule_appointment",
            {"appointment_id": appointment_id, "new_slot_id": new_slot_id},
            await ctx.client.post(
                f"/appointments/{appointment_id}/reschedule",
                actor=ctx.identity().subject,
                body={"new_slot_id": new_slot_id, "reason": reason},
            ),
        )

    # --- record_attendance -------------------------------------------- #

    async def _ask_attendance(
        context: Context, appointment_id: int, status: str
    ) -> Elicit[Confirmation]:
        arguments = {"appointment_id": appointment_id, "status": status}
        identity = ctx.authorize_audited("record_attendance", SCOPE, arguments)
        require_client_that_can_confirm(context)
        async with ctx.audit_failure("record_attendance", SCOPE, arguments, identity):
            if status not in ATTENDANCE_STATES:
                raise StructuredToolError(
                    "INVALID_INPUT",
                    f"'{status}' is not an attendance state.",
                    suggestion=f"Use one of: {', '.join(ATTENDANCE_STATES)}.",
                    details={"valid_states": list(ATTENDANCE_STATES)},
                )
            appointment = await require_valid_transition(ctx, appointment_id, status)

        effects = [
            f"La cita pasará de '{state_label(appointment['status'])}' a '{state_label(status)}'."
        ]
        if status == "attended":
            effects.append("Se generará el cargo que corresponda al régimen del paciente.")
        if status == "no_show":
            effects.append(
                "El cupo quedará libre y, si la cita estaba confirmada, se generará una "
                "penalización por inasistencia."
            )
        return ask(
            "record_attendance",
            identity,
            arguments,
            summary=(
                f"Registrar '{state_label(status)}' en la cita {appointment_id} "
                f"de {appointment['patient']}."
            ),
            effects=effects,
        )

    @server_.tool(
        name="record_attendance",
        title="Record attendance",
        description=(
            "Records what happened with the appointment: 'waiting' (the patient "
            "arrived and is in the waiting room), 'attended' (it took place) or "
            "'no_show' (they did not turn up). Asks a person for confirmation, "
            "because 'attended' and 'no_show' create charges in the cartera. The "
            "valid order is scheduled -> confirmed -> waiting -> attended."
        ),
    )
    async def record_attendance(
        appointment_id: Annotated[int, Field(gt=0)],
        status: Annotated[str, Field(description="waiting | attended | no_show")],
        confirmation: Annotated[Confirmation, Resolve(_ask_attendance)],
    ) -> dict[str, Any]:
        require_approval(confirmation, "record_attendance")
        return executed(
            "record_attendance",
            {"appointment_id": appointment_id, "status": status},
            await ctx.client.post(
                f"/appointments/{appointment_id}/attendance",
                actor=ctx.identity().subject,
                body={"status": status},
            ),
        )

    # --- offer_slot_to_waiting_list --------------------------------------- #

    async def _ask_offer(context: Context, slot_id: int) -> Elicit[Confirmation]:
        arguments = {"slot_id": slot_id}
        identity = ctx.authorize_audited("offer_slot_to_waiting_list", SCOPE, arguments)
        require_client_that_can_confirm(context)
        return ask(
            "offer_slot_to_waiting_list",
            identity,
            arguments,
            summary=f"Ofrecer el cupo {slot_id} al siguiente paciente en lista de espera.",
            effects=[
                "La entrada de la lista quedará marcada como ofrecida.",
                "Se devolverá el nombre y el teléfono del paciente a contactar.",
                "NO se agenda ninguna cita: agendar es una decisión aparte.",
            ],
        )

    @server_.tool(
        name="offer_slot_to_waiting_list",
        title="Offer a freed slot",
        description=(
            "Offers a free slot to the next patient on the waiting list for that "
            "specialty. Urgent cases jump the queue; within the same priority it is "
            "first come, first served. It does NOT book the appointment: it returns who "
            "to contact and their phone number. Asks a person for confirmation, because "
            "it means contacting someone."
        ),
    )
    async def offer_slot_to_waiting_list(
        slot_id: Annotated[int, Field(gt=0)],
        confirmation: Annotated[Confirmation, Resolve(_ask_offer)],
    ) -> dict[str, Any]:
        require_approval(confirmation, "offer_slot_to_waiting_list")
        return executed(
            "offer_slot_to_waiting_list",
            {"slot_id": slot_id},
            await ctx.client.post(
                "/waiting-list/offer",
                actor=ctx.identity().subject,
                body={"slot_id": slot_id},
            ),
        )
