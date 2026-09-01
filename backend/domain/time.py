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


def to_clinic_time(occurred_at: datetime) -> datetime:
    """Convert an aware datetime to clinic local time.

    Naive datetimes are rejected, not guessed. Assuming a timezone silently is
    how schedules drift.
    """
    if occurred_at.tzinfo is None:
        raise ValueError("naive datetime: every timestamp must carry a timezone")
    return occurred_at.astimezone(CLINIC_TZ)


def to_utc(occurred_at: datetime) -> datetime:
    """Convert any aware datetime to UTC for persistence."""
    if occurred_at.tzinfo is None:
        raise ValueError("naive datetime: every timestamp must carry a timezone")
    return occurred_at.astimezone(UTC)


def local(day: date, hora: time) -> datetime:
    """Build an aware datetime from a clinic-local date and time."""
    return datetime.combine(day, hora, tzinfo=CLINIC_TZ)


def is_working_day(day: date) -> bool:
    """Monday to Saturday. Colombian dental clinics work Saturday mornings."""
    return day.weekday() < 6


def is_within_hours(occurred_at: datetime) -> bool:
    """True when the instant falls inside consulting hours, lunch excluded."""
    loc = to_clinic_time(occurred_at)
    if not is_working_day(loc.date()):
        return False
    hora = loc.time()
    if LUNCH_START <= hora < LUNCH_END:
        return False
    if loc.weekday() == 5:  # Saturday: morning shift only
        return OPENING <= hora < LUNCH_START
    return OPENING <= hora < CLOSING


def hours_until(occurred_at: datetime, *, since: datetime | None = None) -> float:
    """Signed hours from `desde` (default: now) to `momento`."""
    reference = since if since is not None else now_utc()
    return (to_utc(occurred_at) - to_utc(reference)).total_seconds() / 3600


def within_confirmation_window(appointment_start: datetime, *, now: datetime | None = None) -> bool:
    """True when the appointment is close enough that confirmation is due."""
    remaining = hours_until(appointment_start, since=now)
    return 0 <= remaining <= CONFIRMATION_WINDOW.total_seconds() / 3600


def slots_for_day(day: date) -> list[tuple[datetime, datetime]]:
    """(start, end) pairs for every working slot of a day, in UTC."""
    if not is_working_day(day):
        return []
    end_of_day = LUNCH_START if day.weekday() == 5 else CLOSING
    slots: list[tuple[datetime, datetime]] = []
    cursor = local(day, OPENING)
    limit = local(day, end_of_day)
    while cursor + SLOT_LENGTH <= limit:
        end = cursor + SLOT_LENGTH
        if not (LUNCH_START <= cursor.time() < LUNCH_END):
            slots.append((to_utc(cursor), to_utc(end)))
        cursor = end
    return slots
