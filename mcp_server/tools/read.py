"""Read tools (scope: ``read``).

Six lookups, no side effects, no approval gate. The descriptions are written for
the model rather than for a developer: each one says when to reach for the tool
and what it will get back, because a tool the model picks wrongly is worse than
a tool it does not have.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, TypeVar

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from mcp_server.auth import Scope
from mcp_server.context import ToolContext
from mcp_server.errors import StructuredToolError

SCOPE = Scope.READ

T = TypeVar("T")


def register(server_: MCPServer[Any], ctx: ToolContext) -> None:
    async def _call(
        tool_name: str,
        arguments: dict[str, Any],
        request: Callable[[], Awaitable[T]],
    ) -> T:
        """Authorise, then call.

        The request is a *thunk*, not an already-created coroutine: building it
        eagerly leaves an un-awaited coroutine behind every time the scope check
        denies the call.
        """
        identity = ctx.authorize(tool_name, SCOPE)
        try:
            result = await request()
        except StructuredToolError as error:
            ctx.auditor.tool_call(
                tool_name,
                subject=identity.subject,
                scope=str(SCOPE),
                arguments=arguments,
                result="error",
                error_code=error.code,
            )
            raise
        ctx.auditor.tool_call(
            tool_name,
            subject=identity.subject,
            scope=str(SCOPE),
            arguments=arguments,
            result="ok",
        )
        return result

    @server_.tool(
        name="search_patients",
        title="Find a patient",
        description=(
            "Finds a patient by document number (exact match) or by name (partial, "
            "case-insensitive). ALWAYS use it before booking or looking anything up: "
            "every other tool works with the patient_id this one returns. If the "
            "document number does not turn up, do not invent an id, ask the patient "
            "for the number again."
        ),
    )
    async def search_patients(
        document_number: Annotated[
            str | None, Field(description="Document number, no dots and no dashes.")
        ] = None,
        name: Annotated[
            str | None, Field(description="Given name or surname, whole or partial.")
        ] = None,
        limit: Annotated[int, Field(ge=1, le=25)] = 10,
    ) -> list[dict[str, Any]]:
        return await _call(
            "search_patients",
            {"document_number": document_number, "name": name, "limit": limit},
            lambda: ctx.client.get_list(
                "/patients", document_number=document_number, name=name, limit=limit
            ),
        )

    @server_.tool(
        name="check_availability",
        title="Check available slots",
        description=(
            "Lists the FREE, future slots in the agenda. Filter by specialty, date "
            "(YYYY-MM-DD) or professional. Returns the time in the clinic's timezone "
            "(America/Bogota) and the slot_id you need in order to book. If there are "
            "no slots on the date asked for, query without a date to see the nearest "
            "ones."
        ),
    )
    async def check_availability(
        specialty: Annotated[
            str | None,
            Field(
                description=(
                    "general_dentistry | orthodontics | endodontics | "
                    "periodontics | pediatric_dentistry"
                )
            ),
        ] = None,
        day: Annotated[str | None, Field(description="YYYY-MM-DD")] = None,
        professional_id: int | None = None,
        limit: Annotated[int, Field(ge=1, le=25)] = 10,
    ) -> list[dict[str, Any]]:
        return await _call(
            "check_availability",
            {
                "specialty": specialty,
                "day": day,
                "professional_id": professional_id,
                "limit": limit,
            },
            lambda: ctx.client.get_list(
                "/availability",
                specialty=specialty,
                day=day,
                professional_id=professional_id,
                limit=limit,
            ),
        )

    @server_.tool(
        name="get_appointment",
        title="Look up an appointment",
        description=(
            "Full detail of one appointment: current state, patient, professional, "
            "time, and the change history with who did what and when. It also returns "
            "transiciones_validas, which tells you exactly what the appointment can "
            "become next. Use it to check the state before attempting a change."
        ),
    )
    async def get_appointment(appointment_id: Annotated[int, Field(gt=0)]) -> dict[str, Any]:
        return await _call(
            "get_appointment",
            {"appointment_id": appointment_id},
            lambda: ctx.client.get_object(f"/appointments/{appointment_id}"),
        )

    @server_.tool(
        name="list_patient_appointments",
        title="List a patient's appointments",
        description=(
            "A patient's appointment history, most recent first. Optionally filtered by "
            "date range (YYYY-MM-DD). It does not include the reason for consultation: "
            "that is clinical data and is not exposed through this route."
        ),
    )
    async def list_patient_appointments(
        patient_id: Annotated[int, Field(gt=0)],
        since: Annotated[str | None, Field(description="YYYY-MM-DD")] = None,
        until: Annotated[str | None, Field(description="YYYY-MM-DD")] = None,
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
    ) -> list[dict[str, Any]]:
        return await _call(
            "list_patient_appointments",
            {"patient_id": patient_id, "since": since, "until": until, "limit": limit},
            lambda: ctx.client.get_list(
                f"/patients/{patient_id}/appointments", since=since, until=until, limit=limit
            ),
        )

    @server_.tool(
        name="check_cartera",
        title="Check the patient's cartera",
        description=(
            "The patient's outstanding cartera: what is overdue, how many days late, "
            "and the ageing breakdown. IMPORTANT: an overdue cartera does NOT prevent "
            "booking. It is there to inform the patient, not to refuse them an "
            "appointment."
        ),
    )
    async def check_cartera(patient_id: Annotated[int, Field(gt=0)]) -> dict[str, Any]:
        return await _call(
            "check_cartera",
            {"patient_id": patient_id},
            lambda: ctx.client.get_object(f"/patients/{patient_id}/cartera"),
        )

    @server_.tool(
        name="validate_affiliation",
        title="Validate the patient's affiliation",
        description=(
            "The patient's régimen de afiliación (contributivo, subsidiado, particular "
            "or SOAT) and whether it is active. It determines whether they pay a cuota "
            "moderadora, a copago, or the private tariff. An inactive afiliación does "
            "NOT prevent care: it is billed at the private tariff. Check it before "
            "booking so you can tell the patient the cost."
        ),
    )
    async def validate_affiliation(patient_id: Annotated[int, Field(gt=0)]) -> dict[str, Any]:
        return await _call(
            "validate_affiliation",
            {"patient_id": patient_id},
            lambda: ctx.client.get_object(f"/patients/{patient_id}/affiliation"),
        )
