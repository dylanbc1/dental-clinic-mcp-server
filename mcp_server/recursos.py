"""Resources and the receptionist prompt.

Resources are read-only context the client can pull without spending a tool
call. Putting the clinic's own rules here, the tariff table and the no-show
policy included, means the model reads the actual policy instead of inferring
one. That is where most "the agent invented a price" failures come from.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from mcp.server.mcpserver import MCPServer

from backend.domain.tiempo import ahora_local
from mcp_server.contexto import Contexto

#: `strftime` follows the process locale, usually C in a container. An
#: assistant that says "Monday 31 de August" reads as machine-translated.
DIAS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
MESES = (
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)


def fecha_en_espanol(momento: datetime) -> str:
    return (
        f"{DIAS[momento.weekday()]} {momento.day} de {MESES[momento.month - 1]} de {momento.year}"
    )


PROMPT_RECEPCIONISTA = """\
Eres el asistente de recepción de {nombre}, una clínica odontológica en {ciudad}, Colombia.
Hablas español colombiano, tratas de "usted" a los pacientes y eres breve y concreto.

QUÉ PUEDES HACER
- Buscar pacientes, consultar la agenda, el estado de una cita, la cartera y la afiliación.
- Proponer: agendar, confirmar, cancelar, reprogramar, registrar asistencia y ofrecer
  cupos liberados a la lista de espera.

CÓMO OPERAS (esto no es negociable)
1. Ninguna herramienta de escritura cambia nada por sí sola. Devuelven una PROPUESTA con
   un token. Léele el resumen a la persona responsable y espera su aprobación explícita
   antes de llamar confirmar_operacion. Mientras tanto, NUNCA le digas al paciente que
   algo "ya quedó": todavía no ha pasado.
2. Verifica antes de proponer. Busca al paciente y consulta la cita o el cupo real; no
   trabajes con ids que asumiste.
3. Si una herramienta devuelve un error, lee la sugerencia y actúa sobre ella. No repitas
   la misma llamada esperando otro resultado.

QUÉ NO PUEDES HACER, NUNCA
- No das consejo clínico, no diagnosticas, no recomiendas tratamientos ni medicamentos,
  no interpretas síntomas. Si el paciente describe un síntoma, lo registras tal cual
  (si hay consentimiento) y escalas a un profesional.
- No niegas una cita por deuda. Un saldo en mora se INFORMA, no bloquea.
- No niegas atención por afiliación inactiva. Se atiende a tarifa particular y se informa.
- No inventas precios, horarios ni disponibilidad: consúltalos con las herramientas y con
  el recurso politicas://cartera.
- No registras motivo de consulta sin consentimiento informado del paciente.

CUÁNDO ESCALAS A UN HUMANO
- Dolor severo, sangrado, trauma o cualquier urgencia: escala de inmediato, no agendes tú.
- El paciente pide consejo clínico o cuestiona un diagnóstico.
- Reclamos de facturación, o cualquier caso donde el paciente esté molesto.
- Cualquier cosa que no puedas resolver con las herramientas disponibles.

Hoy es {hoy} y son las {hora} (hora de {ciudad}).
"""


def registrar(servidor: MCPServer[Any], ctx: Contexto) -> None:
    @servidor.resource(
        "clinica://info",
        name="Información de la clínica",
        description=(
            "Datos de la clínica, sus profesionales y las especialidades disponibles. "
            "Consúltalo antes de ofrecer una especialidad o nombrar a un profesional."
        ),
        mime_type="application/json",
    )
    async def info_clinica() -> str:
        return json.dumps(await ctx.cliente.obtener("/clinica"), ensure_ascii=False, indent=2)

    @servidor.resource(
        "politicas://cartera",
        name="Políticas de cartera y tarifas",
        description=(
            "Reglas de cobro de la clínica: tarifas particulares por especialidad, cuota "
            "moderadora por nivel, porcentaje de copago del régimen subsidiado, "
            "penalización por inasistencia y plazo de pago. Úsalo para informar un costo; "
            "nunca estimes un precio por tu cuenta."
        ),
        mime_type="application/json",
    )
    async def politicas_cartera() -> str:
        return json.dumps(
            await ctx.cliente.obtener("/politicas/cartera"), ensure_ascii=False, indent=2
        )

    @servidor.resource(
        "agenda://hoy",
        name="Agenda de hoy",
        description=(
            "Panorama de las citas de hoy con su estado, para saber quién está en sala, "
            "quién confirmó y quién no llegó."
        ),
        mime_type="application/json",
    )
    async def agenda_hoy() -> str:
        hoy: date = ahora_local().date()
        return json.dumps(
            await ctx.cliente.obtener(f"/agenda/{hoy.isoformat()}"),
            ensure_ascii=False,
            indent=2,
        )

    @servidor.prompt(
        name="recepcionista_odontologia",
        title="Recepcionista de clínica odontológica",
        description=(
            "Configura al agente como recepcionista de la clínica: tono, límites, cuándo "
            "escalar y la regla de que nada se ejecuta sin aprobación humana."
        ),
    )
    async def recepcionista_odontologia() -> str:
        clinica = await ctx.cliente.obtener("/clinica")
        ahora = ahora_local()
        return PROMPT_RECEPCIONISTA.format(
            nombre=clinica["nombre"],
            ciudad=clinica["ciudad"],
            hoy=fecha_en_espanol(ahora),
            hora=f"{ahora:%H:%M}",
        )
