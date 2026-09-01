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


def _cita(payload: dict[str, int], patient: str, **extra: object) -> Appointment:
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


class TestDobleReserva:
    def test_dos_agentes_sobre_el_mismo_cupo_solo_uno_gana(
        self, sessions: Callable[[], Session], minimal_data: dict[str, int]
    ) -> None:
        agente_a, agente_b = sessions(), sessions()

        agente_a.add(_cita(minimal_data, "paciente_a"))
        agente_a.commit()  # A gets there first

        agente_b.add(_cita(minimal_data, "paciente_b"))
        with pytest.raises(IntegrityError):
            agente_b.commit()
        agente_b.rollback()

        # Exactly one appointment survives. Not two, not zero.
        session_ = sessions()
        appointments = session_.query(Appointment).filter_by(slot_id=minimal_data["slot_id"]).all()
        assert len(appointments) == 1
        assert appointments[0].patient_id == minimal_data["paciente_a"]

    def test_cancelar_libera_el_cupo_para_otro_paciente(
        self, sessions: Callable[[], Session], minimal_data: dict[str, int]
    ) -> None:
        """The uniqueness is partial: a cancelled appointment must not keep the
        slot hostage, otherwise cancellation would be pointless."""
        session_ = sessions()
        primera = _cita(minimal_data, "paciente_a")
        session_.add(primera)
        session_.commit()

        primera.status = AppointmentState.CANCELLED
        primera.cancellation_reason = "El paciente viajó"
        session_.commit()

        session_.add(_cita(minimal_data, "paciente_b"))
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
    def test_todo_estado_que_ocupa_bloquea_una_segunda_cita(
        self,
        sessions: Callable[[], Session],
        minimal_data: dict[str, int],
        status: AppointmentState,
    ) -> None:
        session_ = sessions()
        session_.add(_cita(minimal_data, "paciente_a", status=status))
        session_.commit()

        otra = sessions()
        otra.add(_cita(minimal_data, "paciente_b"))
        with pytest.raises(IntegrityError):
            otra.commit()
        otra.rollback()

    @pytest.mark.parametrize(
        "status",
        [AppointmentState.CANCELLED, AppointmentState.RESCHEDULED, AppointmentState.NO_SHOW],
    )
    def test_ningun_estado_liberador_bloquea_una_segunda_cita(
        self,
        sessions: Callable[[], Session],
        minimal_data: dict[str, int],
        status: AppointmentState,
    ) -> None:
        session_ = sessions()
        session_.add(_cita(minimal_data, "paciente_a", status=status))
        session_.commit()

        otra = sessions()
        otra.add(_cita(minimal_data, "paciente_b"))
        otra.commit()  # must not raise


class TestIdempotencia:
    def test_reenviar_la_misma_clave_no_crea_un_duplicado(
        self, sessions: Callable[[], Session], minimal_data: dict[str, int]
    ) -> None:
        """An agent that retries a timed-out booking must not end up with two
        appointments. The database is what makes that a guarantee."""
        session_ = sessions()
        session_.add(_cita(minimal_data, "paciente_a", idempotency_key="req-abc-123"))
        session_.commit()

        reintento = sessions()
        reintento.add(_cita(minimal_data, "paciente_b", idempotency_key="req-abc-123"))
        with pytest.raises(IntegrityError):
            reintento.commit()
        reintento.rollback()

    def test_claves_distintas_no_se_estorban(
        self, sessions: Callable[[], Session], minimal_data: dict[str, int]
    ) -> None:
        session_ = sessions()
        primera = _cita(minimal_data, "paciente_a", idempotency_key="req-1")
        session_.add(primera)
        session_.commit()
        primera.status = AppointmentState.CANCELLED
        primera.cancellation_reason = "cambio de plan"
        session_.commit()

        session_.add(_cita(minimal_data, "paciente_b", idempotency_key="req-2"))
        session_.commit()

    def test_varias_citas_pueden_no_tener_clave(
        self, sessions: Callable[[], Session], minimal_data: dict[str, int]
    ) -> None:
        """NULL is not equal to NULL in SQL: appointments created without an
        idempotency key must not collide with each other."""
        session_ = sessions()
        primera = _cita(minimal_data, "paciente_a")
        session_.add(primera)
        session_.commit()
        primera.status = AppointmentState.CANCELLED
        primera.cancellation_reason = "x"
        session_.commit()

        session_.add(_cita(minimal_data, "paciente_b"))
        session_.commit()


class TestBloqueoOptimista:
    def test_dos_escrituras_sobre_el_mismo_slot_detectan_el_conflicto(
        self, sessions: Callable[[], Session], minimal_data: dict[str, int]
    ) -> None:
        agente_a, agente_b = sessions(), sessions()
        slot_a = agente_a.get(AgendaSlot, minimal_data["slot_id"])
        slot_b = agente_b.get(AgendaSlot, minimal_data["slot_id"])
        assert slot_a is not None and slot_b is not None
        assert slot_a.version_id == slot_b.version_id  # both read the same version

        slot_a.status = SlotState.BUSY
        agente_a.commit()

        slot_b.status = SlotState.BLOCKED
        with pytest.raises(StaleDataError):
            agente_b.commit()
        agente_b.rollback()

    def test_la_version_avanza_en_cada_escritura(
        self, sessions: Callable[[], Session], minimal_data: dict[str, int]
    ) -> None:
        session_ = sessions()
        slot = session_.get(AgendaSlot, minimal_data["slot_id"])
        assert slot is not None
        inicial = slot.version_id

        slot.status = SlotState.BUSY
        session_.commit()
        assert slot.version_id == inicial + 1

        slot.status = SlotState.FREE
        session_.commit()
        assert slot.version_id == inicial + 2


class TestUnicidadDeSlot:
    def test_un_profesional_no_puede_tener_dos_slots_a_la_misma_hora(
        self, sessions: Callable[[], Session], minimal_data: dict[str, int]
    ) -> None:
        session_ = sessions()
        original = session_.get(AgendaSlot, minimal_data["slot_id"])
        assert original is not None

        otra = sessions()
        otra.add(
            AgendaSlot(
                professional_id=original.professional_id,
                day=original.day,
                start=original.start,
                end=original.end,
            )
        )
        with pytest.raises(IntegrityError):
            otra.commit()
        otra.rollback()
