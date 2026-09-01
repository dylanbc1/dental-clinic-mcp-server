"""Service-layer tests: the write rules applied to persisted state.

Three invariants get checked over and over here, because they are the ones that
would silently break: the transition was validated, the audit row exists, and
the derived effects (slot release, charge, waiting list) actually happened.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.domain.errors import (
    AlreadyOnWaitingList,
    AppointmentNotFound,
    ConsentRequired,
    InvalidTransition,
    PatientAlreadyBooked,
    PatientNotFound,
    ProfessionalNotFound,
    ReasonRequired,
    SlotInThePast,
    SlotNotFound,
    SlotUnavailable,
    SpecialtyMismatch,
    WaitingListEmpty,
)
from backend.domain.services import (
    agenda_for_day,
    book_appointment,
    cancel_appointment,
    change_state,
    confirm_appointment,
    get_appointment,
    get_cartera,
    get_clinic,
    get_patient,
    join_waiting_list,
    list_available_slots,
    list_patient_appointments,
    offer_slot_to_waiting_list,
    record_attendance,
    record_visit_reason,
    reschedule_appointment,
    search_patients,
    validate_patient_affiliation,
)
from backend.enums import (
    AppointmentState,
    CarteraState,
    ChargeConcept,
    ChargeState,
    Regimen,
    SlotState,
    Specialty,
    WaitingListPriority,
    WaitingListState,
)
from backend.models import AgendaSlot, Appointment, AppointmentHistory, Charge, WaitingList
from tests.conftest import Scenario

pytestmark = pytest.mark.integration

ACTOR = "recepcion@clinica.test"


def historial_de(session_: Session, appointment_id: int) -> list[AppointmentHistory]:
    return list(
        session_.scalars(
            select(AppointmentHistory)
            .where(AppointmentHistory.appointment_id == appointment_id)
            .order_by(AppointmentHistory.id)
        )
    )


# --------------------------------------------------------------------------- #
# Lookups
# --------------------------------------------------------------------------- #


class TestSearches:
    def test_searches_by_exact_document(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        s = sessions()
        assert [p.id for p in search_patients(s, document_number="11111111")] == [scenario.ana_id]

    def test_the_document_does_not_match_partially(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        """A partial document match is how you hand an agent the wrong record."""
        assert search_patients(sessions(), document_number="1111") == []

    def test_searches_by_name_case_insensitively(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        assert [p.id for p in search_patients(sessions(), name="ANA gómez")] == [scenario.ana_id]

    def test_searches_by_name_fragment(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        assert len(search_patients(sessions(), name="Ruiz")) == 2

    def test_with_no_criterion_it_raises_an_actionable_error(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        with pytest.raises(PatientNotFound) as exc:
            search_patients(sessions())
        assert "search_patients" in (exc.value.suggestion or "")

    def test_it_respects_the_limit(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        assert len(search_patients(sessions(), name="a", limit=2)) <= 2

    def test_nonexistent_patient(self, sessions: Callable[[], Session], scenario: Scenario) -> None:
        with pytest.raises(PatientNotFound):
            get_patient(sessions(), 999_999)

    def test_nonexistent_appointment(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        with pytest.raises(AppointmentNotFound) as exc:
            get_appointment(sessions(), 999_999)
        assert "list_patient_appointments" in (exc.value.suggestion or "")

    def test_there_is_a_clinic(self, sessions: Callable[[], Session], scenario: Scenario) -> None:
        assert get_clinic(sessions()).id == scenario.clinic_id


class TestAvailability:
    def test_returns_only_free_and_future_slots(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        free_slots = list_available_slots(sessions())
        ids = {s.slot_id for s in free_slots}
        assert scenario.past_slot_id not in ids
        assert ids >= set(scenario.slots_general[:3])

    def test_filters_by_specialty(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        free_slots = list_available_slots(sessions(), specialty=Specialty.ORTHODONTICS)
        assert free_slots
        assert all(s.specialty is Specialty.ORTHODONTICS for s in free_slots)

    def test_filters_by_date(self, sessions: Callable[[], Session], scenario: Scenario) -> None:
        assert list_available_slots(sessions(), day=scenario.future_date)
        assert list_available_slots(sessions(), day=date(2000, 1, 3)) == []

    def test_filters_by_professional(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        free_slots = list_available_slots(sessions(), professional_id=scenario.ortho_id)
        assert all(s.professional_id == scenario.ortho_id for s in free_slots)

    def test_a_nonexistent_professional_is_an_error_not_an_empty_list(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        with pytest.raises(ProfessionalNotFound):
            list_available_slots(sessions(), professional_id=999_999)

    def test_the_slots_come_in_chronological_order(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        free_slots = list_available_slots(sessions())
        assert [s.start for s in free_slots] == sorted(s.start for s in free_slots)

    def test_the_label_is_in_local_time(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        label = list_available_slots(sessions())[0].label
        assert label.startswith(str(scenario.future_date))


class TestAffiliationAndCartera:
    def test_active_affiliation(self, sessions: Callable[[], Session], scenario: Scenario) -> None:
        r = validate_patient_affiliation(sessions(), scenario.ana_id)
        assert r.active and r.requires_copago

    def test_inactive_affiliation_falls_back_to_particular(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        r = validate_patient_affiliation(sessions(), scenario.bruno_id)
        assert not r.active
        assert r.effective_regimen is Regimen.PARTICULAR

    def test_cartera_al_dia(self, sessions: Callable[[], Session], scenario: Scenario) -> None:
        assert get_cartera(sessions(), scenario.ana_id).status is CarteraState.AL_DIA

    def test_cartera_en_mora(self, sessions: Callable[[], Session], scenario: Scenario) -> None:
        summary = get_cartera(sessions(), scenario.debtor_id)
        assert summary.status is CarteraState.EN_MORA
        assert summary.overdue_total == Decimal("180000")
        assert summary.max_overdue_days >= 74


# --------------------------------------------------------------------------- #
# Booking
# --------------------------------------------------------------------------- #


class TestBooking:
    def test_creates_the_appointment_takes_the_slot_and_audits(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        s = sessions()
        result = book_appointment(
            s, patient_id=scenario.ana_id, slot_id=scenario.slots_general[0], user=ACTOR
        )
        s.commit()

        assert result.appointment.status is AppointmentState.SCHEDULED
        assert result.appointment.created_by == ACTOR
        assert s.get(AgendaSlot, scenario.slots_general[0]).status is SlotState.BUSY

        history = historial_de(s, result.appointment.id)
        assert len(history) == 1
        assert history[0].previous_status is None
        assert history[0].new_status is AppointmentState.SCHEDULED
        assert history[0].user == ACTOR

    def test_returns_the_affiliation_to_report_the_tariff(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        result = book_appointment(
            sessions(),
            patient_id=scenario.bruno_id,
            slot_id=scenario.slots_general[0],
            user=ACTOR,
        )
        assert result.affiliation.effective_regimen is Regimen.PARTICULAR

    def test_mora_alerts_but_does_not_block(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        """The rule from §2.3 that a naive implementation gets wrong."""
        result = book_appointment(
            sessions(),
            patient_id=scenario.debtor_id,
            slot_id=scenario.slots_general[0],
            user=ACTOR,
        )
        assert result.appointment.id is not None
        assert result.cartera_alert is not None
        assert "can still be booked" in result.cartera_alert

    def test_with_no_mora_there_is_no_alert(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        result = book_appointment(
            sessions(),
            patient_id=scenario.ana_id,
            slot_id=scenario.slots_general[0],
            user=ACTOR,
        )
        assert result.cartera_alert is None

    def test_a_taken_slot_suggests_concrete_alternatives(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        s = sessions()
        book_appointment(
            s, patient_id=scenario.ana_id, slot_id=scenario.slots_general[0], user=ACTOR
        )
        s.commit()

        with pytest.raises(SlotUnavailable) as exc:
            book_appointment(
                s,
                patient_id=scenario.carla_id,
                slot_id=scenario.slots_general[0],
                user=ACTOR,
            )
        # An LLM that receives named alternatives recovers on its own turn.
        assert exc.value.details["alternatives"]
        assert "closest free slots" in (exc.value.suggestion or "")

    def test_a_past_slot_is_refused(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        with pytest.raises(SlotInThePast):
            book_appointment(
                sessions(),
                patient_id=scenario.ana_id,
                slot_id=scenario.past_slot_id,
                user=ACTOR,
            )

    def test_a_nonexistent_slot_is_refused(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        with pytest.raises(SlotNotFound):
            book_appointment(sessions(), patient_id=scenario.ana_id, slot_id=999_999, user=ACTOR)

    def test_the_expected_specialty_is_checked(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        """Guards against the model picking a slot from the wrong list."""
        with pytest.raises(SpecialtyMismatch) as exc:
            book_appointment(
                sessions(),
                patient_id=scenario.ana_id,
                slot_id=scenario.slots_general[0],
                user=ACTOR,
                expected_specialty=Specialty.ORTHODONTICS,
            )
        assert exc.value.details["slot_specialty"] == "general_dentistry"

    def test_the_right_specialty_passes(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        book_appointment(
            sessions(),
            patient_id=scenario.ana_id,
            slot_id=scenario.ortho_slots[0],
            user=ACTOR,
            expected_specialty=Specialty.ORTHODONTICS,
        )

    def test_a_patient_cannot_be_booked_into_two_overlapping_appointments(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        """Same time slot, two professionals: the patient cannot be in both."""
        s = sessions()
        book_appointment(
            s, patient_id=scenario.ana_id, slot_id=scenario.slots_general[0], user=ACTOR
        )
        s.commit()
        with pytest.raises(PatientAlreadyBooked) as exc:
            book_appointment(
                s, patient_id=scenario.ana_id, slot_id=scenario.ortho_slots[0], user=ACTOR
            )
        assert "existing_appointment_id" in exc.value.details

    def test_different_times_are_allowed(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        s = sessions()
        book_appointment(
            s, patient_id=scenario.ana_id, slot_id=scenario.slots_general[0], user=ACTOR
        )
        s.commit()
        book_appointment(
            s, patient_id=scenario.ana_id, slot_id=scenario.slots_general[1], user=ACTOR
        )
        s.commit()
        assert len(list_patient_appointments(s, scenario.ana_id)) == 2


class TestIdempotency:
    def test_the_same_key_returns_the_same_appointment(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        s = sessions()
        first = book_appointment(
            s,
            patient_id=scenario.ana_id,
            slot_id=scenario.slots_general[0],
            user=ACTOR,
            idempotency_key="peticion-1",
        )
        s.commit()
        second = book_appointment(
            s,
            patient_id=scenario.ana_id,
            slot_id=scenario.slots_general[1],
            user=ACTOR,
            idempotency_key="peticion-1",
        )
        assert second.appointment.id == first.appointment.id
        assert second.reused is True
        assert s.scalar(select(func.count()).select_from(Appointment)) == 1

    def test_without_a_key_every_call_creates_an_appointment(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        s = sessions()
        book_appointment(
            s, patient_id=scenario.ana_id, slot_id=scenario.slots_general[0], user=ACTOR
        )
        s.commit()
        book_appointment(
            s, patient_id=scenario.carla_id, slot_id=scenario.slots_general[1], user=ACTOR
        )
        s.commit()
        assert s.scalar(select(func.count()).select_from(Appointment)) == 2


# --------------------------------------------------------------------------- #
# Transitions
# --------------------------------------------------------------------------- #


@pytest.fixture
def booked_appointment(sessions: Callable[[], Session], scenario: Scenario) -> tuple[Session, int]:
    s = sessions()
    result = book_appointment(
        s, patient_id=scenario.ana_id, slot_id=scenario.slots_general[0], user=ACTOR
    )
    s.commit()
    return s, result.appointment.id


class TestConfirming:
    def test_confirms_and_audits(self, booked_appointment: tuple[Session, int]) -> None:
        s, appointment_id = booked_appointment
        result = confirm_appointment(s, appointment_id, user=ACTOR)
        s.commit()
        assert result.appointment.status is AppointmentState.CONFIRMED
        history = historial_de(s, appointment_id)
        assert history[-1].previous_status is AppointmentState.SCHEDULED
        assert history[-1].new_status is AppointmentState.CONFIRMED

    def test_it_neither_frees_the_slot_nor_creates_a_charge(
        self, booked_appointment: tuple[Session, int]
    ) -> None:
        s, appointment_id = booked_appointment
        result = confirm_appointment(s, appointment_id, user=ACTOR)
        assert not result.effects.releases_slot
        assert result.created_charge is None

    def test_confirming_twice_fails_with_a_typed_error(
        self, booked_appointment: tuple[Session, int]
    ) -> None:
        s, appointment_id = booked_appointment
        confirm_appointment(s, appointment_id, user=ACTOR)
        s.commit()
        with pytest.raises(InvalidTransition):
            confirm_appointment(s, appointment_id, user=ACTOR)


class TestCancelling:
    def test_cancelling_frees_the_slot_and_records_the_reason(
        self, booked_appointment: tuple[Session, int], scenario: Scenario
    ) -> None:
        s, appointment_id = booked_appointment
        result = cancel_appointment(s, appointment_id, reason="El paciente viajó", user=ACTOR)
        s.commit()

        assert result.appointment.status is AppointmentState.CANCELLED
        assert result.appointment.cancellation_reason == "El paciente viajó"
        assert result.effects.releases_slot
        assert s.get(AgendaSlot, scenario.slots_general[0]).status is SlotState.FREE
        assert historial_de(s, appointment_id)[-1].reason == "El paciente viajó"

    def test_without_a_reason_it_is_refused(self, booked_appointment: tuple[Session, int]) -> None:
        s, appointment_id = booked_appointment
        with pytest.raises(ReasonRequired):
            change_state(s, appointment_id, AppointmentState.CANCELLED, user=ACTOR)

    def test_the_freed_slot_becomes_available_again(
        self, booked_appointment: tuple[Session, int], scenario: Scenario
    ) -> None:
        s, appointment_id = booked_appointment
        cancel_appointment(s, appointment_id, reason="cambio de planes", user=ACTOR)
        s.commit()
        free_slots = {x.slot_id for x in list_available_slots(s)}
        assert scenario.slots_general[0] in free_slots

    def test_with_no_waiting_list_there_is_no_next_one(
        self, booked_appointment: tuple[Session, int]
    ) -> None:
        """Cancelling with an empty queue is normal, not an error."""
        s, appointment_id = booked_appointment
        result = cancel_appointment(s, appointment_id, reason="x", user=ACTOR)
        assert result.next_in_queue is None

    def test_with_a_waiting_list_it_returns_the_next_one(
        self, booked_appointment: tuple[Session, int], scenario: Scenario
    ) -> None:
        s, appointment_id = booked_appointment
        join_waiting_list(
            s,
            patient_id=scenario.carla_id,
            specialty=Specialty.GENERAL_DENTISTRY,
        )
        s.commit()
        result = cancel_appointment(s, appointment_id, reason="x", user=ACTOR)
        assert result.next_in_queue is not None
        assert result.next_in_queue.patient_id == scenario.carla_id

    def test_the_slot_is_not_offered_to_whoever_freed_it(
        self, booked_appointment: tuple[Session, int], scenario: Scenario
    ) -> None:
        s, appointment_id = booked_appointment
        join_waiting_list(s, patient_id=scenario.ana_id, specialty=Specialty.GENERAL_DENTISTRY)
        s.commit()
        result = cancel_appointment(s, appointment_id, reason="x", user=ACTOR)
        assert result.next_in_queue is None


class TestAttendance:
    def test_the_full_flow_up_to_attended_creates_a_charge(
        self, booked_appointment: tuple[Session, int], scenario: Scenario
    ) -> None:
        s, appointment_id = booked_appointment
        confirm_appointment(s, appointment_id, user=ACTOR)
        record_attendance(s, appointment_id, AppointmentState.WAITING, user=ACTOR)
        result = record_attendance(s, appointment_id, AppointmentState.ATTENDED, user=ACTOR)
        s.commit()

        assert result.appointment.status is AppointmentState.ATTENDED
        assert result.created_charge is not None
        # Ana is contributory level 1 → cuota moderadora, not full tariff.
        assert result.created_charge.concept is ChargeConcept.CUOTA_MODERADORA
        assert result.created_charge.amount == Decimal("5500")
        assert result.created_charge.status is ChargeState.PENDING

    def test_the_charge_falls_due_in_30_days(self, booked_appointment: tuple[Session, int]) -> None:
        s, appointment_id = booked_appointment
        confirm_appointment(s, appointment_id, user=ACTOR)
        record_attendance(s, appointment_id, AppointmentState.WAITING, user=ACTOR)
        result = record_attendance(s, appointment_id, AppointmentState.ATTENDED, user=ACTOR)
        appointment = result.appointment
        assert result.created_charge is not None
        expected = appointment.slot.start.date() + timedelta(days=30)
        assert abs((result.created_charge.due_date - expected).days) <= 1

    def test_inactive_affiliation_charges_the_particular_tariff(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        s = sessions()
        appointment = book_appointment(
            s, patient_id=scenario.bruno_id, slot_id=scenario.slots_general[0], user=ACTOR
        ).appointment
        confirm_appointment(s, appointment.id, user=ACTOR)
        record_attendance(s, appointment.id, AppointmentState.WAITING, user=ACTOR)
        result = record_attendance(s, appointment.id, AppointmentState.ATTENDED, user=ACTOR)
        s.commit()
        assert result.created_charge is not None
        assert result.created_charge.concept is ChargeConcept.PARTICULAR
        assert result.created_charge.amount == Decimal("120000")

    def test_a_no_show_from_confirmed_is_penalised(
        self, booked_appointment: tuple[Session, int]
    ) -> None:
        s, appointment_id = booked_appointment
        confirm_appointment(s, appointment_id, user=ACTOR)
        result = record_attendance(s, appointment_id, AppointmentState.NO_SHOW, user=ACTOR)
        s.commit()
        assert result.created_charge is not None
        assert result.created_charge.concept is ChargeConcept.NO_SHOW
        assert result.created_charge.amount == Decimal("40000")

    def test_a_no_show_without_confirming_is_not_penalised(
        self, booked_appointment: tuple[Session, int]
    ) -> None:
        """The default policy only charges a patient who had committed."""
        s, appointment_id = booked_appointment
        result = record_attendance(s, appointment_id, AppointmentState.NO_SHOW, user=ACTOR)
        s.commit()
        assert result.created_charge is None

    def test_the_no_show_frees_the_slot(
        self, booked_appointment: tuple[Session, int], scenario: Scenario
    ) -> None:
        s, appointment_id = booked_appointment
        record_attendance(s, appointment_id, AppointmentState.NO_SHOW, user=ACTOR)
        s.commit()
        assert s.get(AgendaSlot, scenario.slots_general[0]).status is SlotState.FREE

    def test_skipping_waiting_is_refused(self, booked_appointment: tuple[Session, int]) -> None:
        s, appointment_id = booked_appointment
        confirm_appointment(s, appointment_id, user=ACTOR)
        with pytest.raises(InvalidTransition):
            record_attendance(s, appointment_id, AppointmentState.ATTENDED, user=ACTOR)

    def test_the_charge_shows_up_in_the_cartera(
        self, booked_appointment: tuple[Session, int], scenario: Scenario
    ) -> None:
        s, appointment_id = booked_appointment
        confirm_appointment(s, appointment_id, user=ACTOR)
        record_attendance(s, appointment_id, AppointmentState.WAITING, user=ACTOR)
        record_attendance(s, appointment_id, AppointmentState.ATTENDED, user=ACTOR)
        s.commit()
        summary = get_cartera(s, scenario.ana_id)
        assert summary.charge_count == 1
        assert summary.pending_total == Decimal("5500")


class TestRescheduling:
    def test_frees_the_old_takes_the_new_and_chains_them(
        self, booked_appointment: tuple[Session, int], scenario: Scenario
    ) -> None:
        s, appointment_id = booked_appointment
        result = reschedule_appointment(
            s, appointment_id, scenario.slots_general[2], user=ACTOR, reason="Choque de agenda"
        )
        s.commit()

        original = get_appointment(s, appointment_id)
        assert original.status is AppointmentState.RESCHEDULED
        assert s.get(AgendaSlot, scenario.slots_general[0]).status is SlotState.FREE
        assert s.get(AgendaSlot, scenario.slots_general[2]).status is SlotState.BUSY

        replacement = result.appointment
        assert replacement.id != appointment_id
        assert replacement.status is AppointmentState.SCHEDULED
        assert replacement.source_appointment_id == appointment_id

    def test_the_new_appointment_has_its_own_history(
        self, booked_appointment: tuple[Session, int], scenario: Scenario
    ) -> None:
        s, appointment_id = booked_appointment
        replacement = reschedule_appointment(
            s, appointment_id, scenario.slots_general[2], user=ACTOR
        ).appointment
        s.commit()
        history = historial_de(s, replacement.id)
        assert len(history) == 1
        assert "Rescheduled from" in (history[0].reason or "")

    def test_it_does_not_reschedule_onto_a_taken_slot(
        self, booked_appointment: tuple[Session, int], scenario: Scenario
    ) -> None:
        s, appointment_id = booked_appointment
        book_appointment(
            s, patient_id=scenario.carla_id, slot_id=scenario.slots_general[3], user=ACTOR
        )
        s.commit()
        with pytest.raises(SlotUnavailable):
            reschedule_appointment(s, appointment_id, scenario.slots_general[3], user=ACTOR)

    def test_it_does_not_reschedule_into_the_past(
        self, booked_appointment: tuple[Session, int], scenario: Scenario
    ) -> None:
        s, appointment_id = booked_appointment
        with pytest.raises(SlotInThePast):
            reschedule_appointment(s, appointment_id, scenario.past_slot_id, user=ACTOR)

    def test_a_rescheduled_appointment_is_final(
        self, booked_appointment: tuple[Session, int], scenario: Scenario
    ) -> None:
        s, appointment_id = booked_appointment
        reschedule_appointment(s, appointment_id, scenario.slots_general[2], user=ACTOR)
        s.commit()
        with pytest.raises(InvalidTransition):
            confirm_appointment(s, appointment_id, user=ACTOR)


# --------------------------------------------------------------------------- #
# Waiting list
# --------------------------------------------------------------------------- #


class TestWaitingList:
    def test_enrols_and_avoids_duplicates(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        s = sessions()
        join_waiting_list(s, patient_id=scenario.ana_id, specialty=Specialty.ORTHODONTICS)
        s.commit()
        with pytest.raises(AlreadyOnWaitingList):
            join_waiting_list(s, patient_id=scenario.ana_id, specialty=Specialty.ORTHODONTICS)

    def test_a_nonexistent_patient_is_not_enrolled(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        with pytest.raises(PatientNotFound):
            join_waiting_list(sessions(), patient_id=999_999, specialty=Specialty.ORTHODONTICS)

    def test_it_offers_the_slot_to_the_first_in_the_queue(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        s = sessions()
        join_waiting_list(s, patient_id=scenario.ana_id, specialty=Specialty.ORTHODONTICS)
        join_waiting_list(
            s,
            patient_id=scenario.carla_id,
            specialty=Specialty.ORTHODONTICS,
            priority=WaitingListPriority.URGENT,
        )
        s.commit()

        offer = offer_slot_to_waiting_list(s, scenario.ortho_slots[0], user=ACTOR)
        s.commit()
        # Urgency jumps the queue even though Ana enrolled first.
        assert offer.patient.id == scenario.carla_id
        assert offer.original_position == 1
        assert offer.entry.status is WaitingListState.OFFERED
        assert offer.entry.offered_slot_id == scenario.ortho_slots[0]

    def test_offering_books_nothing(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        """Offering is a contact instruction, not a booking. Booking the slot is
        a separate decision that gets its own approval."""
        s = sessions()
        join_waiting_list(s, patient_id=scenario.ana_id, specialty=Specialty.ORTHODONTICS)
        s.commit()
        offer_slot_to_waiting_list(s, scenario.ortho_slots[0], user=ACTOR)
        s.commit()
        assert s.scalar(select(func.count()).select_from(Appointment)) == 0
        assert s.get(AgendaSlot, scenario.ortho_slots[0]).status is SlotState.FREE

    def test_an_empty_list_gives_an_actionable_error(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        with pytest.raises(WaitingListEmpty) as exc:
            offer_slot_to_waiting_list(sessions(), scenario.ortho_slots[0], user=ACTOR)
        assert "check_availability" in (exc.value.suggestion or "")

    def test_it_does_not_offer_from_another_specialty(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        s = sessions()
        join_waiting_list(s, patient_id=scenario.ana_id, specialty=Specialty.ENDODONTICS)
        s.commit()
        with pytest.raises(WaitingListEmpty):
            offer_slot_to_waiting_list(s, scenario.ortho_slots[0], user=ACTOR)

    def test_an_offered_entry_leaves_the_queue(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        s = sessions()
        join_waiting_list(s, patient_id=scenario.ana_id, specialty=Specialty.ORTHODONTICS)
        s.commit()
        offer_slot_to_waiting_list(s, scenario.ortho_slots[0], user=ACTOR)
        s.commit()
        active = s.scalars(
            select(WaitingList).where(WaitingList.status == WaitingListState.ACTIVE)
        ).all()
        assert active == []


# --------------------------------------------------------------------------- #
# Clinical
# --------------------------------------------------------------------------- #


class TestVisitReason:
    def test_with_consent_it_records_and_audits(
        self, booked_appointment: tuple[Session, int]
    ) -> None:
        s, appointment_id = booked_appointment
        appointment = record_visit_reason(
            s, appointment_id, "Dolor en molar inferior derecho", user="odontologa@clinica.test"
        )
        s.commit()

        assert appointment.reason == "Dolor en molar inferior derecho"
        assert appointment.reason_recorded_by == "odontologa@clinica.test"
        assert appointment.reason_recorded_at is not None

        ultimo = historial_de(s, appointment_id)[-1]
        assert "clinical data" in (ultimo.reason or "")
        assert ultimo.user == "odontologa@clinica.test"

    def test_without_consent_it_is_refused(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        """Carla has no consent on file. Scope alone must not be enough."""
        s = sessions()
        appointment = book_appointment(
            s, patient_id=scenario.carla_id, slot_id=scenario.slots_general[0], user=ACTOR
        ).appointment
        s.commit()
        with pytest.raises(ConsentRequired) as exc:
            record_visit_reason(s, appointment.id, "Dolor", user=ACTOR)
        assert "2654" in (exc.value.suggestion or "")
        assert exc.value.http_status == 403

    def test_the_refusal_leaves_no_trace_of_the_reason(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        s = sessions()
        appointment = book_appointment(
            s, patient_id=scenario.carla_id, slot_id=scenario.slots_general[0], user=ACTOR
        ).appointment
        s.commit()
        with pytest.raises(ConsentRequired):
            record_visit_reason(s, appointment.id, "Dolor agudo", user=ACTOR)
        s.rollback()
        assert get_appointment(s, appointment.id).reason is None

    def test_recording_clinical_data_does_not_change_the_status(
        self, booked_appointment: tuple[Session, int]
    ) -> None:
        s, appointment_id = booked_appointment
        before = get_appointment(s, appointment_id).status
        record_visit_reason(s, appointment_id, "Control de rutina", user=ACTOR)
        s.commit()
        assert get_appointment(s, appointment_id).status is before


# --------------------------------------------------------------------------- #
# Day view
# --------------------------------------------------------------------------- #


class TestDayAgenda:
    def test_lists_the_days_appointments_in_order(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        s = sessions()
        book_appointment(
            s, patient_id=scenario.ana_id, slot_id=scenario.slots_general[1], user=ACTOR
        )
        book_appointment(
            s, patient_id=scenario.carla_id, slot_id=scenario.slots_general[0], user=ACTOR
        )
        s.commit()
        appointments = agenda_for_day(s, scenario.future_date)
        assert [c.slot.start for c in appointments] == sorted(c.slot.start for c in appointments)
        assert len(appointments) == 2

    def test_a_day_with_no_appointments_returns_empty(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        assert agenda_for_day(sessions(), date(2000, 1, 3)) == []

    def test_includes_the_cancelled_ones(
        self, booked_appointment: tuple[Session, int], scenario: Scenario
    ) -> None:
        """The front desk needs to see what was cancelled today, not a clean slate."""
        s, appointment_id = booked_appointment
        cancel_appointment(s, appointment_id, reason="x", user=ACTOR)
        s.commit()
        appointments = agenda_for_day(s, scenario.future_date)
        assert [c.status for c in appointments] == [AppointmentState.CANCELLED]


class TestCompleteAudit:
    def test_every_transition_leaves_exactly_one_row(
        self, booked_appointment: tuple[Session, int]
    ) -> None:
        s, appointment_id = booked_appointment
        confirm_appointment(s, appointment_id, user=ACTOR)
        record_attendance(s, appointment_id, AppointmentState.WAITING, user=ACTOR)
        record_attendance(s, appointment_id, AppointmentState.ATTENDED, user=ACTOR)
        s.commit()

        history = historial_de(s, appointment_id)
        assert [h.new_status for h in history] == [
            AppointmentState.SCHEDULED,
            AppointmentState.CONFIRMED,
            AppointmentState.WAITING,
            AppointmentState.ATTENDED,
        ]
        assert all(h.user == ACTOR for h in history)

    def test_a_refused_transition_leaves_no_trace(
        self, booked_appointment: tuple[Session, int]
    ) -> None:
        s, appointment_id = booked_appointment
        before = len(historial_de(s, appointment_id))
        with pytest.raises(InvalidTransition):
            record_attendance(s, appointment_id, AppointmentState.ATTENDED, user=ACTOR)
        s.rollback()
        assert len(historial_de(s, appointment_id)) == before

    def test_the_actor_is_recorded_per_operation(
        self, booked_appointment: tuple[Session, int]
    ) -> None:
        """An audit trail with the same user in every row is not an audit trail."""
        s, appointment_id = booked_appointment
        confirm_appointment(s, appointment_id, user="ana@clinica.test")
        cancel_appointment(s, appointment_id, reason="x", user="jefe@clinica.test")
        s.commit()
        users = [h.user for h in historial_de(s, appointment_id)]
        assert users == [ACTOR, "ana@clinica.test", "jefe@clinica.test"]

    def test_every_charge_from_an_appointment_stays_linked_to_it(
        self, booked_appointment: tuple[Session, int]
    ) -> None:
        """A charge produced by a transition must point back at it, so the
        patient can be told *what* they are being billed for. (Standalone
        charges with no appointment are legitimate, imported debt for instance,
        so the assertion is scoped to the generated one.)"""
        s, appointment_id = booked_appointment
        confirm_appointment(s, appointment_id, user=ACTOR)
        result = record_attendance(s, appointment_id, AppointmentState.NO_SHOW, user=ACTOR)
        s.commit()
        assert result.created_charge is not None
        assert result.created_charge.appointment_id == appointment_id
        assert all(c.patient_id is not None for c in s.scalars(select(Charge)))
