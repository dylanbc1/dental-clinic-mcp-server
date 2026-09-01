"""Time handling.

One rule everywhere: **store UTC, present America/Bogota**.

Clinic schedules are the most common source of off-by-five-hours bugs. Every
persisted timestamp is timezone-aware UTC; everything shown to a human, or to a
model reasoning about "tomorrow at 9am", goes through :func:`a_local`.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ZONA_CLINICA = ZoneInfo("America/Bogota")
UTC = ZoneInfo("UTC")

#: Consulting hours of the clinic, local time. Slots are only generated here.
APERTURA = time(8, 0)
CIERRE = time(18, 0)
ALMUERZO_INICIO = time(12, 0)
ALMUERZO_FIN = time(14, 0)

#: Duration of a standard dental appointment slot.
DURACION_SLOT = timedelta(minutes=30)

#: How far ahead a patient is expected to confirm. Unconfirmed appointments
#: become eligible for release (§2.1).
VENTANA_CONFIRMACION = timedelta(hours=48)


def ahora_utc() -> datetime:
    """Current instant, timezone-aware, in UTC."""
    return datetime.now(tz=UTC)


def ahora_local() -> datetime:
    """Current instant as the clinic's front desk would read it."""
    return datetime.now(tz=ZONA_CLINICA)


def a_local(momento: datetime) -> datetime:
    """Convert an aware datetime to clinic local time.

    Naive datetimes are rejected, not guessed. Assuming a timezone silently is
    how schedules drift.
    """
    if momento.tzinfo is None:
        raise ValueError("naive datetime: every timestamp must carry a timezone")
    return momento.astimezone(ZONA_CLINICA)


def a_utc(momento: datetime) -> datetime:
    """Convert any aware datetime to UTC for persistence."""
    if momento.tzinfo is None:
        raise ValueError("naive datetime: every timestamp must carry a timezone")
    return momento.astimezone(UTC)


def local(fecha: date, hora: time) -> datetime:
    """Build an aware datetime from a clinic-local date and time."""
    return datetime.combine(fecha, hora, tzinfo=ZONA_CLINICA)


def es_dia_habil(fecha: date) -> bool:
    """Monday to Saturday. Colombian dental clinics work Saturday mornings."""
    return fecha.weekday() < 6


def es_horario_habil(momento: datetime) -> bool:
    """True when the instant falls inside consulting hours, lunch excluded."""
    loc = a_local(momento)
    if not es_dia_habil(loc.date()):
        return False
    hora = loc.time()
    if ALMUERZO_INICIO <= hora < ALMUERZO_FIN:
        return False
    if loc.weekday() == 5:  # Saturday: morning shift only
        return APERTURA <= hora < ALMUERZO_INICIO
    return APERTURA <= hora < CIERRE


def horas_hasta(momento: datetime, *, desde: datetime | None = None) -> float:
    """Signed hours from `desde` (default: now) to `momento`."""
    referencia = desde if desde is not None else ahora_utc()
    return (a_utc(momento) - a_utc(referencia)).total_seconds() / 3600


def dentro_de_ventana_confirmacion(inicio_cita: datetime, *, ahora: datetime | None = None) -> bool:
    """True when the appointment is close enough that confirmation is due."""
    restantes = horas_hasta(inicio_cita, desde=ahora)
    return 0 <= restantes <= VENTANA_CONFIRMACION.total_seconds() / 3600


def slots_del_dia(fecha: date) -> list[tuple[datetime, datetime]]:
    """(start, end) pairs for every working slot of a day, in UTC."""
    if not es_dia_habil(fecha):
        return []
    fin_jornada = ALMUERZO_INICIO if fecha.weekday() == 5 else CIERRE
    slots: list[tuple[datetime, datetime]] = []
    cursor = local(fecha, APERTURA)
    limite = local(fecha, fin_jornada)
    while cursor + DURACION_SLOT <= limite:
        fin = cursor + DURACION_SLOT
        if not (ALMUERZO_INICIO <= cursor.time() < ALMUERZO_FIN):
            slots.append((a_utc(cursor), a_utc(fin)))
        cursor = fin
    return slots
