"""What survives a retry, and what a lost race looks like.

Three bugs found by driving the running stack rather than the suite, all in the
gap between "the database is safe" and "the caller was told something useful".
Each test here names the observed failure, so a regression reads as a story and
not as an assertion nobody remembers the reason for.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy.orm import Session

from backend.domain import services
from backend.domain.errors import SlotUnavailable
from backend.enums import Specialty, WaitingListPriority, WaitingListState
from backend.models import Appointment, WaitingList
from tests.conftest import Scenario

pytestmark = pytest.mark.integration


class TestALostRace:
    def test_the_loser_is_told_the_slot_is_taken_with_alternatives(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        """Ten concurrent bookings over HTTP produced one success, two clean
        refusals and six `500`s. The optimistic `version_id` on `agenda_slot`
        raises `StaleDataError`, a different class from the `IntegrityError` the
        code caught, so it escaped as an unhandled error.

        The loser now gets exactly what a caller arriving a second later gets,
        alternatives included: one fact, one shape, whatever the timing.
        """
        first, second = sessions(), sessions()
        slot_id = scenario.slots_general[0]

        services.book_appointment(
            first, patient_id=scenario.ana_id, slot_id=slot_id, user="agent-a"
        )
        first.commit()

        with pytest.raises(SlotUnavailable) as exc:
            services.book_appointment(
                second, patient_id=scenario.bruno_id, slot_id=slot_id, user="agent-b"
            )
        assert "alternatives" in exc.value.details
        second.rollback()

        check = sessions()
        assert check.query(Appointment).filter_by(slot_id=slot_id).count() == 1


class TestAnIdempotentRetry:
    def test_the_same_key_returns_the_same_appointment(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        session_ = sessions()
        key = "retry-me-once"
        first = services.book_appointment(
            session_,
            patient_id=scenario.ana_id,
            slot_id=scenario.slots_general[0],
            user="agent",
            idempotency_key=key,
        )
        session_.commit()
        again = services.book_appointment(
            session_,
            patient_id=scenario.ana_id,
            slot_id=scenario.slots_general[0],
            user="agent",
            idempotency_key=key,
        )
        assert again.appointment.id == first.appointment.id
        assert again.reused is True
        assert session_.query(Appointment).filter_by(idempotency_key=key).count() == 1


class TestOfferingAFreedSlotTwice:
    def test_the_standing_offer_is_returned_instead_of_a_second_one(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        """The one with real-world consequences.

        Replaying an approved confirmation re-ran the tool, and this operation
        is protected by neither a unique index nor the state machine, so the
        same freed slot was offered to a second patient: two people told to come
        in for one appointment. A slot can only be promised once, so repeating
        the call returns the offer that already stands.
        """
        session_ = sessions()
        for patient in (scenario.ana_id, scenario.bruno_id):
            session_.add(
                WaitingList(
                    patient_id=patient,
                    specialty=Specialty.GENERAL_DENTISTRY,
                    priority=WaitingListPriority.SENIORITY,
                    status=WaitingListState.ACTIVE,
                )
            )
        session_.commit()

        slot_id = scenario.slots_general[0]
        first = services.offer_slot_to_waiting_list(session_, slot_id, user="agent")
        session_.commit()
        again = services.offer_slot_to_waiting_list(session_, slot_id, user="agent")

        assert again.entry.id == first.entry.id
        assert again.reused is True
        offered = session_.query(WaitingList).filter_by(offered_slot_id=slot_id).count()
        assert offered == 1, "a freed slot must be promised to exactly one person"
