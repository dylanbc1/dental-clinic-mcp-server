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
from mcp_server.contexto import Contexto
from mcp_server.errores import ErrorHerramienta

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
            "Encuentra un paciente por número de documento (coincidencia exacta) o por "
            "nombre (coincidencia parcial, sin distinguir mayúsculas). Úsala SIEMPRE "
            "antes de agendar o consultar: el resto de herramientas trabajan con el "
            "paciente_id que esta devuelve. Si el documento no aparece, no inventes un "
            "id: vuelve a preguntar el número al paciente."
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
            "Lista los cupos LIBRES y futuros de la agenda. Filtra por especialidad, "
            "fecha (AAAA-MM-DD) o profesional. Devuelve la hora en zona de la clínica "
            "(America/Bogota) y el slot_id que necesitas para agendar. Si no hay cupos "
            "para la fecha pedida, consulta sin fecha para ver los más próximos."
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
            "Detalle completo de una cita: estado actual, paciente, profesional, horario "
            "e historial de cambios con quién hizo cada uno y cuándo. Úsala para "
            "verificar el estado antes de proponer un cambio."
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
            "Historial de citas de un paciente, de la más reciente a la más antigua. "
            "Filtra opcionalmente por rango de fechas (AAAA-MM-DD). No incluye el motivo "
            "de consulta: eso es dato clínico y no se expone por esta vía."
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
            "Saldos pendientes del paciente, cuánto está vencido, cuántos días de mora y "
            "el desglose por antigüedad. IMPORTANTE: un saldo en mora NO impide agendar. "
            "Sirve para informar al paciente, no para negarle la cita."
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
            "Régimen de afiliación (contributivo, subsidiado, particular o SOAT) y si "
            "está vigente. Determina si el paciente paga cuota moderadora, copago o "
            "tarifa particular. Una afiliación inactiva NO impide la atención: se liquida "
            "a tarifa particular. Consúltala antes de agendar para poder informar el costo."
        ),
    )
    async def validar_afiliacion(paciente_id: Annotated[int, Field(gt=0)]) -> dict[str, Any]:
        return await _llamar(
            "validar_afiliacion",
            {"paciente_id": paciente_id},
            lambda: ctx.cliente.obtener(f"/pacientes/{paciente_id}/afiliacion"),
        )
