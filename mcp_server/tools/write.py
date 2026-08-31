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

from mcp.server.mcpserver import Elicit, MCPServer, Resolve
from pydantic import Field

from mcp_server.auth import Identidad, Scope
from mcp_server.confirmacion import Confirmacion, redactar_propuesta
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
            f"La persona responsable no aprobó '{accion}'. No se modificó ningún dato.",
            sugerencia=(
                "No reintentes la misma operación. Pregunta qué debe cambiar, o "
                "informa al paciente que no se pudo hacer."
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
            f"La cita {cita_id} está en '{cita['estado']}' y no puede pasar a '{destino}'.",
            sugerencia=(
                "Desde este estado solo son válidas: " + ", ".join(validas) + "."
                if validas
                else "La cita está en un estado final y ya no admite cambios. Si el "
                "paciente necesita otra atención, agenda una cita nueva."
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
            "Agenda una cita en un cupo libre. NO agenda de inmediato: primero pide "
            "confirmación a una persona, describiendo qué va a pasar. Hasta que esa "
            "persona apruebe, no le digas al paciente que la cita quedó agendada. Usa "
            "el slot_id de consultar_disponibilidad. Si el paciente tiene saldo en mora "
            "la confirmación lo advierte, pero la cita SÍ se puede agendar."
        ),
    )
    async def agendar_cita(
        paciente_id: Annotated[int, Field(gt=0)],
        slot_id: Annotated[int, Field(gt=0)],
        confirmacion: Annotated[Confirmacion, Resolve(_confirmar_agendar)],
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

    async def _confirmar_confirmar(cita_id: int) -> Elicit[Confirmacion]:
        argumentos = {"cita_id": cita_id}
        identidad = ctx.autorizar_auditando("confirmar_cita", SCOPE, argumentos)
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
            "Marca una cita como confirmada por el paciente, idealmente 48 horas antes. "
            "Pide confirmación a una persona antes de aplicarlo. Confirmar protege el "
            "cupo: las citas sin confirmar son las candidatas a liberarse."
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

    async def _confirmar_cancelar(cita_id: int, motivo: str) -> Elicit[Confirmacion]:
        argumentos = {"cita_id": cita_id, "motivo": motivo}
        identidad = ctx.autorizar_auditando("cancelar_cita", SCOPE, argumentos)
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
            "Cancela una cita. El motivo es OBLIGATORIO: sin él la clínica no puede "
            "auditar sus cancelaciones. Pide confirmación a una persona antes de "
            "aplicarlo. Al ejecutarse libera el cupo y, si hay alguien en lista de "
            "espera para esa especialidad, lo informa para que se le ofrezca."
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
        cita_id: int, nuevo_slot_id: int, motivo: str | None = None
    ) -> Elicit[Confirmacion]:
        argumentos = {"cita_id": cita_id, "nuevo_slot_id": nuevo_slot_id, "motivo": motivo}
        identidad = ctx.autorizar_auditando("reprogramar_cita", SCOPE, argumentos)
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
            "Mueve una cita a otro cupo. Tiene doble efecto: libera el cupo actual y "
            "ocupa el nuevo, por eso pide confirmación a una persona. La cita original "
            "queda en estado 'reprogramada' y se crea una nueva enlazada a ella."
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

    async def _confirmar_asistencia(cita_id: int, estado: str) -> Elicit[Confirmacion]:
        argumentos = {"cita_id": cita_id, "estado": estado}
        identidad = ctx.autorizar_auditando("registrar_asistencia", SCOPE, argumentos)
        async with ctx.auditar_fallo("registrar_asistencia", SCOPE, argumentos, identidad):
            if estado not in ESTADOS_ASISTENCIA:
                raise ErrorHerramienta(
                    "ENTRADA_INVALIDA",
                    f"'{estado}' no es un estado de asistencia.",
                    sugerencia=f"Usa uno de: {', '.join(ESTADOS_ASISTENCIA)}.",
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
            "Registra qué pasó con la cita: 'en_espera' (el paciente llegó y está en "
            "sala), 'atendida' (se realizó) o 'no_asistio' (no llegó). Pide confirmación "
            "a una persona porque 'atendida' y 'no_asistio' generan cargos en cartera. "
            "El orden válido es agendada → confirmada → en_espera → atendida."
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

    async def _confirmar_ofrecer(slot_id: int) -> Elicit[Confirmacion]:
        argumentos = {"slot_id": slot_id}
        identidad = ctx.autorizar_auditando("ofrecer_cupo_lista_espera", SCOPE, argumentos)
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
            "Ofrece un cupo libre al siguiente paciente de la lista de espera de esa "
            "especialidad. Prioriza urgencias y, a igual prioridad, antigüedad. NO "
            "agenda la cita: devuelve a quién contactar y su teléfono. Pide confirmación "
            "a una persona porque implica contactar a alguien."
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
