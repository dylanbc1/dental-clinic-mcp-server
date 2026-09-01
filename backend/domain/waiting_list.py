"""Waiting list (§2.4).

When a cancellation frees a slot, the clinic offers it to the next patient in
line. "Next" is not "first to arrive": urgent cases jump the queue, and within
the same priority the queue is FIFO by enrolment time.

The ordering is pure, deterministic and total, so the tool that uses it has no
judgement of its own to exercise.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from backend.domain.errors import WaitingListEmpty
from backend.enums import Specialty, WaitingListPriority, WaitingListState

#: Lower is served first. Explicit rather than enum order, so adding a priority
#: later cannot silently reshuffle the queue.
PRIORITY_WEIGHT: dict[WaitingListPriority, int] = {
    WaitingListPriority.URGENT: 0,
    WaitingListPriority.SENIORITY: 1,
}


@dataclass(frozen=True, slots=True)
class WaitingListEntry:
    entrada_id: int
    paciente_id: int
    especialidad: Specialty
    prioridad: WaitingListPriority
    creada_en: datetime
    estado: WaitingListState = WaitingListState.ACTIVE

    @property
    def sort_key(self) -> tuple[int, datetime, int]:
        """Priority, then enrolment time, then id.

        The id keeps two entries created in the same microsecond deterministic.
        Without it the queue reorders between calls and the same patient gets
        offered a slot twice.
        """
        return (PRIORITY_WEIGHT[self.prioridad], self.creada_en, self.entrada_id)


def in_queue_order(entries: list[WaitingListEntry]) -> list[WaitingListEntry]:
    """Queue order for the active entries. Non-active entries are dropped."""
    active = [e for e in entries if e.estado is WaitingListState.ACTIVE]
    return sorted(active, key=lambda e: e.sort_key)


def candidates_for_slot(
    entries: list[WaitingListEntry],
    especialidad: Specialty,
    *,
    excluir_pacientes: frozenset[int] = frozenset(),
) -> list[WaitingListEntry]:
    """Ordered candidates for a freed slot of a given specialty.

    `excluir_pacientes` stops the patient whose cancellation freed the slot from
    being offered it straight back.
    """
    return [
        e
        for e in in_queue_order(entries)
        if e.especialidad is especialidad and e.paciente_id not in excluir_pacientes
    ]


def next_in_queue(
    entries: list[WaitingListEntry],
    especialidad: Specialty,
    *,
    excluir_pacientes: frozenset[int] = frozenset(),
) -> WaitingListEntry:
    """The single patient to offer the slot to.

    Raises :class:`ListaEsperaVacia` with a suggestion instead of returning
    ``None``: an LLM handles a typed error better than a null.
    """
    candidates = candidates_for_slot(entries, especialidad, excluir_pacientes=excluir_pacientes)
    if not candidates:
        raise WaitingListEmpty(
            f"No patients are on the waiting list for {especialidad}.",
            sugerencia=(
                "The slot stays free in the agenda. You can offer it directly with "
                "check_availability and book_appointment."
            ),
            detalles={"especialidad": str(especialidad)},
        )
    return candidates[0]


def position_in_queue(
    entries: list[WaitingListEntry],
    paciente_id: int,
    especialidad: Specialty,
) -> int | None:
    """1-based position of a patient in the queue, or ``None`` if not enrolled."""
    for indice, entry in enumerate(candidates_for_slot(entries, especialidad), start=1):
        if entry.paciente_id == paciente_id:
            return indice
    return None
