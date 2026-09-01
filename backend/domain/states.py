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
from backend.enums import FINAL_STATES, AppointmentState

#: From KEY you may go to any of VALUES. Taken from the state machine Colombian
#: clinic systems use, not invented.
TRANSITIONS: Final[dict[AppointmentState, frozenset[AppointmentState]]] = {
    AppointmentState.SCHEDULED: frozenset(
        {
            AppointmentState.CONFIRMED,
            AppointmentState.CANCELLED,
            AppointmentState.RESCHEDULED,
            AppointmentState.NO_SHOW,
        }
    ),
    AppointmentState.CONFIRMED: frozenset(
        {
            AppointmentState.WAITING,
            AppointmentState.CANCELLED,
            AppointmentState.RESCHEDULED,
            AppointmentState.NO_SHOW,
        }
    ),
    AppointmentState.WAITING: frozenset({AppointmentState.ATTENDED, AppointmentState.CANCELLED}),
    AppointmentState.ATTENDED: frozenset(),
    AppointmentState.CANCELLED: frozenset(),
    AppointmentState.RESCHEDULED: frozenset(),
    AppointmentState.NO_SHOW: frozenset(),
}

#: Cancelling without a reason destroys the clinic's ability to audit its own
#: cancellations, so the domain refuses it.
TRANSITIONS_REQUIRING_REASON: Final[frozenset[AppointmentState]] = frozenset(
    {AppointmentState.CANCELLED}
)

#: Free the slot back into the agenda, and may trigger the waiting list (§2.4).
TRANSITIONS_FREEING_SLOT: Final[frozenset[AppointmentState]] = frozenset(
    {AppointmentState.CANCELLED, AppointmentState.RESCHEDULED, AppointmentState.NO_SHOW}
)

#: Transitions that produce a charge in accounts receivable (§2.3).
TRANSITIONS_CREATING_CHARGE: Final[frozenset[AppointmentState]] = frozenset(
    {AppointmentState.ATTENDED, AppointmentState.NO_SHOW}
)


@dataclass(frozen=True, slots=True)
class TransitionEffects:
    """What a legal transition implies beyond changing a column.

    Returned by :func:`validar_transicion` so callers never re-derive these
    rules, and so the effects are testable on their own.
    """

    previous_status: AppointmentState
    new_status: AppointmentState
    libera_slot: bool
    genera_cargo: bool
    dispara_lista_espera: bool
    requiere_auditoria: bool = True


@dataclass(frozen=True, slots=True)
class HistoryRecord:
    """One immutable audit row for a state change (security layer 5)."""

    previous_status: AppointmentState
    new_status: AppointmentState
    user: str
    occurred_at: datetime
    reason: str | None = None
    metadatos: dict[str, str] = field(default_factory=dict)


def reachable_states(status: AppointmentState) -> frozenset[AppointmentState]:
    """States reachable from `estado` in one step."""
    return TRANSITIONS[status]


def is_final(status: AppointmentState) -> bool:
    return status in FINAL_STATES


def is_valid_transition(current: AppointmentState, new_state: AppointmentState) -> bool:
    """Pure predicate, no exceptions. Useful for filtering and for tests."""
    return new_state in TRANSITIONS[current]


def validate_transition(
    current: AppointmentState,
    new_state: AppointmentState,
    *,
    reason: str | None = None,
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
                suggestion=(
                    "If the patient needs another visit, book a new one with book_appointment."
                ),
                details={"estado_actual": str(current), "requested_state": str(new_state)},
                code=ErrorCode.APPOINTMENT_IN_FINAL_STATE,
            )
        raise InvalidTransition(
            f"Cannot move from '{current}' to '{new_state}'.",
            suggestion=(
                "From this state only these are valid: " + ", ".join(str(e) for e in allowed) + "."
            ),
            details={
                "estado_actual": str(current),
                "requested_state": str(new_state),
                "valid_transitions": [str(e) for e in allowed],
            },
        )

    if new_state in TRANSITIONS_REQUIRING_REASON and not (reason or "").strip():
        raise ReasonRequired(
            f"Moving to '{new_state}' requires a reason.",
            suggestion=(
                "Call the tool again including the 'motivo' parameter with the reason "
                "the patient gave."
            ),
            details={"requested_state": str(new_state)},
        )

    libera = new_state in TRANSITIONS_FREEING_SLOT
    return TransitionEffects(
        previous_status=current,
        new_status=new_state,
        libera_slot=libera,
        genera_cargo=new_state in TRANSITIONS_CREATING_CHARGE,
        # Only a cancellation frees a slot someone on the waiting list could
        # take. A reschedule moves the same patient; a no-show happens once the
        # slot has already elapsed.
        dispara_lista_espera=new_state is AppointmentState.CANCELLED,
    )
