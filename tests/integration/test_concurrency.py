"""Concurrency guarantees, the tests that justify the schema.

Two agents will race for the same slot. An application-level "is it free?"
check always loses that race, because both read *free* before either writes.
These tests prove the database itself refuses the second booking.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from backend.enums import AppointmentState, SlotState
from backend.models import AgendaSlot, Appointment

pytestmark = pytest.mark.integration


def _appointment(payload: dict[str, int], patient: str, **extra: object) -> Appointment:
    fields: dict[str, object] = {
        "patient_id": payload[patient],
        "professional_id": payload["professional_id"],
        "slot_id": payload["slot_id"],
        "status": AppointmentState.SCHEDULED,
        "created_by": f"agente-{patient}",
    }
    fields.update(extra)
    # A cancelled appointment needs its reason, exactly as the domain demands.
    if fields["status"] is AppointmentState.CANCELLED:
        fields.setdefault("cancellation_reason", "motivo de prueba")
    return Appointment(**fields)  # type: ignore[arg-type]


class TestDoubleBooking:
    def test_two_agents_on_the_same_slot_only_one_wins(
        self, sessions: Callable[[], Session], minimal_data: dict[str, int]
    ) -> None:
        agent_a, agent_b = sessions(), sessions()

        agent_a.add(_appointment(minimal_data, "paciente_a"))
        agent_a.commit()  # A gets there first

        agent_b.add(_appointment(minimal_data, "paciente_b"))
        with pytest.raises(IntegrityError):
            agent_b.commit()
        agent_b.rollback()

        # Exactly one appointment survives. Not two, not zero.
        session_ = sessions()
        appointments = session_.query(Appointment).filter_by(slot_id=minimal_data["slot_id"]).all()
        assert len(appointments) == 1
        assert appointments[0].patient_id == minimal_data["paciente_a"]

    def test_cancelling_frees_the_slot_for_another_patient(
        self, sessions: Callable[[], Session], minimal_data: dict[str, int]
    ) -> None:
        """The uniqueness is partial: a cancelled appointment must not keep the
        slot hostage, otherwise cancellation would be pointless."""
        session_ = sessions()
        first = _appointment(minimal_data, "paciente_a")
        session_.add(first)
        session_.commit()

        first.status = AppointmentState.CANCELLED
        first.cancellation_reason = "El paciente viajó"
        session_.commit()

        session_.add(_appointment(minimal_data, "paciente_b"))
        session_.commit()  # must not raise

        active = (
            session_.query(Appointment)
            .filter(
                Appointment.slot_id == minimal_data["slot_id"],
                Appointment.status != AppointmentState.CANCELLED,
            )
            .all()
        )
        assert len(active) == 1

    @pytest.mark.parametrize(
        "status",
        [
            AppointmentState.SCHEDULED,
            AppointmentState.CONFIRMED,
            AppointmentState.WAITING,
            AppointmentState.ATTENDED,
        ],
    )
    def test_every_holding_state_blocks_a_second_appointment(
        self,
        sessions: Callable[[], Session],
        minimal_data: dict[str, int],
        status: AppointmentState,
    ) -> None:
        session_ = sessions()
        session_.add(_appointment(minimal_data, "paciente_a", status=status))
        session_.commit()

        another = sessions()
        another.add(_appointment(minimal_data, "paciente_b"))
        with pytest.raises(IntegrityError):
            another.commit()
        another.rollback()

    @pytest.mark.parametrize(
        "status",
        [AppointmentState.CANCELLED, AppointmentState.RESCHEDULED, AppointmentState.NO_SHOW],
    )
    def test_no_releasing_state_blocks_a_second_appointment(
        self,
        sessions: Callable[[], Session],
        minimal_data: dict[str, int],
        status: AppointmentState,
    ) -> None:
        session_ = sessions()
        session_.add(_appointment(minimal_data, "paciente_a", status=status))
        session_.commit()

        another = sessions()
        another.add(_appointment(minimal_data, "paciente_b"))
        another.commit()  # must not raise


class TestIdempotency:
    def test_resending_the_same_key_creates_no_duplicate(
        self, sessions: Callable[[], Session], minimal_data: dict[str, int]
    ) -> None:
        """An agent that retries a timed-out booking must not end up with two
        appointments. The database is what makes that a guarantee."""
        session_ = sessions()
        session_.add(_appointment(minimal_data, "paciente_a", idempotency_key="req-abc-123"))
        session_.commit()

        retry = sessions()
        retry.add(_appointment(minimal_data, "paciente_b", idempotency_key="req-abc-123"))
        with pytest.raises(IntegrityError):
            retry.commit()
        retry.rollback()

    def test_different_keys_do_not_interfere(
        self, sessions: Callable[[], Session], minimal_data: dict[str, int]
    ) -> None:
        session_ = sessions()
        first = _appointment(minimal_data, "paciente_a", idempotency_key="req-1")
        session_.add(first)
        session_.commit()
        first.status = AppointmentState.CANCELLED
        first.cancellation_reason = "cambio de plan"
        session_.commit()

        session_.add(_appointment(minimal_data, "paciente_b", idempotency_key="req-2"))
        session_.commit()

    def test_several_appointments_may_have_no_key(
        self, sessions: Callable[[], Session], minimal_data: dict[str, int]
    ) -> None:
        """NULL is not equal to NULL in SQL: appointments created without an
        idempotency key must not collide with each other."""
        session_ = sessions()
        first = _appointment(minimal_data, "paciente_a")
        session_.add(first)
        session_.commit()
        first.status = AppointmentState.CANCELLED
        first.cancellation_reason = "x"
        session_.commit()

        session_.add(_appointment(minimal_data, "paciente_b"))
        session_.commit()


class TestOptimisticLocking:
    def test_two_writes_on_the_same_slot_detect_the_conflict(
        self, sessions: Callable[[], Session], minimal_data: dict[str, int]
    ) -> None:
        agent_a, agent_b = sessions(), sessions()
        slot_a = agent_a.get(AgendaSlot, minimal_data["slot_id"])
        slot_b = agent_b.get(AgendaSlot, minimal_data["slot_id"])
        assert slot_a is not None and slot_b is not None
        assert slot_a.version_id == slot_b.version_id  # both read the same version

        slot_a.status = SlotState.BUSY
        agent_a.commit()

        slot_b.status = SlotState.BLOCKED
        with pytest.raises(StaleDataError):
            agent_b.commit()
        agent_b.rollback()

    def test_the_version_advances_on_every_write(
        self, sessions: Callable[[], Session], minimal_data: dict[str, int]
    ) -> None:
        session_ = sessions()
        slot = session_.get(AgendaSlot, minimal_data["slot_id"])
        assert slot is not None
        initial = slot.version_id

        slot.status = SlotState.BUSY
        session_.commit()
        assert slot.version_id == initial + 1

        slot.status = SlotState.FREE
        session_.commit()
        assert slot.version_id == initial + 2


class TestSlotUniqueness:
    def test_a_professional_cannot_have_two_slots_at_the_same_time(
        self, sessions: Callable[[], Session], minimal_data: dict[str, int]
    ) -> None:
        session_ = sessions()
        original = session_.get(AgendaSlot, minimal_data["slot_id"])
        assert original is not None

        another = sessions()
        another.add(
            AgendaSlot(
                professional_id=original.professional_id,
                day=original.day,
                start=original.start,
                end=original.end,
            )
        )
        with pytest.raises(IntegrityError):
            another.commit()
        another.rollback()
