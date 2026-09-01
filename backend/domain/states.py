"""The appointment state machine (§2.1).

Pure logic: no database, no I/O. Which transitions are legal, what each one
requires and what it triggers all live here. The whole 7x7 space is enumerated
in the tests, so an illegal transition cannot slip in later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from backend.domain.errors import ErrorCode, InvalidTransition, ReasonRequired
from backend.enums import FINAL_STATES, EstadoCita

#: From KEY you may go to any of VALUES. Taken from the state machine Colombian
#: clinic systems use, not invented.
TRANSITIONS: Final[dict[EstadoCita, frozenset[EstadoCita]]] = {
    EstadoCita.AGENDADA: frozenset(
        {
            EstadoCita.CONFIRMADA,
            EstadoCita.CANCELADA,
            EstadoCita.REPROGRAMADA,
            EstadoCita.NO_ASISTIO,
        }
    ),
    EstadoCita.CONFIRMADA: frozenset(
        {
            EstadoCita.EN_ESPERA,
            EstadoCita.CANCELADA,
            EstadoCita.REPROGRAMADA,
            EstadoCita.NO_ASISTIO,
        }
    ),
    EstadoCita.EN_ESPERA: frozenset({EstadoCita.ATENDIDA, EstadoCita.CANCELADA}),
    EstadoCita.ATENDIDA: frozenset(),
    EstadoCita.CANCELADA: frozenset(),
    EstadoCita.REPROGRAMADA: frozenset(),
    EstadoCita.NO_ASISTIO: frozenset(),
}

#: Cancelling without a reason destroys the clinic's ability to audit its own
#: cancellations, so the domain refuses it.
TRANSITIONS_REQUIRING_REASON: Final[frozenset[EstadoCita]] = frozenset({EstadoCita.CANCELADA})

#: Free the slot back into the agenda, and may trigger the waiting list (§2.4).
TRANSITIONS_FREEING_SLOT: Final[frozenset[EstadoCita]] = frozenset(
    {EstadoCita.CANCELADA, EstadoCita.REPROGRAMADA, EstadoCita.NO_ASISTIO}
)

#: Transitions that produce a charge in accounts receivable (§2.3).
TRANSITIONS_CREATING_CHARGE: Final[frozenset[EstadoCita]] = frozenset(
    {EstadoCita.ATENDIDA, EstadoCita.NO_ASISTIO}
)


@dataclass(frozen=True, slots=True)
class TransitionEffects:
    """What a legal transition implies beyond changing a column.

    Returned by :func:`validar_transicion` so callers never re-derive these
    rules, and so the effects are testable on their own.
    """

    estado_anterior: EstadoCita
    estado_nuevo: EstadoCita
    libera_slot: bool
    genera_cargo: bool
    dispara_lista_espera: bool
    requiere_auditoria: bool = True


@dataclass(frozen=True, slots=True)
class HistoryRecord:
    """One immutable audit row for a state change (security layer 5)."""

    estado_anterior: EstadoCita
    estado_nuevo: EstadoCita
    usuario: str
    momento: datetime
    motivo: str | None = None
    metadatos: dict[str, str] = field(default_factory=dict)


def reachable_states(estado: EstadoCita) -> frozenset[EstadoCita]:
    """States reachable from `estado` in one step."""
    return TRANSITIONS[estado]


def is_final(estado: EstadoCita) -> bool:
    return estado in FINAL_STATES


def is_valid_transition(current: EstadoCita, new_state: EstadoCita) -> bool:
    """Pure predicate, no exceptions. Useful for filtering and for tests."""
    return new_state in TRANSITIONS[current]


def validate_transition(
    current: EstadoCita,
    new_state: EstadoCita,
    *,
    motivo: str | None = None,
) -> TransitionEffects:
    """Validate a state change and describe its consequences.

    Raises :class:`TransicionInvalida` or :class:`MotivoRequerido`, each
    carrying a suggestion that lists the transitions that would be accepted.
    """
    if not is_valid_transition(current, new_state):
        allowed = sorted(TRANSITIONS[current])
        if is_final(current):
            raise InvalidTransition(
                f"The appointment is already in final state '{current}' and accepts no "
                "further changes.",
                sugerencia=(
                    "If the patient needs another visit, book a new appointment with agendar_cita."
                ),
                detalles={"estado_actual": str(current), "estado_solicitado": str(new_state)},
                codigo=ErrorCode.CITA_EN_ESTADO_FINAL,
            )
        raise InvalidTransition(
            f"Cannot move from '{current}' to '{new_state}'.",
            sugerencia=(
                "From this state only these are valid: " + ", ".join(str(e) for e in allowed) + "."
            ),
            detalles={
                "estado_actual": str(current),
                "estado_solicitado": str(new_state),
                "transiciones_validas": [str(e) for e in allowed],
            },
        )

    if new_state in TRANSITIONS_REQUIRING_REASON and not (motivo or "").strip():
        raise ReasonRequired(
            f"Moving to '{new_state}' requires a reason.",
            sugerencia=(
                "Call the tool again including the 'motivo' parameter with the reason "
                "the patient gave."
            ),
            detalles={"estado_solicitado": str(new_state)},
        )

    libera = new_state in TRANSITIONS_FREEING_SLOT
    return TransitionEffects(
        estado_anterior=current,
        estado_nuevo=new_state,
        libera_slot=libera,
        genera_cargo=new_state in TRANSITIONS_CREATING_CHARGE,
        # Only a cancellation frees a slot someone on the waiting list could
        # take. A reschedule moves the same patient; a no-show happens once the
        # slot has already elapsed.
        dispara_lista_espera=new_state is EstadoCita.CANCELADA,
    )
