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


def require_approval(confirmacion: Confirmation, action: str) -> None:
    """Refuse politely when the person said no.

    A declined elicitation aborts the call on its own; this covers the explicit
    `confirmado: false`, which is the same decision expressed differently.
    """
    if not confirmacion.confirmado:
        raise StructuredToolError(
            "OPERACION_NO_APROBADA",
            f"The responsible person did not approve '{action}'. Nothing was changed.",
            sugerencia=(
                "Do not retry the same operation. Ask what needs to change, or tell the "
                "patient it could not be done."
            ),
        )


async def require_valid_transition(ctx: ToolContext, cita_id: int, target: str) -> dict[str, Any]:
    """Fetch the appointment and refuse a transition that would fail.

    Without this a human is asked to approve something impossible, and only
    finds out when they say yes. The domain re-checks at the moment of effect,
    because the state can change between the two rounds.
    """
    cita = await ctx.client.get_object(f"/citas/{cita_id}")
    valid = cita.get("transiciones_validas", [])
    if target not in valid:
        raise StructuredToolError(
            "TRANSICION_INVALIDA",
            f"Appointment {cita_id} is in '{cita['estado']}' and cannot move to '{target}'.",
            sugerencia=(
                "From this state only these are valid: " + ", ".join(valid) + "."
                if valid
                else "The appointment is in a final state and accepts no more changes. "
                "If the patient needs another visit, book a new appointment."
            ),
            detalles={
                "cita_id": cita_id,
                "estado_actual": cita["estado"],
                "transiciones_validas": valid,
            },
        )
    return cita


