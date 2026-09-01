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

from backend.domain.errores import ListaEsperaVacia
from backend.enums import Especialidad, EstadoListaEspera, PrioridadListaEspera

#: Lower is served first. Explicit rather than enum order, so adding a priority
#: later cannot silently reshuffle the queue.
PESO_PRIORIDAD: dict[PrioridadListaEspera, int] = {
    PrioridadListaEspera.URGENCIA: 0,
    PrioridadListaEspera.ANTIGUEDAD: 1,
}


@dataclass(frozen=True, slots=True)
class EntradaListaEspera:
    entrada_id: int
    paciente_id: int
    especialidad: Especialidad
    prioridad: PrioridadListaEspera
    creada_en: datetime
    estado: EstadoListaEspera = EstadoListaEspera.ACTIVA

    @property
    def clave_orden(self) -> tuple[int, datetime, int]:
        """Priority, then enrolment time, then id.

        The id keeps two entries created in the same microsecond deterministic.
        Without it the queue reorders between calls and the same patient gets
        offered a slot twice.
        """
        return (PESO_PRIORIDAD[self.prioridad], self.creada_en, self.entrada_id)


def ordenar(entradas: list[EntradaListaEspera]) -> list[EntradaListaEspera]:
    """Queue order for the active entries. Non-active entries are dropped."""
    activas = [e for e in entradas if e.estado is EstadoListaEspera.ACTIVA]
    return sorted(activas, key=lambda e: e.clave_orden)


def candidatos_para_cupo(
    entradas: list[EntradaListaEspera],
    especialidad: Especialidad,
    *,
    excluir_pacientes: frozenset[int] = frozenset(),
) -> list[EntradaListaEspera]:
    """Ordered candidates for a freed slot of a given specialty.

    `excluir_pacientes` stops the patient whose cancellation freed the slot from
    being offered it straight back.
    """
    return [
        e
        for e in ordenar(entradas)
        if e.especialidad is especialidad and e.paciente_id not in excluir_pacientes
    ]


def siguiente_en_lista(
    entradas: list[EntradaListaEspera],
    especialidad: Especialidad,
    *,
    excluir_pacientes: frozenset[int] = frozenset(),
) -> EntradaListaEspera:
    """The single patient to offer the slot to.

    Raises :class:`ListaEsperaVacia` with a suggestion instead of returning
    ``None``: an LLM handles a typed error better than a null.
    """
    candidatos = candidatos_para_cupo(entradas, especialidad, excluir_pacientes=excluir_pacientes)
    if not candidatos:
        raise ListaEsperaVacia(
            f"No patients are on the waiting list for {especialidad}.",
            sugerencia=(
                "The slot stays free in the agenda. You can offer it directly with "
                "consultar_disponibilidad and agendar_cita."
            ),
            detalles={"especialidad": str(especialidad)},
        )
    return candidatos[0]


def posicion_en_lista(
    entradas: list[EntradaListaEspera],
    paciente_id: int,
    especialidad: Especialidad,
) -> int | None:
    """1-based position of a patient in the queue, or ``None`` if not enrolled."""
    for indice, entrada in enumerate(candidatos_para_cupo(entradas, especialidad), start=1):
        if entrada.paciente_id == paciente_id:
            return indice
    return None
