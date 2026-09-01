"""Waiting list ordering tests (§2.4).

The queue must be a *total* order: same input, same output, every time. A queue
that reshuffles between calls offers the same slot to two different patients.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from backend.domain.errors import ErrorCode, WaitingListEmpty
from backend.domain.time import UTC
from backend.domain.waiting_list import (
    WaitingListEntry,
    candidates_for_slot,
    in_queue_order,
    next_in_queue,
    position_in_queue,
)
from backend.enums import Specialty, WaitingListPriority, WaitingListState

BASE = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
GENERAL = Specialty.GENERAL_DENTISTRY
ORTHO = Specialty.ORTHODONTICS


def entry(
    entry_id: int,
    patient_id: int,
    *,
    minutes: int = 0,
    specialty: Specialty = GENERAL,
    priority: WaitingListPriority = WaitingListPriority.SENIORITY,
    status: WaitingListState = WaitingListState.ACTIVE,
) -> WaitingListEntry:
    return WaitingListEntry(
        entry_id=entry_id,
        patient_id=patient_id,
        specialty=specialty,
        priority=priority,
        created_at=BASE + timedelta(minutes=minutes),
        status=status,
    )


class TestOrdering:
    def test_at_equal_priority_seniority_decides(self) -> None:
        entries = [entry(1, 10, minutes=30), entry(2, 20, minutes=0)]
        assert [e.patient_id for e in in_queue_order(entries)] == [20, 10]

    def test_urgency_outranks_seniority(self) -> None:
        entries = [
            entry(1, 10, minutes=0),  # enrolled first, but routine
            entry(2, 20, minutes=500, priority=WaitingListPriority.URGENT),
        ]
        assert [e.patient_id for e in in_queue_order(entries)] == [20, 10]

    def test_among_urgent_entries_seniority_decides(self) -> None:
        entries = [
            entry(1, 10, minutes=100, priority=WaitingListPriority.URGENT),
            entry(2, 20, minutes=50, priority=WaitingListPriority.URGENT),
        ]
        assert [e.patient_id for e in in_queue_order(entries)] == [20, 10]

    def test_the_id_breaks_ties_on_identical_times(self) -> None:
        """Two entries created in the same microsecond must still have a
        deterministic order, or the same slot gets offered twice."""
        entries = [entry(9, 90), entry(3, 30), entry(5, 50)]
        assert [e.entry_id for e in in_queue_order(entries)] == [3, 5, 9]

    def test_only_active_entries_take_part(self) -> None:
        entries = [
            entry(1, 10, status=WaitingListState.WITHDRAWN),
            entry(2, 20, minutes=10, status=WaitingListState.OFFERED),
            entry(3, 30, minutes=20, status=WaitingListState.ACCEPTED),
            entry(4, 40, minutes=30),
        ]
        assert [e.patient_id for e in in_queue_order(entries)] == [40]

    def test_the_order_is_stable_under_input_permutations(self) -> None:
        entries = [entry(i, i * 10, minutes=i * 7) for i in range(1, 9)]
        expected = [e.entry_id for e in in_queue_order(entries)]
        rng = random.Random(42)  # noqa: S311 - shuffling test input
        for _ in range(20):
            shuffled = entries[:]
            rng.shuffle(shuffled)
            assert [e.entry_id for e in in_queue_order(shuffled)] == expected

    def test_ordering_does_not_mutate_the_list_it_receives(self) -> None:
        entries = [entry(2, 20, minutes=5), entry(1, 10)]
        duplicate = list(entries)
        in_queue_order(entries)
        assert entries == duplicate


class TestCandidates:
    def test_filters_by_specialty(self) -> None:
        entries = [entry(1, 10, specialty=ORTHO), entry(2, 20, specialty=GENERAL)]
        assert [e.patient_id for e in candidates_for_slot(entries, GENERAL)] == [20]

    def test_excludes_the_given_patients(self) -> None:
        """The patient whose cancellation freed the slot is not offered it back."""
        entries = [entry(1, 10), entry(2, 20, minutes=5)]
        candidates = candidates_for_slot(entries, GENERAL, exclude_patients=frozenset({10}))
        assert [e.patient_id for e in candidates] == [20]

    def test_no_matches_returns_an_empty_list(self) -> None:
        assert candidates_for_slot([entry(1, 10, specialty=ORTHO)], GENERAL) == []


class TestNextInQueue:
    def test_returns_the_first_in_the_queue(self) -> None:
        entries = [
            entry(1, 10, minutes=60),
            entry(2, 20, minutes=10, priority=WaitingListPriority.URGENT),
        ]
        assert next_in_queue(entries, GENERAL).patient_id == 20

    def test_an_empty_list_raises_a_typed_error_not_none(self) -> None:
        with pytest.raises(WaitingListEmpty) as exc:
            next_in_queue([], GENERAL)
        assert exc.value.code is ErrorCode.WAITING_LIST_EMPTY
        assert exc.value.suggestion is not None
        assert "check_availability" in exc.value.suggestion
        assert exc.value.details["specialty"] == str(GENERAL)

    def test_excluding_everyone_is_the_same_as_an_empty_list(self) -> None:
        entries = [entry(1, 10)]
        with pytest.raises(WaitingListEmpty):
            next_in_queue(entries, GENERAL, exclude_patients=frozenset({10}))


class TestPosition:
    def test_returns_the_position_one_based(self) -> None:
        entries = [entry(1, 10), entry(2, 20, minutes=5), entry(3, 30, minutes=9)]
        assert position_in_queue(entries, 10, GENERAL) == 1
        assert position_in_queue(entries, 30, GENERAL) == 3

    def test_a_patient_not_enrolled_returns_none(self) -> None:
        assert position_in_queue([entry(1, 10)], 99, GENERAL) is None

    def test_the_position_respects_urgency(self) -> None:
        entries = [
            entry(1, 10),
            entry(2, 20, minutes=99, priority=WaitingListPriority.URGENT),
        ]
        assert position_in_queue(entries, 20, GENERAL) == 1
        assert position_in_queue(entries, 10, GENERAL) == 2


class TestProperties:
    st_entries = st.lists(
        st.builds(
            entry,
            entry_id=st.integers(min_value=1, max_value=500),
            patient_id=st.integers(min_value=1, max_value=500),
            minutes=st.integers(min_value=0, max_value=10_000),
            specialty=st.sampled_from(list(Specialty)),
            priority=st.sampled_from(list(WaitingListPriority)),
            status=st.sampled_from(list(WaitingListState)),
        ),
        max_size=25,
        unique_by=lambda e: e.entry_id,
    )

    @given(entries=st_entries)
    def test_the_order_is_total_and_deterministic(self, entries: list[WaitingListEntry]) -> None:
        first = [e.entry_id for e in in_queue_order(entries)]
        second = [e.entry_id for e in in_queue_order(list(reversed(entries)))]
        assert first == second

    @given(entries=st_entries)
    def test_no_urgent_entry_sits_behind_a_routine_one(
        self, entries: list[WaitingListEntry]
    ) -> None:
        queue = in_queue_order(entries)
        seen_routine = False
        for e in queue:
            if e.priority is WaitingListPriority.SENIORITY:
                seen_routine = True
            elif seen_routine:
                pytest.fail("una urgencia quedó detrás de una entrada por antigüedad")

    @given(entries=st_entries)
    def test_ordering_keeps_exactly_the_active_ones(self, entries: list[WaitingListEntry]) -> None:
        active = {e.entry_id for e in entries if e.status is WaitingListState.ACTIVE}
        assert {e.entry_id for e in in_queue_order(entries)} == active
