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
        peticion: Callable[[], Awaitable[T]],
    ) -> T:
        """Authorise, then call.

        The request is a *thunk*, not an already-created coroutine: building it
        eagerly leaves an un-awaited coroutine behind every time the scope check
        denies the call.
        """
        identity = ctx.authorize(tool_name, SCOPE)
        try:
            result = await peticion()
        except StructuredToolError as error:
            ctx.auditor.tool_call(
                tool_name,
                subject=identity.subject,
                scope=str(SCOPE),
                arguments=arguments,
                result="error",
                error_code=error.codigo,
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
        name="buscar_paciente",
        title="Buscar paciente",
        description=(
            "Finds a patient by documento (exact match) or by name (partial, "
            "case-insensitive). ALWAYS use it before booking or looking anything up: "
            "every other tool works with the paciente_id this one returns. If the "
            "documento does not turn up, do not invent an id, ask the patient for the "
            "number again."
        ),
    )
    async def search_patients_route(
        documento: Annotated[
            str | None, Field(description="Número de documento, sin puntos ni guiones.")
        ] = None,
        nombre: Annotated[
            str | None, Field(description="Nombre o apellido, completo o parcial.")
        ] = None,
        limite: Annotated[int, Field(ge=1, le=25)] = 10,
    ) -> list[dict[str, Any]]:
        return await _call(
            "buscar_paciente",
            {"documento": documento, "nombre": nombre, "limite": limite},
            lambda: ctx.client.get_list(
                "/pacientes", documento=documento, nombre=nombre, limite=limite
            ),
        )

    @server_.tool(
        name="consultar_disponibilidad",
        title="Consultar cupos disponibles",
        description=(
            "Lists the FREE, future slots in the agenda. Filter by specialty, date "
            "(YYYY-MM-DD) or professional. Returns the time in the clinic's timezone "
            "(America/Bogota) and the slot_id you need in order to book. If there are "
            "no slots on the date asked for, query without a date to see the nearest "
            "ones."
        ),
    )
    async def list_available_slots(
        especialidad: Annotated[
            str | None,
            Field(
                description=(
                    "odontologia_general | ortodoncia | endodoncia | periodoncia | odontopediatria"
                )
            ),
        ] = None,
        fecha: Annotated[str | None, Field(description="AAAA-MM-DD")] = None,
        profesional_id: int | None = None,
        limite: Annotated[int, Field(ge=1, le=25)] = 10,
    ) -> list[dict[str, Any]]:
        return await _call(
            "consultar_disponibilidad",
            {
                "especialidad": especialidad,
                "fecha": fecha,
                "profesional_id": profesional_id,
                "limite": limite,
            },
            lambda: ctx.client.get_list(
                "/disponibilidad",
                especialidad=especialidad,
                fecha=fecha,
                profesional_id=profesional_id,
                limite=limite,
            ),
        )

    @server_.tool(
        name="consultar_cita",
        title="Consultar una cita",
        description=(
            "Full detail of one appointment: current state, patient, professional, "
            "time, and the change history with who did what and when. It also returns "
            "transiciones_validas, which tells you exactly what the appointment can "
            "become next. Use it to check the state before attempting a change."
        ),
    )
    async def appointment_by_id(cita_id: Annotated[int, Field(gt=0)]) -> dict[str, Any]:
        return await _call(
            "consultar_cita",
            {"cita_id": cita_id},
            lambda: ctx.client.get_object(f"/citas/{cita_id}"),
        )

    @server_.tool(
        name="listar_citas_paciente",
        title="Listar las citas de un paciente",
        description=(
            "A patient's appointment history, most recent first. Optionally filtered by "
            "date range (YYYY-MM-DD). It does not include the reason for consultation: "
            "that is clinical data and is not exposed through this route."
        ),
    )
    async def list_patient_appointments(
        paciente_id: Annotated[int, Field(gt=0)],
        desde: Annotated[str | None, Field(description="AAAA-MM-DD")] = None,
        hasta: Annotated[str | None, Field(description="AAAA-MM-DD")] = None,
        limite: Annotated[int, Field(ge=1, le=50)] = 20,
    ) -> list[dict[str, Any]]:
        return await _call(
            "listar_citas_paciente",
            {"paciente_id": paciente_id, "desde": desde, "hasta": hasta, "limite": limite},
            lambda: ctx.client.get_list(
                f"/pacientes/{paciente_id}/citas", desde=desde, hasta=hasta, limite=limite
            ),
        )

    @server_.tool(
        name="consultar_cartera",
        title="Consultar la cartera del paciente",
        description=(
            "The patient's outstanding cartera: what is overdue, how many days late, "
            "and the ageing breakdown. IMPORTANT: an overdue cartera does NOT prevent "
            "booking. It is there to inform the patient, not to refuse them an "
            "appointment."
        ),
    )
    async def get_cartera(paciente_id: Annotated[int, Field(gt=0)]) -> dict[str, Any]:
        return await _call(
            "consultar_cartera",
            {"paciente_id": paciente_id},
            lambda: ctx.client.get_object(f"/pacientes/{paciente_id}/cartera"),
        )

    @server_.tool(
        name="validar_afiliacion",
        title="Validar la afiliación del paciente",
        description=(
            "The patient's régimen de afiliación (contributivo, subsidiado, particular "
            "or SOAT) and whether it is active. It determines whether they pay a cuota "
            "moderadora, a copago, or the private tariff. An inactive afiliación does "
            "NOT prevent care: it is billed at the private tariff. Check it before "
            "booking so you can tell the patient the cost."
        ),
    )
    async def validate_afiliacion(paciente_id: Annotated[int, Field(gt=0)]) -> dict[str, Any]:
        return await _call(
            "validar_afiliacion",
            {"paciente_id": paciente_id},
            lambda: ctx.client.get_object(f"/pacientes/{paciente_id}/afiliacion"),
        )
