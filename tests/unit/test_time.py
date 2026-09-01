"""Timezone and business-hours tests.

America/Bogota is UTC-5 with no DST, which makes the arithmetic checkable by
hand, and makes an off-by-five-hours bug obvious the moment it appears.
"""

from __future__ import annotations

import itertools
from datetime import date, datetime, time, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from backend.domain.time import (
    CLINIC_TZ,
    CLOSING,
    OPENING,
    SLOT_LENGTH,
    UTC,
    hours_until,
    is_within_hours,
    is_working_day,
    local,
    now_at_clinic,
    now_utc,
    slots_for_day,
    to_clinic_time,
    to_utc,
    within_confirmation_window,
)

LUNES = date(2026, 8, 31)
SATURDAY = date(2026, 9, 5)
SUNDAY = date(2026, 9, 6)


class TestConversions:
    def test_bogota_is_utc_minus_five(self) -> None:
        occurred_at = local(LUNES, time(9, 0))
        assert to_utc(occurred_at).hour == 14

    def test_the_round_trip_preserves_the_instant(self) -> None:
        occurred_at = local(LUNES, time(15, 30))
        assert to_clinic_time(to_utc(occurred_at)) == occurred_at

    @pytest.mark.parametrize("function", [to_clinic_time, to_utc])
    def test_a_naive_datetime_is_refused(self, function: object) -> None:
        """Guessing a timezone is how schedules silently drift."""
        with pytest.raises(ValueError, match="naive"):
            function(datetime(2026, 8, 31, 9, 0))  # type: ignore[operator]

    def test_now_always_carries_a_timezone(self) -> None:
        assert now_utc().tzinfo is not None
        assert now_at_clinic().tzinfo is not None
        assert now_at_clinic().tzinfo is CLINIC_TZ

    def test_now_utc_and_now_local_are_the_same_instant(self) -> None:
        difference = abs((now_utc() - now_at_clinic()).total_seconds())
        assert difference < 1


class TestWorkingDays:
    @pytest.mark.parametrize(
        ("day", "working"),
        [(LUNES, True), (date(2026, 9, 4), True), (SATURDAY, True), (SUNDAY, False)],
    )
    def test_sunday_is_not_a_working_day(self, day: date, working: bool) -> None:
        assert is_working_day(day) is working


class TestWorkingHours:
    @pytest.mark.parametrize(
        ("hour", "expected"),
        [
            (time(7, 59), False),  # before opening
            (time(8, 0), True),  # opening sharp
            (time(11, 59), True),
            (time(12, 0), False),  # lunch
            (time(13, 59), False),
            (time(14, 0), True),
            (time(17, 59), True),
            (time(18, 0), False),  # closing sharp
            (time(23, 0), False),
        ],
    )
    def test_the_slots_of_a_weekday(self, hour: time, expected: bool) -> None:
        assert is_within_hours(local(LUNES, hour)) is expected

    def test_saturday_mornings_only(self) -> None:
        assert is_within_hours(local(SATURDAY, time(9, 0)))
        assert not is_within_hours(local(SATURDAY, time(15, 0)))

    def test_never_on_sunday(self) -> None:
        assert not is_within_hours(local(SUNDAY, time(10, 0)))

    def test_it_evaluates_in_local_time_not_utc(self) -> None:
        """14:00 UTC is 09:00 in Bogota: business hours. The naive check fails."""
        moment_utc = datetime(2026, 8, 31, 14, 0, tzinfo=UTC)
        assert is_within_hours(moment_utc)
        # And 02:00 UTC is 21:00 the previous day locally: closed.
        assert not is_within_hours(datetime(2026, 9, 1, 2, 0, tzinfo=UTC))


class TestSlotsOfTheDay:
    def test_a_weekday_has_16_slots(self) -> None:
        """8-12 and 14-18, half-hour slots."""
        assert len(slots_for_day(LUNES)) == 16

    def test_a_saturday_has_8_slots(self) -> None:
        assert len(slots_for_day(SATURDAY)) == 8

    def test_sunday_has_no_slots(self) -> None:
        assert slots_for_day(SUNDAY) == []

    def test_the_slots_are_returned_in_utc(self) -> None:
        for start, end in slots_for_day(LUNES):
            assert start.tzinfo is UTC
            assert end.tzinfo is UTC

    def test_no_slot_falls_in_the_lunch_break(self) -> None:
        for start, _ in slots_for_day(LUNES):
            assert not (time(12, 0) <= to_clinic_time(start).time() < time(14, 0))

    def test_the_slots_do_not_overlap_and_are_contiguous_per_block(self) -> None:
        for (_, fin_anterior), (start, _) in itertools.pairwise(slots_for_day(LUNES)):
            assert start >= fin_anterior

    def test_every_slot_lasts_what_was_declared(self) -> None:
        for start, end in slots_for_day(LUNES):
            assert end - start == SLOT_LENGTH

    def test_the_first_opens_and_the_last_closes(self) -> None:
        slots = slots_for_day(LUNES)
        assert to_clinic_time(slots[0][0]).time() == OPENING
        assert to_clinic_time(slots[-1][1]).time() == CLOSING

    def test_every_generated_slot_falls_in_working_hours(self) -> None:
        for day in (LUNES, SATURDAY):
            for start, _ in slots_for_day(day):
                assert is_within_hours(start)


class TestConfirmationWindow:
    NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)

    @pytest.mark.parametrize(
        ("hours", "within"),
        [(-1, False), (0, True), (1, True), (24, True), (48, True), (49, False), (72, False)],
    )
    def test_the_window_is_48_hours(self, hours: float, within: bool) -> None:
        appointment = self.NOW + timedelta(hours=hours)
        assert within_confirmation_window(appointment, now=self.NOW) is within

    def test_a_past_appointment_is_left_out(self) -> None:
        past = self.NOW - timedelta(days=3)
        assert not within_confirmation_window(past, now=self.NOW)


class TestHoursUntil:
    def test_positive_sign_towards_the_future(self) -> None:
        now = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
        assert hours_until(now + timedelta(hours=5), since=now) == 5

    def test_negative_sign_towards_the_past(self) -> None:
        now = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
        assert hours_until(now - timedelta(hours=2), since=now) == -2

    def test_it_works_across_different_timezones(self) -> None:
        utc = datetime(2026, 8, 31, 14, 0, tzinfo=UTC)
        bogota = local(LUNES, time(9, 0))  # the same instant
        assert hours_until(utc, since=bogota) == 0


class TestProperties:
    dates = st.dates(min_value=date(2026, 1, 1), max_value=date(2027, 12, 31))

    @given(day=dates)
    def test_every_generated_slot_is_valid(self, day: date) -> None:
        for start, end in slots_for_day(day):
            assert end > start
            assert end - start == SLOT_LENGTH
            assert is_within_hours(start)
            assert to_clinic_time(start).date() == day

    @given(day=dates)
    def test_no_slot_on_a_sunday(self, day: date) -> None:
        if day.weekday() == 6:
            assert slots_for_day(day) == []

    @given(
        day=dates,
        hour=st.times(),
    )
    def test_the_conversion_is_involutive(self, day: date, hour: object) -> None:
        occurred_at = local(day, hour)  # type: ignore[arg-type]
        assert to_utc(to_clinic_time(occurred_at)) == to_utc(occurred_at)
