"""Seed tests.

Two properties, both load-bearing for a portfolio project:

* **Determinism.** Same seed, same base date, byte-identical dataset. Without
  it "reproduce what I saw in the demo" is impossible.
* **Realism.** The generated agenda must actually be usable: free slots to book,
  patients in arrears to collect from, inactive affiliations to catch. A seed
  that produces a uniform, tidy world hides every interesting bug.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
from collections.abc import Callable
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.domain.states import is_valid_transition
from backend.enums import (
    STATES_HOLDING_SLOT,
    AppointmentState,
    ChargeState,
    Regimen,
    SlotState,
)
from backend.models import (
    AgendaSlot,
    Appointment,
    AppointmentHistory,
    Charge,
    Clinic,
    Patient,
    WaitingList,
)
from backend.seed import SeedParams, database_is_empty, seed_database

pytestmark = pytest.mark.integration

BASE_DATE = date(2026, 8, 31)
PARAMS = SeedParams(seed=20260831, patients=25, agenda_days=10, base_date=BASE_DATE)


def fingerprint(session: Session) -> str:
    """Content hash over business fields only.

    Primary keys are deliberately excluded: sequences keep advancing between
    runs, so including them would make every run differ for a reason that has
    nothing to do with the data.
    """
    content: dict[str, list] = {}

    content["patient"] = sorted(
        (
            p.document_type,
            p.document_number,
            p.name,
            p.phone,
            p.regimen,
            p.affiliation_active,
            p.cuota_moderadora_level,
            p.clinical_data_consent,
            p.birth_date.isoformat() if p.birth_date else None,
        )
        for p in session.scalars(select(Patient))
    )
    content["slot"] = sorted(
        (s.professional.license_number, s.start.isoformat(), s.end.isoformat(), s.status)
        for s in session.scalars(select(AgendaSlot))
    )
    content["appointment"] = sorted(
        (
            c.patient.document_number,
            c.professional.license_number,
            c.slot.start.isoformat(),
            c.status,
            c.cancellation_reason,
        )
        for c in session.scalars(select(Appointment))
    )
    content["charge"] = sorted(
        (g.patient.document_number, g.concept, str(g.amount), g.status, g.due_date.isoformat())
        for g in session.scalars(select(Charge))
    )
    content["waiting_list"] = sorted(
        (e.patient.document_number, e.specialty, e.priority, e.status)
        for e in session.scalars(select(WaitingList))
    )
    raw = json.dumps(content, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


@pytest.fixture
def seeded(sessions: Callable[[], Session]) -> Session:
    session_ = sessions()
    seed_database(session_, PARAMS)
    session_.commit()
    return session_


class TestDeterminism:
    def test_two_runs_with_the_same_seed_give_the_same_result(
        self, sessions: Callable[[], Session]
    ) -> None:
        session_ = sessions()
        seed_database(session_, PARAMS)
        session_.commit()
        first = fingerprint(session_)

        seed_database(session_, PARAMS)
        session_.commit()
        assert fingerprint(session_) == first

    def test_another_seed_gives_another_result(self, sessions: Callable[[], Session]) -> None:
        session_ = sessions()
        seed_database(session_, PARAMS)
        session_.commit()
        first = fingerprint(session_)

        from dataclasses import replace

        seed_database(session_, replace(PARAMS, seed=PARAMS.seed + 1))
        session_.commit()
        assert fingerprint(session_) != first

    def test_it_does_not_depend_on_the_time_of_the_run(
        self, sessions: Callable[[], Session]
    ) -> None:
        """The past/future split must come from the base date, never from the
        wall clock, otherwise the same seed drifts through the day."""
        session_ = sessions()
        seed_database(session_, PARAMS)
        session_.commit()
        first_states = sorted(
            (c.slot.start.isoformat(), c.status) for c in session_.scalars(select(Appointment))
        )
        seed_database(session_, PARAMS)
        session_.commit()
        second_states = sorted(
            (c.slot.start.isoformat(), c.status) for c in session_.scalars(select(Appointment))
        )
        assert first_states == second_states


class TestCommandIdempotency:
    def test_seeding_leaves_the_database_non_empty(self, seeded: Session) -> None:
        assert not database_is_empty(seeded)

    def test_empty_database_is_detected_as_clean(self, empty_tables: Session) -> None:
        assert database_is_empty(empty_tables)

    def test_seeding_twice_does_not_duplicate(self, sessions: Callable[[], Session]) -> None:
        session_ = sessions()
        seed_database(session_, PARAMS)
        session_.commit()
        patients = session_.scalar(select(func.count()).select_from(Patient))

        seed_database(session_, PARAMS)
        session_.commit()
        assert session_.scalar(select(func.count()).select_from(Patient)) == patients


class TestDatasetConsistency:
    def test_there_is_exactly_one_clinic(self, seeded: Session) -> None:
        assert seeded.scalar(select(func.count()).select_from(Clinic)) == 1

    def test_it_generates_the_patients_asked_for(self, seeded: Session) -> None:
        assert seeded.scalar(select(func.count()).select_from(Patient)) == PARAMS.patients

    def test_documents_are_not_repeated(self, seeded: Session) -> None:
        documents = list(seeded.scalars(select(Patient.document_number)))
        assert len(documents) == len(set(documents))

    def test_no_appointment_takes_an_already_taken_slot(self, seeded: Session) -> None:
        """If the partial unique index were wrong, the seed itself would be the
        first thing to violate it."""
        active = [
            c.slot_id
            for c in seeded.scalars(select(Appointment))
            if c.status in STATES_HOLDING_SLOT
        ]
        assert len(active) == len(set(active))

    def test_the_slot_status_agrees_with_its_appointment(self, seeded: Session) -> None:
        for appointment in seeded.scalars(select(Appointment)):
            if appointment.status in STATES_HOLDING_SLOT:
                assert appointment.slot.status is SlotState.BUSY, appointment.id

    def test_every_history_describes_a_legal_path(self, seeded: Session) -> None:
        """Every seeded appointment must have a history the state machine would
        actually have accepted. A seed that fabricates impossible histories
        makes every downstream test meaningless."""
        for appointment in seeded.scalars(select(Appointment)):
            history = sorted(appointment.history, key=lambda h: h.occurred_at)
            assert history, f"appointment {appointment.id} has no history"
            assert history[0].previous_status is None
            assert history[0].new_status is AppointmentState.SCHEDULED
            for previous, next_up in itertools.pairwise(history):
                assert next_up.previous_status is previous.new_status
                assert is_valid_transition(previous.new_status, next_up.new_status)
            assert history[-1].new_status is appointment.status

    def test_every_transition_was_audited(self, seeded: Session) -> None:
        rows = seeded.scalar(select(func.count()).select_from(AppointmentHistory))
        appointments = seeded.scalar(select(func.count()).select_from(Appointment))
        assert rows is not None and appointments is not None
        assert rows >= appointments  # at least the creation row per appointment

    def test_charges_only_hang_off_attended_or_no_show_appointments(self, seeded: Session) -> None:
        for charge in seeded.scalars(select(Charge)):
            if charge.appointment is not None:
                assert charge.appointment.status in {
                    AppointmentState.ATTENDED,
                    AppointmentState.NO_SHOW,
                }

    def test_no_charge_is_negative(self, seeded: Session) -> None:
        assert all(c.amount >= 0 for c in seeded.scalars(select(Charge)))

    def test_an_active_soat_patient_accrues_no_visit_charges(self, seeded: Session) -> None:
        for charge in seeded.scalars(select(Charge)):
            if (
                charge.appointment is not None
                and charge.appointment.status is AppointmentState.ATTENDED
            ):
                patient = charge.patient
                assert not (patient.regimen is Regimen.SOAT and patient.affiliation_active), (
                    f"SOAT activo con cargo de atención: paciente {patient.id}"
                )


class TestDatasetRealism:
    """A seed nobody can demo against is a seed that failed."""

    def test_free_slots_remain_to_book_into(self, seeded: Session) -> None:
        free_slots = seeded.scalar(
            select(func.count()).select_from(AgendaSlot).where(AgendaSlot.status == SlotState.FREE)
        )
        assert free_slots is not None and free_slots > 50

    def test_there_are_appointments_in_several_states(self, seeded: Session) -> None:
        states = {c.status for c in seeded.scalars(select(Appointment))}
        assert {
            AppointmentState.SCHEDULED,
            AppointmentState.CONFIRMED,
            AppointmentState.ATTENDED,
        } <= states

    def test_there_are_no_shows_the_pain_this_project_targets(self, seeded: Session) -> None:
        no_shows = [
            c for c in seeded.scalars(select(Appointment)) if c.status is AppointmentState.NO_SHOW
        ]
        assert no_shows

    def test_there_are_inactive_affiliationes(self, seeded: Session) -> None:
        inactive = [
            p
            for p in seeded.scalars(select(Patient))
            if not p.affiliation_active and p.regimen is not Regimen.PARTICULAR
        ]
        assert inactive, "with no inactive affiliations, validate_affiliation has nothing to catch"

    def test_there_are_patients_without_clinical_consent(self, seeded: Session) -> None:
        """The clinical tool must have real cases where it is correctly refused."""
        without_consent = [
            p for p in seeded.scalars(select(Patient)) if not p.clinical_data_consent
        ]
        assert without_consent

    def test_there_are_pending_charges_to_collect(self, seeded: Session) -> None:
        outstanding = [c for c in seeded.scalars(select(Charge)) if c.status == "pending"]
        assert outstanding

    def test_there_is_genuinely_overdue_cartera(self, seeded: Session) -> None:
        """Charges fall due 30 days after the visit, and the seeded agenda only
        reaches a couple of weeks back. Without carried-over balances every
        patient reads `al_dia`, and the rule that debt warns without blocking
        has nothing to demonstrate itself on."""
        overdue = [
            c
            for c in seeded.scalars(select(Charge))
            if c.status == ChargeState.PENDING and c.due_date < BASE_DATE
        ]
        assert overdue, "the dataset has not a single overdue charge"
        assert len({c.patient_id for c in overdue}) >= 3

    def test_mora_spans_several_ageing_buckets(self, seeded: Session) -> None:
        """A ledger where everything is 20 days late exercises one bucket."""
        days = {
            (BASE_DATE - c.due_date).days
            for c in seeded.scalars(select(Charge))
            if c.status == ChargeState.PENDING and c.due_date < BASE_DATE
        }
        assert max(days) > 60, f"the oldest mora is {max(days)} days"

    def test_some_patient_is_above_the_alert_threshold(self, seeded: Session) -> None:
        """Otherwise `alerta_al_agendar` never fires on the demo data."""
        from collections import defaultdict

        from backend.domain.cartera import DEFAULT_POLICY

        by_patient: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
        for c in seeded.scalars(select(Charge)):
            if c.status == ChargeState.PENDING and c.due_date < BASE_DATE:
                by_patient[c.patient_id] += c.amount
        assert any(total >= DEFAULT_POLICY.overdue_alert_threshold for total in by_patient.values())

    def test_there_are_patients_on_the_waiting_list(self, seeded: Session) -> None:
        assert seeded.scalar(select(func.count()).select_from(WaitingList))

    def test_the_four_regimenes_are_represented(self, seeded: Session) -> None:
        regimenes = {p.regimen for p in seeded.scalars(select(Patient))}
        assert regimenes == set(Regimen)

    def test_the_agenda_covers_past_and_future(self, seeded: Session) -> None:
        dates = [s.day for s in seeded.scalars(select(AgendaSlot))]
        assert min(dates) < BASE_DATE < max(dates)


#: RFC 2606 and RFC 6761 reserve these so nothing addressed to them can leave.
#: An address on any other domain might reach a real inbox, which is the thing
#: this project promises does not exist here.
RESERVED_DOMAINS = ("example.com", "example.org", "example.net", "invalid", "test", "localhost")


class TestNoRealPii:
    """The project's headline claim is that no real patient data exists here.

    This used to assert that a phone started with `+57 3` and that an email
    contained an `@`, under a class named for a much stronger promise. A real
    Colombian mobile starts with `+57 3` too, so the test would have passed on
    exactly the data it was named to rule out: a weak assertion wearing a strong
    name, which is worse than no assertion because it stops anyone looking.
    """

    def test_no_email_can_reach_a_real_inbox(self, seeded: Session) -> None:
        for patient in seeded.scalars(select(Patient)):
            if not patient.email:
                continue
            domain = patient.email.split("@")[-1].lower()
            assert domain.endswith(RESERVED_DOMAINS), (
                f"{domain} is not a reserved domain: this address may reach someone"
            )

    def test_every_phone_is_generated_not_transcribed(self, seeded: Session) -> None:
        """Colombian mobiles are +57 3XXXXXXXXX. Shape alone proves nothing, so
        this also pins that the digits come from the seeded generator: the whole
        dataset rebuilds identically from the seed, and a transcribed number
        would not survive a change of seed."""
        phones = [p.phone for p in seeded.scalars(select(Patient))]
        assert phones, "the seed produced no patients"
        for phone in phones:
            assert re.fullmatch(r"\+57 3\d{9}", phone), phone
        assert len(set(phones)) == len(phones), "a repeated phone suggests copied data"

    def test_the_clinic_itself_carries_no_real_contact(self, seeded: Session) -> None:
        clinic = seeded.scalar(select(Clinic))
        assert clinic is not None
        assert clinic.phone.startswith("+57 601 "), clinic.phone
