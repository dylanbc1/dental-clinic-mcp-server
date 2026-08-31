"""Write tools (scope: ``write``), all gated by human approval.

Every tool here is two-phase. Calling it **proposes**; nothing changes until a
human approves and `confirmar_operacion` runs the proposal. The descriptions say
so explicitly, because a model that believes it already booked the appointment
will tell the patient it did.

The mapping from a proposal back to the action that executes it lives in
:data:`EJECUTORES`, keyed by action name. A confirmation token names its action,
so a token minted for `cancelar_cita` cannot be redeemed to run `agendar_cita`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from mcp_server.auth import Scope
from mcp_server.contexto import Contexto
from mcp_server.errores import ErrorHerramienta

SCOPE = Scope.WRITE

Ejecutor = Callable[[Contexto, str, dict[str, Any]], Awaitable[dict[str, Any]]]


# --------------------------------------------------------------------------- #
# What each approved action actually does
# --------------------------------------------------------------------------- #


async def _ejecutar_agendar(ctx: Contexto, actor: str, args: dict[str, Any]) -> dict[str, Any]:
    cuerpo = {
        "paciente_id": args["paciente_id"],
        "slot_id": args["slot_id"],
        "especialidad_esperada": args.get("especialidad_esperada"),
        "idempotency_key": args.get("idempotency_key"),
    }
    return await ctx.cliente.enviar("/citas", actor=actor, cuerpo=cuerpo)


async def _ejecutar_confirmar(ctx: Contexto, actor: str, args: dict[str, Any]) -> dict[str, Any]:
    return await ctx.cliente.enviar(f"/citas/{args['cita_id']}/confirmar", actor=actor)


async def _ejecutar_cancelar(ctx: Contexto, actor: str, args: dict[str, Any]) -> dict[str, Any]:
    return await ctx.cliente.enviar(
        f"/citas/{args['cita_id']}/cancelar", actor=actor, cuerpo={"motivo": args["motivo"]}
    )


async def _ejecutar_reprogramar(ctx: Contexto, actor: str, args: dict[str, Any]) -> dict[str, Any]:
    return await ctx.cliente.enviar(
        f"/citas/{args['cita_id']}/reprogramar",
        actor=actor,
        cuerpo={"nuevo_slot_id": args["nuevo_slot_id"], "motivo": args.get("motivo")},
    )


async def _ejecutar_asistencia(ctx: Contexto, actor: str, args: dict[str, Any]) -> dict[str, Any]:
    return await ctx.cliente.enviar(
        f"/citas/{args['cita_id']}/asistencia", actor=actor, cuerpo={"estado": args["estado"]}
    )


async def _ejecutar_ofrecer(ctx: Contexto, actor: str, args: dict[str, Any]) -> dict[str, Any]:
    return await ctx.cliente.enviar(
        "/lista-espera/ofrecer", actor=actor, cuerpo={"slot_id": args["slot_id"]}
    )


async def _ejecutar_motivo(ctx: Contexto, actor: str, args: dict[str, Any]) -> dict[str, Any]:
    return await ctx.cliente.enviar(
        f"/citas/{args['cita_id']}/motivo", actor=actor, cuerpo={"motivo": args["motivo"]}
    )


#: Action name → the coroutine that performs it once approved.
EJECUTORES: dict[str, Ejecutor] = {
    "agendar_cita": _ejecutar_agendar,
    "confirmar_cita": _ejecutar_confirmar,
    "cancelar_cita": _ejecutar_cancelar,
    "reprogramar_cita": _ejecutar_reprogramar,
    "registrar_asistencia": _ejecutar_asistencia,
    "ofrecer_cupo_lista_espera": _ejecutar_ofrecer,
    "registrar_motivo_consulta": _ejecutar_motivo,
}

#: Which scope each action needs. Checked again at confirmation time: a token
#: minted while the caller held `clinical` must not execute if that scope is
#: gone by the time it is redeemed.
SCOPE_DE_ACCION: dict[str, Scope] = {
    "agendar_cita": Scope.WRITE,
    "confirmar_cita": Scope.WRITE,
    "cancelar_cita": Scope.WRITE,
    "reprogramar_cita": Scope.WRITE,
    "registrar_asistencia": Scope.WRITE,
    "ofrecer_cupo_lista_espera": Scope.WRITE,
    "registrar_motivo_consulta": Scope.CLINICAL,
}


ESTADOS_ASISTENCIA = ("en_espera", "atendida", "no_asistio")


def registrar(servidor: MCPServer[Any], ctx: Contexto) -> None:
    @servidor.tool(
        name="agendar_cita",
        title="Agendar una cita (requiere confirmación)",
        description=(
            "PROPONE agendar una cita en un cupo libre. NO agenda nada por sí sola: "
            "devuelve un resumen y un token_confirmacion que un humano debe aprobar "
            "llamando confirmar_operacion. Hasta entonces no digas al paciente que la "
            "cita quedó agendada. Usa el slot_id de consultar_disponibilidad. Si el "
            "paciente tiene saldo en mora la propuesta lo advierte, pero la cita SÍ se "
            "puede agendar."
        ),
    )
    async def agendar_cita(
        paciente_id: Annotated[int, Field(gt=0)],
        slot_id: Annotated[int, Field(gt=0)],
        especialidad_esperada: Annotated[
            str | None,
            Field(description="Verificación opcional de que el cupo es de la especialidad."),
        ] = None,
        idempotency_key: Annotated[
            str | None,
            Field(
                max_length=80,
                description=(
                    "Identificador propio de esta solicitud. Si reintentas, reutilízalo: "
                    "evita crear una cita duplicada."
                ),
            ),
        ] = None,
    ) -> dict[str, Any]:
        identidad = ctx.autorizar("agendar_cita", SCOPE)
        argumentos = {
            "paciente_id": paciente_id,
            "slot_id": slot_id,
            "especialidad_esperada": especialidad_esperada,
            "idempotency_key": idempotency_key,
        }

        # Built from live data, so the human approves what will actually
        # happen, not what the model believed a few turns ago.
        paciente = await ctx.cliente.obtener(f"/pacientes/{paciente_id}/afiliacion")
        cartera = await ctx.cliente.obtener(f"/pacientes/{paciente_id}/cartera")

        advertencias: list[str] = []
        if not paciente["activa"]:
            advertencias.append(
                f"La afiliación al régimen {paciente['regimen']} está inactiva: "
                "se liquidará a tarifa particular."
            )
        if cartera["estado"] == "en_mora":
            advertencias.append(
                f"El paciente registra ${float(cartera['total_vencido']):,.0f} COP en mora "
                f"({cartera['dias_mora_maximo']} días). No impide agendar."
            )

        ctx.auditor.invocacion(
            "agendar_cita",
            sujeto=identidad.sujeto,
            scope=str(SCOPE),
            argumentos=argumentos,
            resultado="propuesta",
        )
        return ctx.proponer(
            "agendar_cita",
            argumentos,
            resumen=f"Agendar el cupo {slot_id} para el paciente {paciente_id}.",
            efectos=[
                "Se creará una cita en estado 'agendada'.",
                "El cupo quedará ocupado y dejará de aparecer como disponible.",
                f"El cobro aplicable será: {paciente['concepto_cargo']}.",
            ],
            sujeto=identidad.sujeto,
            advertencias=advertencias,
        )

    @servidor.tool(
        name="confirmar_cita",
        title="Confirmar asistencia (requiere confirmación)",
        description=(
            "PROPONE marcar una cita como confirmada por el paciente, idealmente 48 horas "
            "antes. Requiere aprobación humana vía confirmar_operacion. Confirmar protege "
            "el cupo: las citas sin confirmar son las candidatas a liberarse."
        ),
    )
    async def confirmar_cita(cita_id: Annotated[int, Field(gt=0)]) -> dict[str, Any]:
        identidad = ctx.autorizar("confirmar_cita", SCOPE)
        cita = await ctx.cliente.obtener(f"/citas/{cita_id}")
        ctx.auditor.invocacion(
            "confirmar_cita",
            sujeto=identidad.sujeto,
            scope=str(SCOPE),
            argumentos={"cita_id": cita_id},
            resultado="propuesta",
        )
        return ctx.proponer(
            "confirmar_cita",
            {"cita_id": cita_id},
            resumen=(
                f"Confirmar la cita {cita_id} de {cita['paciente']} del {cita['inicio_local']}."
            ),
            efectos=[f"La cita pasará de '{cita['estado']}' a 'confirmada'."],
            sujeto=identidad.sujeto,
        )

    @servidor.tool(
        name="cancelar_cita",
        title="Cancelar una cita (requiere confirmación)",
        description=(
            "PROPONE cancelar una cita. El motivo es OBLIGATORIO: sin él la clínica no "
            "puede auditar sus cancelaciones. Requiere aprobación humana. Al ejecutarse "
            "libera el cupo y, si hay alguien en lista de espera para esa especialidad, "
            "lo informa para que se le ofrezca."
        ),
    )
    async def cancelar_cita(
        cita_id: Annotated[int, Field(gt=0)],
        motivo: Annotated[
            str,
            Field(
                min_length=3,
                max_length=500,
                description="Razón que dio el paciente. Se guarda en el historial.",
            ),
        ],
    ) -> dict[str, Any]:
        identidad = ctx.autorizar("cancelar_cita", SCOPE)
        cita = await ctx.cliente.obtener(f"/citas/{cita_id}")
        ctx.auditor.invocacion(
            "cancelar_cita",
            sujeto=identidad.sujeto,
            scope=str(SCOPE),
            argumentos={"cita_id": cita_id, "motivo": motivo},
            resultado="propuesta",
        )
        return ctx.proponer(
            "cancelar_cita",
            {"cita_id": cita_id, "motivo": motivo},
            resumen=(
                f"Cancelar la cita {cita_id} de {cita['paciente']} del {cita['inicio_local']}."
            ),
            efectos=[
                f"La cita pasará de '{cita['estado']}' a 'cancelada'.",
                "El cupo quedará libre en la agenda.",
                "El motivo quedará registrado en el historial de la cita.",
                "Si hay lista de espera para esa especialidad, se informará al siguiente.",
            ],
            sujeto=identidad.sujeto,
        )

    @servidor.tool(
        name="reprogramar_cita",
        title="Reprogramar una cita (requiere confirmación)",
        description=(
            "PROPONE mover una cita a otro cupo. Tiene doble efecto: libera el cupo actual "
            "y ocupa el nuevo, por eso exige aprobación humana. La cita original queda en "
            "estado 'reprogramada' y se crea una nueva enlazada a ella."
        ),
    )
    async def reprogramar_cita(
        cita_id: Annotated[int, Field(gt=0)],
        nuevo_slot_id: Annotated[int, Field(gt=0)],
        motivo: Annotated[str | None, Field(max_length=500)] = None,
    ) -> dict[str, Any]:
        identidad = ctx.autorizar("reprogramar_cita", SCOPE)
        cita = await ctx.cliente.obtener(f"/citas/{cita_id}")
        ctx.auditor.invocacion(
            "reprogramar_cita",
            sujeto=identidad.sujeto,
            scope=str(SCOPE),
            argumentos={"cita_id": cita_id, "nuevo_slot_id": nuevo_slot_id},
            resultado="propuesta",
        )
        return ctx.proponer(
            "reprogramar_cita",
            {"cita_id": cita_id, "nuevo_slot_id": nuevo_slot_id, "motivo": motivo},
            resumen=(
                f"Mover la cita {cita_id} de {cita['paciente']} "
                f"({cita['inicio_local']}) al cupo {nuevo_slot_id}."
            ),
            efectos=[
                "El cupo actual quedará libre.",
                "El cupo nuevo quedará ocupado.",
                "Se creará una cita nueva enlazada a la original.",
            ],
            sujeto=identidad.sujeto,
        )

    @servidor.tool(
        name="registrar_asistencia",
        title="Registrar asistencia (requiere confirmación)",
        description=(
            "PROPONE registrar qué pasó con la cita: 'en_espera' (el paciente llegó y está "
            "en sala), 'atendida' (se realizó) o 'no_asistio' (no llegó). Requiere "
            "aprobación humana porque 'atendida' y 'no_asistio' generan cargos en cartera. "
            "El orden válido es agendada → confirmada → en_espera → atendida."
        ),
    )
    async def registrar_asistencia(
        cita_id: Annotated[int, Field(gt=0)],
        estado: Annotated[str, Field(description="en_espera | atendida | no_asistio")],
    ) -> dict[str, Any]:
        identidad = ctx.autorizar("registrar_asistencia", SCOPE)
        if estado not in ESTADOS_ASISTENCIA:
            raise ErrorHerramienta(
                "ENTRADA_INVALIDA",
                f"'{estado}' no es un estado de asistencia.",
                sugerencia=f"Usa uno de: {', '.join(ESTADOS_ASISTENCIA)}.",
                detalles={"estados_validos": list(ESTADOS_ASISTENCIA)},
            )
        cita = await ctx.cliente.obtener(f"/citas/{cita_id}")
        efectos = [f"La cita pasará de '{cita['estado']}' a '{estado}'."]
        if estado == "atendida":
            efectos.append("Se generará el cargo que corresponda al régimen del paciente.")
        if estado == "no_asistio":
            efectos.append(
                "El cupo quedará libre y, si la cita estaba confirmada, se generará una "
                "penalización por inasistencia."
            )
        ctx.auditor.invocacion(
            "registrar_asistencia",
            sujeto=identidad.sujeto,
            scope=str(SCOPE),
            argumentos={"cita_id": cita_id, "estado": estado},
            resultado="propuesta",
        )
        return ctx.proponer(
            "registrar_asistencia",
            {"cita_id": cita_id, "estado": estado},
            resumen=f"Registrar '{estado}' en la cita {cita_id} de {cita['paciente']}.",
            efectos=efectos,
            sujeto=identidad.sujeto,
        )

    @servidor.tool(
        name="ofrecer_cupo_lista_espera",
        title="Ofrecer un cupo liberado (requiere confirmación)",
        description=(
            "PROPONE ofrecer un cupo libre al siguiente paciente de la lista de espera de "
            "esa especialidad. Prioriza urgencias y, a igual prioridad, antigüedad. NO "
            "agenda la cita: devuelve a quién contactar y su teléfono. Requiere aprobación "
            "humana porque implica contactar a una persona."
        ),
    )
    async def ofrecer_cupo_lista_espera(
        slot_id: Annotated[int, Field(gt=0)],
    ) -> dict[str, Any]:
        identidad = ctx.autorizar("ofrecer_cupo_lista_espera", SCOPE)
        ctx.auditor.invocacion(
            "ofrecer_cupo_lista_espera",
            sujeto=identidad.sujeto,
            scope=str(SCOPE),
            argumentos={"slot_id": slot_id},
            resultado="propuesta",
        )
        return ctx.proponer(
            "ofrecer_cupo_lista_espera",
            {"slot_id": slot_id},
            resumen=f"Ofrecer el cupo {slot_id} al siguiente paciente en lista de espera.",
            efectos=[
                "Se marcará la entrada de la lista como 'ofrecida'.",
                "Se devolverá el nombre y el teléfono del paciente a contactar.",
                "NO se agenda ninguna cita: agendar es una decisión aparte.",
            ],
            sujeto=identidad.sujeto,
        )
