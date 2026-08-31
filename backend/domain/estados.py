"""The appointment state machine (§2.1).

Pure logic: no database, no I/O. Which transitions are legal, what each one
requires and what it triggers all live here. The whole 7x7 space is enumerated
in the tests, so an illegal transition cannot slip in later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from backend.domain.errores import CodigoError, MotivoRequerido, TransicionInvalida
from backend.enums import ESTADOS_FINALES, EstadoCita

#: From KEY you may go to any of VALUES. Taken from the state machine Colombian
#: clinic systems use, not invented.
TRANSICIONES: Final[dict[EstadoCita, frozenset[EstadoCita]]] = {
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
TRANSICIONES_QUE_EXIGEN_MOTIVO: Final[frozenset[EstadoCita]] = frozenset({EstadoCita.CANCELADA})

#: Free the slot back into the agenda, and may trigger the waiting list (§2.4).
TRANSICIONES_QUE_LIBERAN_SLOT: Final[frozenset[EstadoCita]] = frozenset(
    {EstadoCita.CANCELADA, EstadoCita.REPROGRAMADA, EstadoCita.NO_ASISTIO}
)

#: Transitions that produce a charge in accounts receivable (§2.3).
TRANSICIONES_QUE_GENERAN_CARGO: Final[frozenset[EstadoCita]] = frozenset(
    {EstadoCita.ATENDIDA, EstadoCita.NO_ASISTIO}
)


@dataclass(frozen=True, slots=True)
class EfectosTransicion:
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
class RegistroHistorial:
    """One immutable audit row for a state change (security layer 5)."""

    estado_anterior: EstadoCita
    estado_nuevo: EstadoCita
    usuario: str
    momento: datetime
    motivo: str | None = None
    metadatos: dict[str, str] = field(default_factory=dict)


def transiciones_posibles(estado: EstadoCita) -> frozenset[EstadoCita]:
    """States reachable from `estado` in one step."""
    return TRANSICIONES[estado]


def es_final(estado: EstadoCita) -> bool:
    return estado in ESTADOS_FINALES


def es_transicion_valida(actual: EstadoCita, nuevo: EstadoCita) -> bool:
    """Pure predicate, no exceptions. Useful for filtering and for tests."""
    return nuevo in TRANSICIONES[actual]


def validar_transicion(
    actual: EstadoCita,
    nuevo: EstadoCita,
    *,
    motivo: str | None = None,
) -> EfectosTransicion:
    """Validate a state change and describe its consequences.

    Raises :class:`TransicionInvalida` or :class:`MotivoRequerido`, each
    carrying a suggestion that lists the transitions that would be accepted.
    """
    if not es_transicion_valida(actual, nuevo):
        permitidas = sorted(TRANSICIONES[actual])
        if es_final(actual):
            raise TransicionInvalida(
                f"La cita ya está en estado final '{actual}' y no admite más cambios.",
                sugerencia=(
                    "Si el paciente necesita otra atención, agenda una cita nueva con agendar_cita."
                ),
                detalles={"estado_actual": str(actual), "estado_solicitado": str(nuevo)},
                codigo=CodigoError.CITA_EN_ESTADO_FINAL,
            )
        raise TransicionInvalida(
            f"No se puede pasar de '{actual}' a '{nuevo}'.",
            sugerencia=(
                "Desde este estado solo son válidas: " + ", ".join(str(e) for e in permitidas) + "."
            ),
            detalles={
                "estado_actual": str(actual),
                "estado_solicitado": str(nuevo),
                "transiciones_validas": [str(e) for e in permitidas],
            },
        )

    if nuevo in TRANSICIONES_QUE_EXIGEN_MOTIVO and not (motivo or "").strip():
        raise MotivoRequerido(
            f"La transición a '{nuevo}' exige un motivo.",
            sugerencia=(
                "Vuelve a llamar la herramienta incluyendo el parámetro 'motivo' "
                "con la razón que dio el paciente."
            ),
            detalles={"estado_solicitado": str(nuevo)},
        )

    libera = nuevo in TRANSICIONES_QUE_LIBERAN_SLOT
    return EfectosTransicion(
        estado_anterior=actual,
        estado_nuevo=nuevo,
        libera_slot=libera,
        genera_cargo=nuevo in TRANSICIONES_QUE_GENERAN_CARGO,
        # Only a cancellation frees a slot someone on the waiting list could
        # take. A reschedule moves the same patient; a no-show happens once the
        # slot has already elapsed.
        dispara_lista_espera=nuevo is EstadoCita.CANCELADA,
    )
