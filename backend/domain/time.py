"""Time handling.

One rule everywhere: **store UTC, present America/Bogota**.

Clinic schedules are the most common source of off-by-five-hours bugs. Every
persisted timestamp is timezone-aware UTC; everything shown to a human, or to a
model reasoning about "tomorrow at 9am", goes through :func:`a_local`.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

CLINIC_TZ = ZoneInfo("America/Bogota")
UTC = ZoneInfo("UTC")

#: Consulting hours of the clinic, local time. Slots are only generated here.
OPENING = time(8, 0)
CLOSING = time(18, 0)
LUNCH_START = time(12, 0)
LUNCH_END = time(14, 0)

#: Duration of a standard dental appointment slot.
SLOT_LENGTH = timedelta(minutes=30)

#: How far ahead a patient is expected to confirm. Unconfirmed appointments
#: become eligible for release (§2.1).
CONFIRMATION_WINDOW = timedelta(hours=48)


def now_utc() -> datetime:
    """Current instant, timezone-aware, in UTC."""
    return datetime.now(tz=UTC)


def now_at_clinic() -> datetime:
    """Current instant as the clinic's front desk would read it."""
    return datetime.now(tz=CLINIC_TZ)


def to_clinic_time(momento: datetime) -> datetime:
    """Convert an aware datetime to clinic local time.

    Naive datetimes are rejected, not guessed. Assuming a timezone silently is
    how schedules drift.
    """
    if momento.tzinfo is None:
        raise ValueError("naive datetime: every timestamp must carry a timezone")
    return momento.astimezone(CLINIC_TZ)


def to_utc(momento: datetime) -> datetime:
    """Convert any aware datetime to UTC for persistence."""
    if momento.tzinfo is None:
        raise ValueError("naive datetime: every timestamp must carry a timezone")
    return momento.astimezone(UTC)


def local(fecha: date, hora: time) -> datetime:
    """Build an aware datetime from a clinic-local date and time."""
    return datetime.combine(fecha, hora, tzinfo=CLINIC_TZ)


def is_working_day(fecha: date) -> bool:
    """Monday to Saturday. Colombian dental clinics work Saturday mornings."""
    return fecha.weekday() < 6


def is_within_hours(momento: datetime) -> bool:
    """True when the instant falls inside consulting hours, lunch excluded."""
    loc = to_clinic_time(momento)
    if not is_working_day(loc.date()):
        return False
    hora = loc.time()
    if LUNCH_START <= hora < LUNCH_END:
        return False
    if loc.weekday() == 5:  # Saturday: morning shift only
        return OPENING <= hora < LUNCH_START
    return OPENING <= hora < CLOSING


def hours_until(momento: datetime, *, desde: datetime | None = None) -> float:
    """Signed hours from `desde` (default: now) to `momento`."""
    reference = desde if desde is not None else now_utc()
    return (to_utc(momento) - to_utc(reference)).total_seconds() / 3600


def within_confirmation_window(inicio_cita: datetime, *, now: datetime | None = None) -> bool:
    """True when the appointment is close enough that confirmation is due."""
    restantes = hours_until(inicio_cita, desde=now)
    return 0 <= restantes <= CONFIRMATION_WINDOW.total_seconds() / 3600


def slots_for_day(fecha: date) -> list[tuple[datetime, datetime]]:
    """(start, end) pairs for every working slot of a day, in UTC."""
    if not is_working_day(fecha):
        return []
    fin_jornada = LUNCH_START if fecha.weekday() == 5 else CLOSING
    slots: list[tuple[datetime, datetime]] = []
    cursor = local(fecha, OPENING)
    limite = local(fecha, fin_jornada)
    while cursor + SLOT_LENGTH <= limite:
        fin = cursor + SLOT_LENGTH
        if not (LUNCH_START <= cursor.time() < LUNCH_END):
            slots.append((to_utc(cursor), to_utc(fin)))
        cursor = fin
    return slots
