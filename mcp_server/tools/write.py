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

from mcp_server.auth import Identidad, Scope
from mcp_server.confirmacion import (
    Confirmacion,
    exigir_cliente_que_confirma,
    redactar_propuesta,
)
from mcp_server.contexto import Contexto
from mcp_server.errores import ErrorHerramienta

SCOPE = Scope.WRITE

ESTADOS_ASISTENCIA = ("en_espera", "atendida", "no_asistio")


def exigir_aprobacion(confirmacion: Confirmacion, accion: str) -> None:
    """Refuse politely when the person said no.

    A declined elicitation aborts the call on its own; this covers the explicit
    `confirmado: false`, which is the same decision expressed differently.
    """
    if not confirmacion.confirmado:
        raise ErrorHerramienta(
            "OPERACION_NO_APROBADA",
            f"The responsible person did not approve '{accion}'. Nothing was changed.",
            sugerencia=(
                "Do not retry the same operation. Ask what needs to change, or tell the "
                "patient it could not be done."
            ),
        )


async def exigir_transicion_posible(ctx: Contexto, cita_id: int, destino: str) -> dict[str, Any]:
    """Fetch the appointment and refuse a transition that would fail.

    Without this a human is asked to approve something impossible, and only
    finds out when they say yes. The domain re-checks at the moment of effect,
    because the state can change between the two rounds.
    """
    cita = await ctx.cliente.obtener(f"/citas/{cita_id}")
    validas = cita.get("transiciones_validas", [])
    if destino not in validas:
        raise ErrorHerramienta(
            "TRANSICION_INVALIDA",
            f"Appointment {cita_id} is in '{cita['estado']}' and cannot move to '{destino}'.",
            sugerencia=(
                "From this state only these are valid: " + ", ".join(validas) + "."
                if validas
                else "The appointment is in a final state and accepts no more changes. "
                "If the patient needs another visit, book a new appointment."
            ),
            detalles={
                "cita_id": cita_id,
                "estado_actual": cita["estado"],
                "transiciones_validas": validas,
            },
        )
    return cita