def register(server_: MCPServer[Any], ctx: ToolContext) -> None:
    def ask(
        tool_name: str,
        identity: Identity,
        arguments: dict[str, Any],
        *,
        resumen: str,
        effects: list[str],
        advertencias: list[str] | None = None,
    ) -> Elicit[Confirmation]:
        """Record that a person was asked, then ask."""
        ctx.auditor.tool_call(
            tool_name,
            subject=identity.subject,
            scope=str(SCOPE),
            arguments=arguments,
            result="input_required",
        )
        return Elicit(render_question(resumen, effects, advertencias), Confirmation)

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
        contexto: Context,
        paciente_id: int,
        slot_id: int,
        especialidad_esperada: str | None = None,
        idempotency_key: str | None = None,
    ) -> Elicit[Confirmation]:
        arguments = {
            "paciente_id": paciente_id,
            "slot_id": slot_id,
            "especialidad_esperada": especialidad_esperada,
            "idempotency_key": idempotency_key,
        }
        identity = ctx.authorize_audited("book_appointment", SCOPE, arguments)
        require_client_that_can_confirm(contexto)
        async with ctx.audit_failure("book_appointment", SCOPE, arguments, identity):
            # Built from live data, so the person approves what will actually
            # happen. The slot is checked first: proposing to book a taken slot
            # asks someone to approve an operation that cannot succeed.
            slot = await ctx.client.get_object(
                f"/disponibilidad/{slot_id}",
                paciente_id=paciente_id,
                especialidad_esperada=especialidad_esperada,
            )
            afiliacion = await ctx.client.get_object(f"/pacientes/{paciente_id}/afiliacion")
            cartera = await ctx.client.get_object(f"/pacientes/{paciente_id}/cartera")

        advertencias: list[str] = []
        if not afiliacion["activa"]:
            advertencias.append(
                f"La afiliación al régimen {afiliacion['regimen']} está inactiva: "
                "se liquidará a tarifa particular."
            )
        if cartera["estado"] == "en_mora":
            advertencias.append(
                f"El paciente registra ${float(cartera['total_vencido']):,.0f} COP en mora "
                f"({cartera['dias_mora_maximo']} días). No impide agendar."
            )
        return ask(
            "book_appointment",
            identity,
            arguments,
            resumen=(
                f"Agendar el {slot['inicio_local']} con {slot['profesional']} "
                f"({specialty_label(slot['especialidad'])}) para el paciente {paciente_id}."
            ),
            effects=[
                "Se creará la cita, pendiente de confirmar.",
                f"El cupo del {slot['inicio_local']} quedará ocupado.",
                f"El cobro aplicable será: {afiliacion['concepto_cargo']}.",
            ],
            advertencias=advertencias,
        )

    @server_.tool(
        name="book_appointment",
        title="Agendar una cita",
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
        paciente_id: Annotated[int, Field(gt=0)],
        slot_id: Annotated[int, Field(gt=0)],
        confirmacion: Annotated[Confirmation, Resolve(_ask_book)],
        especialidad_esperada: Annotated[
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
        require_approval(confirmacion, "book_appointment")
        arguments = {
            "paciente_id": paciente_id,
            "slot_id": slot_id,
            "especialidad_esperada": especialidad_esperada,
            "idempotency_key": idempotency_key,
        }
        return executed(
            "book_appointment",
            arguments,
            await ctx.client.post("/citas", actor=ctx.identity().subject, body=arguments),
        )

    # --- confirm_appointment -------------------------------------------------- #

    async def _ask_confirm(contexto: Context, cita_id: int) -> Elicit[Confirmation]:
        arguments = {"cita_id": cita_id}
        identity = ctx.authorize_audited("confirm_appointment", SCOPE, arguments)
        require_client_that_can_confirm(contexto)
        async with ctx.audit_failure("confirm_appointment", SCOPE, arguments, identity):
            cita = await require_valid_transition(ctx, cita_id, "confirmed")
        return ask(
            "confirm_appointment",
            identity,
            arguments,
            resumen=(
                f"Confirmar la cita {cita_id} de {cita['paciente']} del {cita['inicio_local']}."
            ),
            effects=["La cita quedará confirmada."],
        )

    @server_.tool(
        name="confirm_appointment",
        title="Confirmar asistencia",
        description=(
            "Marks an appointment as confirmed by the patient, ideally 48 hours ahead. "
            "Asks a person for confirmation before applying it. Confirming protects the "
            "slot: unconfirmed appointments are the ones eligible to be released."
        ),
    )
    async def confirm_appointment(
        cita_id: Annotated[int, Field(gt=0)],
        confirmacion: Annotated[Confirmation, Resolve(_ask_confirm)],
    ) -> dict[str, Any]:
        require_approval(confirmacion, "confirm_appointment")
        return executed(
            "confirm_appointment",
            {"cita_id": cita_id},
            await ctx.client.post(f"/citas/{cita_id}/confirmar", actor=ctx.identity().subject),
        )

    # --- cancel_appointment --------------------------------------------------- #

    async def _ask_cancel(contexto: Context, cita_id: int, motivo: str) -> Elicit[Confirmation]:
        arguments = {"cita_id": cita_id, "motivo": motivo}
        identity = ctx.authorize_audited("cancel_appointment", SCOPE, arguments)
        require_client_that_can_confirm(contexto)
        async with ctx.audit_failure("cancel_appointment", SCOPE, arguments, identity):
            cita = await require_valid_transition(ctx, cita_id, "cancelled")
        return ask(
            "cancel_appointment",
            identity,
            arguments,
            resumen=(
                f"Cancelar la cita {cita_id} de {cita['paciente']} "
                f"del {cita['inicio_local']}. Motivo: {motivo}"
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
        title="Cancelar una cita",
        description=(
            "Cancels an appointment. The motivo is MANDATORY: without it the clinic "
            "cannot audit its own cancellations. Asks a person for confirmation before "
            "applying it. Once it runs the slot is released and, if someone is on the "
            "waiting list for that specialty, it says so, so they can be offered it."
        ),
    )
    async def cancel_appointment(
        cita_id: Annotated[int, Field(gt=0)],
        motivo: Annotated[
            str,
            Field(
                min_length=3,
                max_length=500,
                description="The reason the patient gave. Stored in the history.",
            ),
        ],
        confirmacion: Annotated[Confirmation, Resolve(_ask_cancel)],
    ) -> dict[str, Any]:
        require_approval(confirmacion, "cancel_appointment")
        return executed(
            "cancel_appointment",
            {"cita_id": cita_id, "motivo": motivo},
            await ctx.client.post(
                f"/citas/{cita_id}/cancelar",
                actor=ctx.identity().subject,
                body={"motivo": motivo},
            ),
        )

    # --- reschedule_appointment ------------------------------------------------ #

    async def _ask_reschedule(
        contexto: Context, cita_id: int, nuevo_slot_id: int, motivo: str | None = None
    ) -> Elicit[Confirmation]:
        arguments = {"cita_id": cita_id, "nuevo_slot_id": nuevo_slot_id, "motivo": motivo}
        identity = ctx.authorize_audited("reschedule_appointment", SCOPE, arguments)
        require_client_that_can_confirm(contexto)
        async with ctx.audit_failure("reschedule_appointment", SCOPE, arguments, identity):
            cita = await require_valid_transition(ctx, cita_id, "rescheduled")
            slot = await ctx.client.get_object(
                f"/disponibilidad/{nuevo_slot_id}",
                paciente_id=cita["paciente_id"],
                # The visit being moved must not conflict with itself.
                excluir_cita_id=cita_id,
            )
        return ask(
            "reschedule_appointment",
            identity,
            arguments,
            resumen=(
                f"Mover la cita {cita_id} de {cita['paciente']} ({cita['inicio_local']}) "
                f"al {slot['inicio_local']} con {slot['profesional']}."
            ),
            effects=[
                f"El cupo del {cita['inicio_local']} quedará libre.",
                f"El cupo del {slot['inicio_local']} quedará ocupado.",
                "Se creará una cita nueva enlazada a la original.",
            ],
        )

    @server_.tool(
        name="reschedule_appointment",
        title="Reprogramar una cita",
        description=(
            "Moves an appointment to a different slot. It has two effects at once, "
            "freeing the current slot and taking the new one, which is why it asks a "
            "person for confirmation. The original appointment ends in state "
            "'rescheduled' and a new one is created, linked back to it."
        ),
    )
    async def reschedule_appointment(
        cita_id: Annotated[int, Field(gt=0)],
        nuevo_slot_id: Annotated[int, Field(gt=0)],
        confirmacion: Annotated[Confirmation, Resolve(_ask_reschedule)],
        motivo: Annotated[str | None, Field(max_length=500)] = None,
    ) -> dict[str, Any]:
        require_approval(confirmacion, "reschedule_appointment")
        return executed(
            "reschedule_appointment",
            {"cita_id": cita_id, "nuevo_slot_id": nuevo_slot_id},
            await ctx.client.post(
                f"/citas/{cita_id}/reprogramar",
                actor=ctx.identity().subject,
                body={"nuevo_slot_id": nuevo_slot_id, "motivo": motivo},
            ),
        )

    # --- record_attendance -------------------------------------------- #

    async def _ask_attendance(contexto: Context, cita_id: int, estado: str) -> Elicit[Confirmation]:
        arguments = {"cita_id": cita_id, "estado": estado}
        identity = ctx.authorize_audited("record_attendance", SCOPE, arguments)
        require_client_that_can_confirm(contexto)
        async with ctx.audit_failure("record_attendance", SCOPE, arguments, identity):
            if estado not in ATTENDANCE_STATES:
                raise StructuredToolError(
                    "ENTRADA_INVALIDA",
                    f"'{estado}' is not an attendance state.",
                    sugerencia=f"Use one of: {', '.join(ATTENDANCE_STATES)}.",
                    detalles={"estados_validos": list(ATTENDANCE_STATES)},
                )
            cita = await require_valid_transition(ctx, cita_id, estado)

        effects = [f"La cita pasará de '{state_label(cita['estado'])}' a '{state_label(estado)}'."]
        if estado == "attended":
            effects.append("Se generará el cargo que corresponda al régimen del paciente.")
        if estado == "no_show":
            effects.append(
                "El cupo quedará libre y, si la cita estaba confirmada, se generará una "
                "penalización por inasistencia."
            )
        return ask(
            "record_attendance",
            identity,
            arguments,
            resumen=(
                f"Registrar '{state_label(estado)}' en la cita {cita_id} de {cita['paciente']}."
            ),
            effects=effects,
        )

    @server_.tool(
        name="record_attendance",
        title="Registrar asistencia",
        description=(
            "Records what happened with the appointment: 'waiting' (the patient "
            "arrived and is in the waiting room), 'attended' (it took place) or "
            "'no_show' (they did not turn up). Asks a person for confirmation, "
            "because 'attended' and 'no_show' create charges in the cartera. The "
            "valid order is scheduled -> confirmed -> waiting -> attended."
        ),
    )
    async def record_attendance(
        cita_id: Annotated[int, Field(gt=0)],
        estado: Annotated[str, Field(description="waiting | attended | no_show")],
        confirmacion: Annotated[Confirmation, Resolve(_ask_attendance)],
    ) -> dict[str, Any]:
        require_approval(confirmacion, "record_attendance")
        return executed(
            "record_attendance",
            {"cita_id": cita_id, "estado": estado},
            await ctx.client.post(
                f"/citas/{cita_id}/asistencia",
                actor=ctx.identity().subject,
                body={"estado": estado},
            ),
        )

    # --- offer_slot_to_waiting_list --------------------------------------- #

    async def _ask_offer(contexto: Context, slot_id: int) -> Elicit[Confirmation]:
        arguments = {"slot_id": slot_id}
        identity = ctx.authorize_audited("offer_slot_to_waiting_list", SCOPE, arguments)
        require_client_that_can_confirm(contexto)
        return ask(
            "offer_slot_to_waiting_list",
            identity,
            arguments,
            resumen=f"Ofrecer el cupo {slot_id} al siguiente paciente en lista de espera.",
            effects=[
                "La entrada de la lista quedará marcada como ofrecida.",
                "Se devolverá el nombre y el teléfono del paciente a contactar.",
                "NO se agenda ninguna cita: agendar es una decisión aparte.",
            ],
        )

    @server_.tool(
        name="offer_slot_to_waiting_list",
        title="Ofrecer un cupo liberado",
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
        confirmacion: Annotated[Confirmation, Resolve(_ask_offer)],
    ) -> dict[str, Any]:
        require_approval(confirmacion, "offer_slot_to_waiting_list")
        return executed(
            "offer_slot_to_waiting_list",
            {"slot_id": slot_id},
            await ctx.client.post(
                "/lista-espera/ofrecer",
                actor=ctx.identity().subject,
                body={"slot_id": slot_id},
            ),
        )
