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
from mcp_server.context import Contexto
from mcp_server.errors import ErrorHerramienta

SCOPE = Scope.READ

T = TypeVar("T")


def registrar(servidor: MCPServer[Any], ctx: Contexto) -> None:
    async def _llamar(
        herramienta: str,
        argumentos: dict[str, Any],
        peticion: Callable[[], Awaitable[T]],
    ) -> T:
        """Authorise, then call.

        The request is a *thunk*, not an already-created coroutine: building it
        eagerly leaves an un-awaited coroutine behind every time the scope check
        denies the call.
        """
        identidad = ctx.autorizar(herramienta, SCOPE)
        try:
            resultado = await peticion()
        except ErrorHerramienta as error:
            ctx.auditor.invocacion(
                herramienta,
                sujeto=identidad.sujeto,
                scope=str(SCOPE),
                argumentos=argumentos,
                resultado="error",
                codigo_error=error.codigo,
            )
            raise
        ctx.auditor.invocacion(
            herramienta,
            sujeto=identidad.sujeto,
            scope=str(SCOPE),
            argumentos=argumentos,
            resultado="ok",
        )
        return resultado

    @servidor.tool(
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
    async def buscar_paciente(
        documento: Annotated[
            str | None, Field(description="Número de documento, sin puntos ni guiones.")
        ] = None,
        nombre: Annotated[
            str | None, Field(description="Nombre o apellido, completo o parcial.")
        ] = None,
        limite: Annotated[int, Field(ge=1, le=25)] = 10,
    ) -> list[dict[str, Any]]:
        return await _llamar(
            "buscar_paciente",
            {"documento": documento, "nombre": nombre, "limite": limite},
            lambda: ctx.cliente.listar(
                "/pacientes", documento=documento, nombre=nombre, limite=limite
            ),
        )

    @servidor.tool(
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
    async def consultar_disponibilidad(
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
        return await _llamar(
            "consultar_disponibilidad",
            {
                "especialidad": especialidad,
                "fecha": fecha,
                "profesional_id": profesional_id,
                "limite": limite,
            },
            lambda: ctx.cliente.listar(
                "/disponibilidad",
                especialidad=especialidad,
                fecha=fecha,
                profesional_id=profesional_id,
                limite=limite,
            ),
        )

    @servidor.tool(
        name="consultar_cita",
        title="Consultar una cita",
        description=(
            "Full detail of one appointment: current state, patient, professional, "
            "time, and the change history with who did what and when. It also returns "
            "transiciones_validas, which tells you exactly what the appointment can "
            "become next. Use it to check the state before attempting a change."
        ),
    )
    async def consultar_cita(cita_id: Annotated[int, Field(gt=0)]) -> dict[str, Any]:
        return await _llamar(
            "consultar_cita",
            {"cita_id": cita_id},
            lambda: ctx.cliente.obtener(f"/citas/{cita_id}"),
        )

    @servidor.tool(
        name="listar_citas_paciente",
        title="Listar las citas de un paciente",
        description=(
            "A patient's appointment history, most recent first. Optionally filtered by "
            "date range (YYYY-MM-DD). It does not include the reason for consultation: "
            "that is clinical data and is not exposed through this route."
        ),
    )
    async def listar_citas_paciente(
        paciente_id: Annotated[int, Field(gt=0)],
        desde: Annotated[str | None, Field(description="AAAA-MM-DD")] = None,
        hasta: Annotated[str | None, Field(description="AAAA-MM-DD")] = None,
        limite: Annotated[int, Field(ge=1, le=50)] = 20,
    ) -> list[dict[str, Any]]:
        return await _llamar(
            "listar_citas_paciente",
            {"paciente_id": paciente_id, "desde": desde, "hasta": hasta, "limite": limite},
            lambda: ctx.cliente.listar(
                f"/pacientes/{paciente_id}/citas", desde=desde, hasta=hasta, limite=limite
            ),
        )

    @servidor.tool(
        name="consultar_cartera",
        title="Consultar la cartera del paciente",
        description=(
            "The patient's outstanding cartera: what is overdue, how many days late, "
            "and the ageing breakdown. IMPORTANT: an overdue cartera does NOT prevent "
            "booking. It is there to inform the patient, not to refuse them an "
            "appointment."
        ),
    )
    async def consultar_cartera(paciente_id: Annotated[int, Field(gt=0)]) -> dict[str, Any]:
        return await _llamar(
            "consultar_cartera",
            {"paciente_id": paciente_id},
            lambda: ctx.cliente.obtener(f"/pacientes/{paciente_id}/cartera"),
        )

    @servidor.tool(
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
    async def validar_afiliacion(paciente_id: Annotated[int, Field(gt=0)]) -> dict[str, Any]:
        return await _llamar(
            "validar_afiliacion",
            {"paciente_id": paciente_id},
            lambda: ctx.cliente.obtener(f"/pacientes/{paciente_id}/afiliacion"),
        )