def registrar(servidor: MCPServer[Any], ctx: Contexto) -> None:
    def preguntar(
        herramienta: str,
        identidad: Identidad,
        argumentos: dict[str, Any],
        *,
        resumen: str,
        efectos: list[str],
        advertencias: list[str] | None = None,
    ) -> Elicit[Confirmacion]:
        """Record that a person was asked, then ask."""
        ctx.auditor.invocacion(
            herramienta,
            sujeto=identidad.sujeto,
            scope=str(SCOPE),
            argumentos=argumentos,
            resultado="input_required",
        )
        return Elicit(redactar_propuesta(resumen, efectos, advertencias), Confirmacion)

    def ejecutado(
        herramienta: str, argumentos: dict[str, Any], resultado: dict[str, Any]
    ) -> dict[str, Any]:
        ctx.auditor.invocacion(
            herramienta,
            sujeto=ctx.identidad().sujeto,
            scope=str(SCOPE),
            argumentos=argumentos,
            resultado="ok",
            aprobada=True,
        )
        return resultado

    # --- agendar_cita ---------------------------------------------------- #

    async def _confirmar_agendar(
        contexto: Context,
        paciente_id: int,
        slot_id: int,
        especialidad_esperada: str | None = None,
        idempotency_key: str | None = None,
    ) -> Elicit[Confirmacion]:
        argumentos = {
            "paciente_id": paciente_id,
            "slot_id": slot_id,
            "especialidad_esperada": especialidad_esperada,
            "idempotency_key": idempotency_key,
        }
        identidad = ctx.autorizar_auditando("agendar_cita", SCOPE, argumentos)
        exigir_cliente_que_confirma(contexto)
        async with ctx.auditar_fallo("agendar_cita", SCOPE, argumentos, identidad):
            # Built from live data, so the person approves what will actually
            # happen. The slot is checked first: proposing to book a taken slot
            # asks someone to approve an operation that cannot succeed.
            cupo = await ctx.cliente.obtener(
                f"/disponibilidad/{slot_id}",
                paciente_id=paciente_id,
                especialidad_esperada=especialidad_esperada,
            )
            afiliacion = await ctx.cliente.obtener(f"/pacientes/{paciente_id}/afiliacion")
            cartera = await ctx.cliente.obtener(f"/pacientes/{paciente_id}/cartera")

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
        return preguntar(
            "agendar_cita",
            identidad,
            argumentos,
            resumen=(
                f"Agendar el {cupo['inicio_local']} con {cupo['profesional']} "
                f"({cupo['especialidad']}) para el paciente {paciente_id}."
            ),
            efectos=[
                "Se creará una cita en estado 'agendada'.",
                f"El cupo del {cupo['inicio_local']} quedará ocupado.",
                f"El cobro aplicable será: {afiliacion['concepto_cargo']}.",
            ],
            advertencias=advertencias,
        )

    @servidor.tool(
        name="agendar_cita",
        title="Agendar una cita",
        description=(
            "Books an appointment in a free slot. It does NOT book straight away: it "
            "first asks a person for confirmation, describing what will happen. Until "
            "that person approves, do not tell the patient the appointment is booked. "
            "Use the slot_id from consultar_disponibilidad. If the patient has an "
            "overdue cartera the confirmation warns about it, but the appointment CAN "
            "still be booked."
        ),
    )
    async def agendar_cita(
        paciente_id: Annotated[int, Field(gt=0)],
        slot_id: Annotated[int, Field(gt=0)],
        confirmacion: Annotated[Confirmacion, Resolve(_confirmar_agendar)],
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
        exigir_aprobacion(confirmacion, "agendar_cita")
        argumentos = {
            "paciente_id": paciente_id,
            "slot_id": slot_id,
            "especialidad_esperada": especialidad_esperada,
            "idempotency_key": idempotency_key,
        }
        return ejecutado(
            "agendar_cita",
            argumentos,
            await ctx.cliente.enviar("/citas", actor=ctx.identidad().sujeto, cuerpo=argumentos),
        )

    # --- confirmar_cita -------------------------------------------------- #

    async def _confirmar_confirmar(contexto: Context, cita_id: int) -> Elicit[Confirmacion]:
        argumentos = {"cita_id": cita_id}
        identidad = ctx.autorizar_auditando("confirmar_cita", SCOPE, argumentos)
        exigir_cliente_que_confirma(contexto)
        async with ctx.auditar_fallo("confirmar_cita", SCOPE, argumentos, identidad):
            cita = await exigir_transicion_posible(ctx, cita_id, "confirmada")
        return preguntar(
            "confirmar_cita",
            identidad,
            argumentos,
            resumen=(
                f"Confirmar la cita {cita_id} de {cita['paciente']} del {cita['inicio_local']}."
            ),
            efectos=[f"La cita pasará de '{cita['estado']}' a 'confirmada'."],
        )

    @servidor.tool(
        name="confirmar_cita",
        title="Confirmar asistencia",
        description=(
            "Marks an appointment as confirmed by the patient, ideally 48 hours ahead. "
            "Asks a person for confirmation before applying it. Confirming protects the "
            "slot: unconfirmed appointments are the ones eligible to be released."
        ),
    )
    async def confirmar_cita(
        cita_id: Annotated[int, Field(gt=0)],
        confirmacion: Annotated[Confirmacion, Resolve(_confirmar_confirmar)],
    ) -> dict[str, Any]:
        exigir_aprobacion(confirmacion, "confirmar_cita")
        return ejecutado(
            "confirmar_cita",
            {"cita_id": cita_id},
            await ctx.cliente.enviar(f"/citas/{cita_id}/confirmar", actor=ctx.identidad().sujeto),
        )

    # --- cancelar_cita --------------------------------------------------- #

    async def _confirmar_cancelar(
        contexto: Context, cita_id: int, motivo: str
    ) -> Elicit[Confirmacion]:
        argumentos = {"cita_id": cita_id, "motivo": motivo}
        identidad = ctx.autorizar_auditando("cancelar_cita", SCOPE, argumentos)
        exigir_cliente_que_confirma(contexto)
        async with ctx.auditar_fallo("cancelar_cita", SCOPE, argumentos, identidad):
            cita = await exigir_transicion_posible(ctx, cita_id, "cancelada")
        return preguntar(
            "cancelar_cita",
            identidad,
            argumentos,
            resumen=(
                f"Cancelar la cita {cita_id} de {cita['paciente']} "
                f"del {cita['inicio_local']}. Motivo: {motivo}"
            ),
            efectos=[
                f"La cita pasará de '{cita['estado']}' a 'cancelada'.",
                "El cupo quedará libre en la agenda.",
                "El motivo quedará registrado en el historial de la cita.",
                "Si hay lista de espera para esa especialidad, se informará al siguiente.",
            ],
        )

    @servidor.tool(
        name="cancelar_cita",
        title="Cancelar una cita",
        description=(
            "Cancels an appointment. The motivo is MANDATORY: without it the clinic "
            "cannot audit its own cancellations. Asks a person for confirmation before "
            "applying it. Once it runs the slot is released and, if someone is on the "
            "waiting list for that specialty, it says so, so they can be offered it."
        ),
    )
    async def cancelar_cita(
        cita_id: Annotated[int, Field(gt=0)],
        motivo: Annotated[
            str,
            Field(
                min_length=3,
                max_length=500,
                description="The reason the patient gave. Stored in the history.",
            ),
        ],
        confirmacion: Annotated[Confirmacion, Resolve(_confirmar_cancelar)],
    ) -> dict[str, Any]:
        exigir_aprobacion(confirmacion, "cancelar_cita")
        return ejecutado(
            "cancelar_cita",
            {"cita_id": cita_id, "motivo": motivo},
            await ctx.cliente.enviar(
                f"/citas/{cita_id}/cancelar",
                actor=ctx.identidad().sujeto,
                cuerpo={"motivo": motivo},
            ),
        )

    # --- reprogramar_cita ------------------------------------------------ #

    async def _confirmar_reprogramar(
        contexto: Context, cita_id: int, nuevo_slot_id: int, motivo: str | None = None
    ) -> Elicit[Confirmacion]:
        argumentos = {"cita_id": cita_id, "nuevo_slot_id": nuevo_slot_id, "motivo": motivo}
        identidad = ctx.autorizar_auditando("reprogramar_cita", SCOPE, argumentos)
        exigir_cliente_que_confirma(contexto)
        async with ctx.auditar_fallo("reprogramar_cita", SCOPE, argumentos, identidad):
            cita = await exigir_transicion_posible(ctx, cita_id, "reprogramada")
            cupo = await ctx.cliente.obtener(
                f"/disponibilidad/{nuevo_slot_id}",
                paciente_id=cita["paciente_id"],
                # The visit being moved must not conflict with itself.
                excluir_cita_id=cita_id,
            )
        return preguntar(
            "reprogramar_cita",
            identidad,
            argumentos,
            resumen=(
                f"Mover la cita {cita_id} de {cita['paciente']} ({cita['inicio_local']}) "
                f"al {cupo['inicio_local']} con {cupo['profesional']}."
            ),
            efectos=[
                f"El cupo del {cita['inicio_local']} quedará libre.",
                f"El cupo del {cupo['inicio_local']} quedará ocupado.",
                "Se creará una cita nueva enlazada a la original.",
            ],
        )

    @servidor.tool(
        name="reprogramar_cita",
        title="Reprogramar una cita",
        description=(
            "Moves an appointment to a different slot. It has two effects at once, "
            "freeing the current slot and taking the new one, which is why it asks a "
            "person for confirmation. The original appointment ends in state "
            "'reprogramada' and a new one is created, linked back to it."
        ),
    )
    async def reprogramar_cita(
        cita_id: Annotated[int, Field(gt=0)],
        nuevo_slot_id: Annotated[int, Field(gt=0)],
        confirmacion: Annotated[Confirmacion, Resolve(_confirmar_reprogramar)],
        motivo: Annotated[str | None, Field(max_length=500)] = None,
    ) -> dict[str, Any]:
        exigir_aprobacion(confirmacion, "reprogramar_cita")
        return ejecutado(
            "reprogramar_cita",
            {"cita_id": cita_id, "nuevo_slot_id": nuevo_slot_id},
            await ctx.cliente.enviar(
                f"/citas/{cita_id}/reprogramar",
                actor=ctx.identidad().sujeto,
                cuerpo={"nuevo_slot_id": nuevo_slot_id, "motivo": motivo},
            ),
        )

    # --- registrar_asistencia -------------------------------------------- #

    async def _confirmar_asistencia(
        contexto: Context, cita_id: int, estado: str
    ) -> Elicit[Confirmacion]:
        argumentos = {"cita_id": cita_id, "estado": estado}
        identidad = ctx.autorizar_auditando("registrar_asistencia", SCOPE, argumentos)
        exigir_cliente_que_confirma(contexto)
        async with ctx.auditar_fallo("registrar_asistencia", SCOPE, argumentos, identidad):
            if estado not in ESTADOS_ASISTENCIA:
                raise ErrorHerramienta(
                    "ENTRADA_INVALIDA",
                    f"'{estado}' is not an attendance state.",
                    sugerencia=f"Use one of: {', '.join(ESTADOS_ASISTENCIA)}.",
                    detalles={"estados_validos": list(ESTADOS_ASISTENCIA)},
                )
            cita = await exigir_transicion_posible(ctx, cita_id, estado)

        efectos = [f"La cita pasará de '{cita['estado']}' a '{estado}'."]
        if estado == "atendida":
            efectos.append("Se generará el cargo que corresponda al régimen del paciente.")
        if estado == "no_asistio":
            efectos.append(
                "El cupo quedará libre y, si la cita estaba confirmada, se generará una "
                "penalización por inasistencia."
            )
        return preguntar(
            "registrar_asistencia",
            identidad,
            argumentos,
            resumen=f"Registrar '{estado}' en la cita {cita_id} de {cita['paciente']}.",
            efectos=efectos,
        )

    @servidor.tool(
        name="registrar_asistencia",
        title="Registrar asistencia",
        description=(
            "Records what happened with the appointment: 'en_espera' (the patient "
            "arrived and is in the waiting room), 'atendida' (it took place) or "
            "'no_asistio' (they did not turn up). Asks a person for confirmation, "
            "because 'atendida' and 'no_asistio' create charges in the cartera. The "
            "valid order is agendada → confirmada → en_espera → atendida."
        ),
    )
    async def registrar_asistencia(
        cita_id: Annotated[int, Field(gt=0)],
        estado: Annotated[str, Field(description="en_espera | atendida | no_asistio")],
        confirmacion: Annotated[Confirmacion, Resolve(_confirmar_asistencia)],
    ) -> dict[str, Any]:
        exigir_aprobacion(confirmacion, "registrar_asistencia")
        return ejecutado(
            "registrar_asistencia",
            {"cita_id": cita_id, "estado": estado},
            await ctx.cliente.enviar(
                f"/citas/{cita_id}/asistencia",
                actor=ctx.identidad().sujeto,
                cuerpo={"estado": estado},
            ),
        )

    # --- ofrecer_cupo_lista_espera --------------------------------------- #

    async def _confirmar_ofrecer(contexto: Context, slot_id: int) -> Elicit[Confirmacion]:
        argumentos = {"slot_id": slot_id}
        identidad = ctx.autorizar_auditando("ofrecer_cupo_lista_espera", SCOPE, argumentos)
        exigir_cliente_que_confirma(contexto)
        return preguntar(
            "ofrecer_cupo_lista_espera",
            identidad,
            argumentos,
            resumen=f"Ofrecer el cupo {slot_id} al siguiente paciente en lista de espera.",
            efectos=[
                "Se marcará la entrada de la lista como 'ofrecida'.",
                "Se devolverá el nombre y el teléfono del paciente a contactar.",
                "NO se agenda ninguna cita: agendar es una decisión aparte.",
            ],
        )

    @servidor.tool(
        name="ofrecer_cupo_lista_espera",
        title="Ofrecer un cupo liberado",
        description=(
            "Offers a free slot to the next patient on the waiting list for that "
            "specialty. Urgent cases jump the queue; within the same priority it is "
            "first come, first served. It does NOT book the appointment: it returns who "
            "to contact and their phone number. Asks a person for confirmation, because "
            "it means contacting someone."
        ),
    )
    async def ofrecer_cupo_lista_espera(
        slot_id: Annotated[int, Field(gt=0)],
        confirmacion: Annotated[Confirmacion, Resolve(_confirmar_ofrecer)],
    ) -> dict[str, Any]:
        exigir_aprobacion(confirmacion, "ofrecer_cupo_lista_espera")
        return ejecutado(
            "ofrecer_cupo_lista_espera",
            {"slot_id": slot_id},
            await ctx.cliente.enviar(
                "/lista-espera/ofrecer",
                actor=ctx.identidad().sujeto,
                cuerpo={"slot_id": slot_id},
            ),
        )
