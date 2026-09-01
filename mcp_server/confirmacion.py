"""Security layer 3: human-in-the-loop, over MRTR.

The failure this prevents is documented and recent. In July 2025 an AI agent
deleted a production database at SaaStr during a code freeze: it held the
permission, so it acted. Scopes alone would not have stopped it, because the
token was valid. What was missing was a human between intent and effect.

The 2026-07-28 spec expresses that without a persistent connection. A tool that
needs an answer returns `input_required` carrying the question and a sealed
`requestState`; the client obtains the answer and retries the same call with
both. No session, no server-side pending-operation store, and any replica can
serve either round.

Three properties come from the shape rather than from code we wrote:

* **The question cannot be forged or edited.** `requestState` is sealed with
  AES-256-GCM and bound to the request, the audience and the authenticated
  principal, so an answer cannot be moved onto a different operation or a
  different user.
* **Validation runs on both rounds.** The resolver re-runs when the client
  retries, so authorisation and domain checks are re-applied at the moment of
  effect. A confirmation approves an action, it does not make an illegal one
  legal, and it does not freeze the state it saw.
* **The answer is asked once.** Only the outcome rides the state, so a retry
  does not re-prompt.
"""

from __future__ import annotations

from mcp.server.mcpserver import Context
from pydantic import BaseModel, Field

from mcp_server.errores import ErrorHerramienta


def exigir_cliente_que_confirma(contexto: Context) -> None:
    """Refuse clearly when the client cannot ask a person anything.

    Without this the call dies deep in the transport with "no back-channel for
    server-initiated requests", which tells the user nothing they can act on.
    A client that cannot elicit is not a broken client, it is an older one, and
    it deserves to be told which half of this server it can still use.
    """
    capacidades = contexto.client_capabilities
    if capacidades is not None and capacidades.elicitation is not None:
        return
    raise ErrorHerramienta(
        "CLIENTE_SIN_CONFIRMACION",
        "Your MCP client cannot ask a person for confirmation, and this server does "
        "not perform writes without one.",
        sugerencia=(
            "Read tools work normally. For the write tools you need a client on the "
            "2026-07-28 spec that declares the 'elicitation' capability. If you are "
            "exploring, use `uv run python scripts/consola.py`."
        ),
        detalles={
            "protocolo_negociado": contexto.protocol_version,
            "capacidad_requerida": "elicitation",
        },
    )


class Confirmacion(BaseModel):
    """What the person approving is asked for.

    A single boolean on purpose: the decision is approve or do not approve, and
    a form with more fields invites someone to change the operation instead of
    judging it.
    """

    confirmado: bool = Field(
        description=(
            "true to run the operation exactly as described, "
            "false to abort it without changing anything."
        )
    )


def redactar_propuesta(
    resumen: str,
    efectos: list[str],
    advertencias: list[str] | None = None,
) -> str:
    """Render the question a human reads before approving.

    Written to be read aloud to a receptionist rather than parsed: the summary
    names the appointment in words, and the effects are what they are actually
    consenting to.
    """
    lineas = [resumen, "", "Esto va a pasar:"]
    lineas += [f"  · {e}" for e in efectos]
    if advertencias:
        lineas += ["", "Ten en cuenta:"]
        lineas += [f"  ⚠ {a}" for a in advertencias]
    lineas += ["", "¿Confirmas la operación?"]
    return "\n".join(lineas)
