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
    validate_patient_afiliacion,
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


def historial_de(session_: Session, cita_id: int) -> list[AppointmentHistory]:
    return list(
        session_.scalars(
            select(AppointmentHistory)
            .where(AppointmentHistory.cita_id == cita_id)
            .order_by(AppointmentHistory.id)
        )
    )


# --------------------------------------------------------------------------- #
# Lookups
# --------------------------------------------------------------------------- #


class TestBusquedas:
    def test_busca_por_documento_exacto(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        s = sessions()
        assert [p.id for p in search_patients(s, documento="11111111")] == [scenario.ana_id]

    def test_el_documento_no_hace_match_parcial(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        """A partial document match is how you hand an agent the wrong record."""
        assert search_patients(sessions(), documento="1111") == []

    def test_busca_por_nombre_sin_distinguir_mayusculas(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        assert [p.id for p in search_patients(sessions(), nombre="ANA gómez")] == [scenario.ana_id]

    def test_busca_por_fragmento_de_nombre(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        assert len(search_patients(sessions(), nombre="Ruiz")) == 2

    def test_sin_criterio_lanza_error_accionable(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        with pytest.raises(PatientNotFound) as exc:
            search_patients(sessions())
        assert "search_patients" in (exc.value.sugerencia or "")

    def test_respeta_el_limite(self, sessions: Callable[[], Session], scenario: Scenario) -> None:
        assert len(search_patients(sessions(), nombre="a", limite=2)) <= 2

    def test_paciente_inexistente(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        with pytest.raises(PatientNotFound):
            get_patient(sessions(), 999_999)

    def test_cita_inexistente(self, sessions: Callable[[], Session], scenario: Scenario) -> None:
        with pytest.raises(AppointmentNotFound) as exc:
            get_appointment(sessions(), 999_999)
        assert "list_patient_appointments" in (exc.value.sugerencia or "")

    def test_hay_clinica(self, sessions: Callable[[], Session], scenario: Scenario) -> None:
        assert get_clinic(sessions()).id == scenario.clinica_id


class TestDisponibilidad:
    def test_devuelve_solo_cupos_libres_y_futuros(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        free_slots = list_available_slots(sessions())
        ids = {s.slot_id for s in free_slots}
        assert scenario.slot_pasado_id not in ids
        assert ids >= set(scenario.slots_general[:3])

    def test_filtra_por_especialidad(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        free_slots = list_available_slots(sessions(), especialidad=Specialty.ORTHODONTICS)
        assert free_slots
        assert all(s.especialidad is Specialty.ORTHODONTICS for s in free_slots)

    def test_filtra_por_fecha(self, sessions: Callable[[], Session], scenario: Scenario) -> None:
        assert list_available_slots(sessions(), fecha=scenario.fecha_futura)
        assert list_available_slots(sessions(), fecha=date(2000, 1, 3)) == []

    def test_filtra_por_profesional(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        free_slots = list_available_slots(sessions(), profesional_id=scenario.orto_id)
        assert all(s.profesional_id == scenario.orto_id for s in free_slots)

    def test_profesional_inexistente_es_un_error_no_una_lista_vacia(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        with pytest.raises(ProfessionalNotFound):
            list_available_slots(sessions(), profesional_id=999_999)

    def test_los_cupos_vienen_en_orden_cronologico(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        free_slots = list_available_slots(sessions())
        assert [s.inicio for s in free_slots] == sorted(s.inicio for s in free_slots)

    def test_la_etiqueta_esta_en_hora_local(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        label = list_available_slots(sessions())[0].label
        assert label.startswith(str(scenario.fecha_futura))


class TestAfiliacionYCartera:
    def test_afiliacion_activa(self, sessions: Callable[[], Session], scenario: Scenario) -> None:
        r = validate_patient_afiliacion(sessions(), scenario.ana_id)
        assert r.activa and r.requiere_copago

    def test_afiliacion_inactiva_cae_a_particular(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        r = validate_patient_afiliacion(sessions(), scenario.bruno_id)
        assert not r.activa
        assert r.regimen_efectivo is Regimen.PARTICULAR

    def test_cartera_al_dia(self, sessions: Callable[[], Session], scenario: Scenario) -> None:
        assert get_cartera(sessions(), scenario.ana_id).estado is CarteraState.AL_DIA

    def test_cartera_en_mora(self, sessions: Callable[[], Session], scenario: Scenario) -> None:
        resumen = get_cartera(sessions(), scenario.deudor_id)
        assert resumen.estado is CarteraState.EN_MORA
        assert resumen.total_vencido == Decimal("180000")
        assert resumen.dias_mora_maximo >= 74


# --------------------------------------------------------------------------- #
# Booking
# --------------------------------------------------------------------------- #


class TestAgendar:
    def test_crea_la_cita_ocupa_el_cupo_y_audita(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        s = sessions()
        result = book_appointment(
            s, paciente_id=scenario.ana_id, slot_id=scenario.slots_general[0], usuario=ACTOR
        )
        s.commit()

        assert result.cita.estado is AppointmentState.SCHEDULED
        assert result.cita.creada_por == ACTOR
        assert s.get(AgendaSlot, scenario.slots_general[0]).estado is SlotState.BUSY

        historial = historial_de(s, result.cita.id)
        assert len(historial) == 1
        assert historial[0].estado_anterior is None
        assert historial[0].estado_nuevo is AppointmentState.SCHEDULED
        assert historial[0].usuario == ACTOR

    def test_devuelve_la_afiliacion_para_informar_la_tarifa(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        result = book_appointment(
            sessions(),
            paciente_id=scenario.bruno_id,
            slot_id=scenario.slots_general[0],
            usuario=ACTOR,
        )
        assert result.afiliacion.regimen_efectivo is Regimen.PARTICULAR

    def test_la_mora_alerta_pero_no_bloquea(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        """The rule from §2.3 that a naive implementation gets wrong."""
        result = book_appointment(
            sessions(),
            paciente_id=scenario.deudor_id,
            slot_id=scenario.slots_general[0],
            usuario=ACTOR,
        )
        assert result.cita.id is not None
        assert result.alerta_cartera is not None
        assert "can still be booked" in result.alerta_cartera

    def test_sin_mora_no_hay_alerta(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        result = book_appointment(
            sessions(),
            paciente_id=scenario.ana_id,
            slot_id=scenario.slots_general[0],
            usuario=ACTOR,
        )
        assert result.alerta_cartera is None

    def test_un_cupo_ocupado_sugiere_alternativas_concretas(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        s = sessions()
        book_appointment(
            s, paciente_id=scenario.ana_id, slot_id=scenario.slots_general[0], usuario=ACTOR
        )
        s.commit()

        with pytest.raises(SlotUnavailable) as exc:
            book_appointment(
                s,
                paciente_id=scenario.carla_id,
                slot_id=scenario.slots_general[0],
                usuario=ACTOR,
            )
        # An LLM that receives named alternatives recovers on its own turn.
        assert exc.value.detalles["alternativas"]
        assert "closest free slots" in (exc.value.sugerencia or "")

    def test_un_cupo_pasado_se_rechaza(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        with pytest.raises(SlotInThePast):
            book_appointment(
                sessions(),
                paciente_id=scenario.ana_id,
                slot_id=scenario.slot_pasado_id,
                usuario=ACTOR,
            )

    def test_un_cupo_inexistente_se_rechaza(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        with pytest.raises(SlotNotFound):
            book_appointment(
                sessions(), paciente_id=scenario.ana_id, slot_id=999_999, usuario=ACTOR
            )

    def test_la_especialidad_esperada_se_verifica(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        """Guards against the model picking a slot from the wrong list."""
        with pytest.raises(SpecialtyMismatch) as exc:
            book_appointment(
                sessions(),
                paciente_id=scenario.ana_id,
                slot_id=scenario.slots_general[0],
                usuario=ACTOR,
                especialidad_esperada=Specialty.ORTHODONTICS,
            )
        assert exc.value.detalles["especialidad_del_cupo"] == "general_dentistry"

    def test_la_especialidad_correcta_pasa(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        book_appointment(
            sessions(),
            paciente_id=scenario.ana_id,
            slot_id=scenario.slots_orto[0],
            usuario=ACTOR,
            especialidad_esperada=Specialty.ORTHODONTICS,
        )

    def test_no_se_puede_agendar_dos_citas_solapadas_al_mismo_paciente(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        """Same time slot, two professionals: the patient cannot be in both."""
        s = sessions()
        book_appointment(
            s, paciente_id=scenario.ana_id, slot_id=scenario.slots_general[0], usuario=ACTOR
        )
        s.commit()
        with pytest.raises(PatientAlreadyBooked) as exc:
            book_appointment(
                s, paciente_id=scenario.ana_id, slot_id=scenario.slots_orto[0], usuario=ACTOR
            )
        assert "cita_existente_id" in exc.value.detalles

    def test_horarios_distintos_si_se_permiten(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        s = sessions()
        book_appointment(
            s, paciente_id=scenario.ana_id, slot_id=scenario.slots_general[0], usuario=ACTOR
        )
        s.commit()
        book_appointment(
            s, paciente_id=scenario.ana_id, slot_id=scenario.slots_general[1], usuario=ACTOR
        )
        s.commit()
        assert len(list_patient_appointments(s, scenario.ana_id)) == 2


class TestIdempotencia:
    def test_la_misma_clave_devuelve_la_misma_cita(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        s = sessions()
        primera = book_appointment(
            s,
            paciente_id=scenario.ana_id,
            slot_id=scenario.slots_general[0],
            usuario=ACTOR,
            idempotency_key="peticion-1",
        )
        s.commit()
        segunda = book_appointment(
            s,
            paciente_id=scenario.ana_id,
            slot_id=scenario.slots_general[1],
            usuario=ACTOR,
            idempotency_key="peticion-1",
        )
        assert segunda.cita.id == primera.cita.id
        assert segunda.reutilizada is True
        assert s.scalar(select(func.count()).select_from(Appointment)) == 1

    def test_sin_clave_cada_llamada_crea_una_cita(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        s = sessions()
        book_appointment(
            s, paciente_id=scenario.ana_id, slot_id=scenario.slots_general[0], usuario=ACTOR
        )
        s.commit()
        book_appointment(
            s, paciente_id=scenario.carla_id, slot_id=scenario.slots_general[1], usuario=ACTOR
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
        s, paciente_id=scenario.ana_id, slot_id=scenario.slots_general[0], usuario=ACTOR
    )
    s.commit()
    return s, result.cita.id


class TestConfirmar:
    def test_confirma_y_audita(self, booked_appointment: tuple[Session, int]) -> None:
        s, cita_id = booked_appointment
        result = confirm_appointment(s, cita_id, usuario=ACTOR)
        s.commit()
        assert result.cita.estado is AppointmentState.CONFIRMED
        historial = historial_de(s, cita_id)
        assert historial[-1].estado_anterior is AppointmentState.SCHEDULED
        assert historial[-1].estado_nuevo is AppointmentState.CONFIRMED

    def test_no_libera_el_cupo_ni_genera_cargo(
        self, booked_appointment: tuple[Session, int]
    ) -> None:
        s, cita_id = booked_appointment
        result = confirm_appointment(s, cita_id, usuario=ACTOR)
        assert not result.effects.libera_slot
        assert result.created_charge is None

    def test_confirmar_dos_veces_falla_con_error_tipado(
        self, booked_appointment: tuple[Session, int]
    ) -> None:
        s, cita_id = booked_appointment
        confirm_appointment(s, cita_id, usuario=ACTOR)
        s.commit()
        with pytest.raises(InvalidTransition):
            confirm_appointment(s, cita_id, usuario=ACTOR)


class TestCancelar:
    def test_cancela_libera_el_cupo_y_guarda_el_motivo(
        self, booked_appointment: tuple[Session, int], scenario: Scenario
    ) -> None:
        s, cita_id = booked_appointment
        result = cancel_appointment(s, cita_id, motivo="El paciente viajó", usuario=ACTOR)
        s.commit()

        assert result.cita.estado is AppointmentState.CANCELLED
        assert result.cita.motivo_cancelacion == "El paciente viajó"
        assert result.effects.libera_slot
        assert s.get(AgendaSlot, scenario.slots_general[0]).estado is SlotState.FREE
        assert historial_de(s, cita_id)[-1].motivo == "El paciente viajó"

    def test_sin_motivo_se_rechaza(self, booked_appointment: tuple[Session, int]) -> None:
        s, cita_id = booked_appointment
        with pytest.raises(ReasonRequired):
            change_state(s, cita_id, AppointmentState.CANCELLED, usuario=ACTOR)

    def test_el_cupo_liberado_vuelve_a_estar_disponible(
        self, booked_appointment: tuple[Session, int], scenario: Scenario
    ) -> None:
        s, cita_id = booked_appointment
        cancel_appointment(s, cita_id, motivo="cambio de planes", usuario=ACTOR)
        s.commit()
        free_slots = {x.slot_id for x in list_available_slots(s)}
        assert scenario.slots_general[0] in free_slots

    def test_sin_lista_de_espera_no_hay_siguiente(
        self, booked_appointment: tuple[Session, int]
    ) -> None:
        """Cancelling with an empty queue is normal, not an error."""
        s, cita_id = booked_appointment
        result = cancel_appointment(s, cita_id, motivo="x", usuario=ACTOR)
        assert result.siguiente_en_espera is None

    def test_con_lista_de_espera_devuelve_al_siguiente(
        self, booked_appointment: tuple[Session, int], scenario: Scenario
    ) -> None:
        s, cita_id = booked_appointment
        join_waiting_list(
            s,
            paciente_id=scenario.carla_id,
            especialidad=Specialty.GENERAL_DENTISTRY,
        )
        s.commit()
        result = cancel_appointment(s, cita_id, motivo="x", usuario=ACTOR)
        assert result.siguiente_en_espera is not None
        assert result.siguiente_en_espera.paciente_id == scenario.carla_id

    def test_no_se_ofrece_el_cupo_a_quien_lo_liberó(
        self, booked_appointment: tuple[Session, int], scenario: Scenario
    ) -> None:
        s, cita_id = booked_appointment
        join_waiting_list(s, paciente_id=scenario.ana_id, especialidad=Specialty.GENERAL_DENTISTRY)
        s.commit()
        result = cancel_appointment(s, cita_id, motivo="x", usuario=ACTOR)
        assert result.siguiente_en_espera is None


class TestAsistencia:
    def test_flujo_completo_hasta_atendida_genera_cargo(
        self, booked_appointment: tuple[Session, int], scenario: Scenario
    ) -> None:
        s, cita_id = booked_appointment
        confirm_appointment(s, cita_id, usuario=ACTOR)
        record_attendance(s, cita_id, AppointmentState.WAITING, usuario=ACTOR)
        result = record_attendance(s, cita_id, AppointmentState.ATTENDED, usuario=ACTOR)
        s.commit()

        assert result.cita.estado is AppointmentState.ATTENDED
        assert result.created_charge is not None
        # Ana is contributory level 1 → cuota moderadora, not full tariff.
        assert result.created_charge.concepto is ChargeConcept.CUOTA_MODERADORA
        assert result.created_charge.monto == Decimal("5500")
        assert result.created_charge.estado is ChargeState.PENDING

    def test_el_cargo_vence_a_30_dias(self, booked_appointment: tuple[Session, int]) -> None:
        s, cita_id = booked_appointment
        confirm_appointment(s, cita_id, usuario=ACTOR)
        record_attendance(s, cita_id, AppointmentState.WAITING, usuario=ACTOR)
        result = record_attendance(s, cita_id, AppointmentState.ATTENDED, usuario=ACTOR)
        cita = result.cita
        assert result.created_charge is not None
        esperado = cita.slot.inicio.date() + timedelta(days=30)
        assert abs((result.created_charge.vencimiento - esperado).days) <= 1

    def test_afiliacion_inactiva_liquida_tarifa_particular(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        s = sessions()
        cita = book_appointment(
            s, paciente_id=scenario.bruno_id, slot_id=scenario.slots_general[0], usuario=ACTOR
        ).cita
        confirm_appointment(s, cita.id, usuario=ACTOR)
        record_attendance(s, cita.id, AppointmentState.WAITING, usuario=ACTOR)
        result = record_attendance(s, cita.id, AppointmentState.ATTENDED, usuario=ACTOR)
        s.commit()
        assert result.created_charge is not None
        assert result.created_charge.concepto is ChargeConcept.PARTICULAR
        assert result.created_charge.monto == Decimal("120000")

    def test_no_show_desde_confirmada_penaliza(
        self, booked_appointment: tuple[Session, int]
    ) -> None:
        s, cita_id = booked_appointment
        confirm_appointment(s, cita_id, usuario=ACTOR)
        result = record_attendance(s, cita_id, AppointmentState.NO_SHOW, usuario=ACTOR)
        s.commit()
        assert result.created_charge is not None
        assert result.created_charge.concepto is ChargeConcept.NO_SHOW
        assert result.created_charge.monto == Decimal("40000")

    def test_no_show_sin_confirmar_no_penaliza(
        self, booked_appointment: tuple[Session, int]
    ) -> None:
        """The default policy only charges a patient who had committed."""
        s, cita_id = booked_appointment
        result = record_attendance(s, cita_id, AppointmentState.NO_SHOW, usuario=ACTOR)
        s.commit()
        assert result.created_charge is None

    def test_el_no_show_libera_el_cupo(
        self, booked_appointment: tuple[Session, int], scenario: Scenario
    ) -> None:
        s, cita_id = booked_appointment
        record_attendance(s, cita_id, AppointmentState.NO_SHOW, usuario=ACTOR)
        s.commit()
        assert s.get(AgendaSlot, scenario.slots_general[0]).estado is SlotState.FREE

    def test_saltarse_en_espera_se_rechaza(self, booked_appointment: tuple[Session, int]) -> None:
        s, cita_id = booked_appointment
        confirm_appointment(s, cita_id, usuario=ACTOR)
        with pytest.raises(InvalidTransition):
            record_attendance(s, cita_id, AppointmentState.ATTENDED, usuario=ACTOR)

    def test_el_cargo_aparece_en_la_cartera(
        self, booked_appointment: tuple[Session, int], scenario: Scenario
    ) -> None:
        s, cita_id = booked_appointment
        confirm_appointment(s, cita_id, usuario=ACTOR)
        record_attendance(s, cita_id, AppointmentState.WAITING, usuario=ACTOR)
        record_attendance(s, cita_id, AppointmentState.ATTENDED, usuario=ACTOR)
        s.commit()
        resumen = get_cartera(s, scenario.ana_id)
        assert resumen.cantidad_cargos == 1
        assert resumen.total_pendiente == Decimal("5500")


class TestReprogramar:
    def test_libera_el_viejo_ocupa_el_nuevo_y_encadena(
        self, booked_appointment: tuple[Session, int], scenario: Scenario
    ) -> None:
        s, cita_id = booked_appointment
        result = reschedule_appointment(
            s, cita_id, scenario.slots_general[2], usuario=ACTOR, motivo="Choque de agenda"
        )
        s.commit()

        original = get_appointment(s, cita_id)
        assert original.estado is AppointmentState.RESCHEDULED
        assert s.get(AgendaSlot, scenario.slots_general[0]).estado is SlotState.FREE
        assert s.get(AgendaSlot, scenario.slots_general[2]).estado is SlotState.BUSY

        nueva = result.cita
        assert nueva.id != cita_id
        assert nueva.estado is AppointmentState.SCHEDULED
        assert nueva.cita_origen_id == cita_id

    def test_la_nueva_cita_tiene_su_propio_historial(
        self, booked_appointment: tuple[Session, int], scenario: Scenario
    ) -> None:
        s, cita_id = booked_appointment
        nueva = reschedule_appointment(s, cita_id, scenario.slots_general[2], usuario=ACTOR).cita
        s.commit()
        historial = historial_de(s, nueva.id)
        assert len(historial) == 1
        assert "Rescheduled from" in (historial[0].motivo or "")

    def test_no_reprograma_a_un_cupo_ocupado(
        self, booked_appointment: tuple[Session, int], scenario: Scenario
    ) -> None:
        s, cita_id = booked_appointment
        book_appointment(
            s, paciente_id=scenario.carla_id, slot_id=scenario.slots_general[3], usuario=ACTOR
        )
        s.commit()
        with pytest.raises(SlotUnavailable):
            reschedule_appointment(s, cita_id, scenario.slots_general[3], usuario=ACTOR)

    def test_no_reprograma_al_pasado(
        self, booked_appointment: tuple[Session, int], scenario: Scenario
    ) -> None:
        s, cita_id = booked_appointment
        with pytest.raises(SlotInThePast):
            reschedule_appointment(s, cita_id, scenario.slot_pasado_id, usuario=ACTOR)

    def test_una_cita_reprogramada_es_final(
        self, booked_appointment: tuple[Session, int], scenario: Scenario
    ) -> None:
        s, cita_id = booked_appointment
        reschedule_appointment(s, cita_id, scenario.slots_general[2], usuario=ACTOR)
        s.commit()
        with pytest.raises(InvalidTransition):
            confirm_appointment(s, cita_id, usuario=ACTOR)


# --------------------------------------------------------------------------- #
# Waiting list
# --------------------------------------------------------------------------- #


class TestListaEspera:
    def test_inscribe_y_evita_duplicados(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        s = sessions()
        join_waiting_list(s, paciente_id=scenario.ana_id, especialidad=Specialty.ORTHODONTICS)
        s.commit()
        with pytest.raises(AlreadyOnWaitingList):
            join_waiting_list(s, paciente_id=scenario.ana_id, especialidad=Specialty.ORTHODONTICS)

    def test_paciente_inexistente_no_se_inscribe(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        with pytest.raises(PatientNotFound):
            join_waiting_list(sessions(), paciente_id=999_999, especialidad=Specialty.ORTHODONTICS)

    def test_ofrece_el_cupo_al_primero_de_la_cola(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        s = sessions()
        join_waiting_list(s, paciente_id=scenario.ana_id, especialidad=Specialty.ORTHODONTICS)
        join_waiting_list(
            s,
            paciente_id=scenario.carla_id,
            especialidad=Specialty.ORTHODONTICS,
            prioridad=WaitingListPriority.URGENT,
        )
        s.commit()

        oferta = offer_slot_to_waiting_list(s, scenario.slots_orto[0], usuario=ACTOR)
        s.commit()
        # Urgency jumps the queue even though Ana enrolled first.
        assert oferta.paciente.id == scenario.carla_id
        assert oferta.posicion_original == 1
        assert oferta.entry.estado is WaitingListState.OFFERED
        assert oferta.entry.slot_ofrecido_id == scenario.slots_orto[0]

    def test_ofrecer_no_agenda_nada(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        """Offering is a contact instruction, not a booking. Booking the slot is
        a separate decision that gets its own approval."""
        s = sessions()
        join_waiting_list(s, paciente_id=scenario.ana_id, especialidad=Specialty.ORTHODONTICS)
        s.commit()
        offer_slot_to_waiting_list(s, scenario.slots_orto[0], usuario=ACTOR)
        s.commit()
        assert s.scalar(select(func.count()).select_from(Appointment)) == 0
        assert s.get(AgendaSlot, scenario.slots_orto[0]).estado is SlotState.FREE

    def test_lista_vacia_da_un_error_accionable(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        with pytest.raises(WaitingListEmpty) as exc:
            offer_slot_to_waiting_list(sessions(), scenario.slots_orto[0], usuario=ACTOR)
        assert "check_availability" in (exc.value.sugerencia or "")

    def test_no_ofrece_de_otra_especialidad(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        s = sessions()
        join_waiting_list(s, paciente_id=scenario.ana_id, especialidad=Specialty.ENDODONTICS)
        s.commit()
        with pytest.raises(WaitingListEmpty):
            offer_slot_to_waiting_list(s, scenario.slots_orto[0], usuario=ACTOR)

    def test_una_entrada_ofrecida_sale_de_la_cola(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        s = sessions()
        join_waiting_list(s, paciente_id=scenario.ana_id, especialidad=Specialty.ORTHODONTICS)
        s.commit()
        offer_slot_to_waiting_list(s, scenario.slots_orto[0], usuario=ACTOR)
        s.commit()
        active = s.scalars(
            select(WaitingList).where(WaitingList.estado == WaitingListState.ACTIVE)
        ).all()
        assert active == []


# --------------------------------------------------------------------------- #
# Clinical
# --------------------------------------------------------------------------- #


class TestMotivoDeConsulta:
    def test_con_consentimiento_registra_y_audita(
        self, booked_appointment: tuple[Session, int]
    ) -> None:
        s, cita_id = booked_appointment
        cita = record_visit_reason(
            s, cita_id, "Dolor en molar inferior derecho", usuario="odontologa@clinica.test"
        )
        s.commit()

        assert cita.motivo == "Dolor en molar inferior derecho"
        assert cita.motivo_registrado_por == "odontologa@clinica.test"
        assert cita.motivo_registrado_en is not None

        ultimo = historial_de(s, cita_id)[-1]
        assert "clinical data" in (ultimo.motivo or "")
        assert ultimo.usuario == "odontologa@clinica.test"

    def test_sin_consentimiento_se_rechaza(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        """Carla has no consent on file. Scope alone must not be enough."""
        s = sessions()
        cita = book_appointment(
            s, paciente_id=scenario.carla_id, slot_id=scenario.slots_general[0], usuario=ACTOR
        ).cita
        s.commit()
        with pytest.raises(ConsentRequired) as exc:
            record_visit_reason(s, cita.id, "Dolor", usuario=ACTOR)
        assert "2654" in (exc.value.sugerencia or "")
        assert exc.value.http_status == 403

    def test_el_rechazo_no_deja_rastro_del_motivo(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        s = sessions()
        cita = book_appointment(
            s, paciente_id=scenario.carla_id, slot_id=scenario.slots_general[0], usuario=ACTOR
        ).cita
        s.commit()
        with pytest.raises(ConsentRequired):
            record_visit_reason(s, cita.id, "Dolor agudo", usuario=ACTOR)
        s.rollback()
        assert get_appointment(s, cita.id).motivo is None

    def test_el_registro_clinico_no_cambia_el_estado(
        self, booked_appointment: tuple[Session, int]
    ) -> None:
        s, cita_id = booked_appointment
        antes = get_appointment(s, cita_id).estado
        record_visit_reason(s, cita_id, "Control de rutina", usuario=ACTOR)
        s.commit()
        assert get_appointment(s, cita_id).estado is antes


# --------------------------------------------------------------------------- #
# Day view
# --------------------------------------------------------------------------- #


class TestAgendaDelDia:
    def test_lista_las_citas_del_dia_en_orden(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        s = sessions()
        book_appointment(
            s, paciente_id=scenario.ana_id, slot_id=scenario.slots_general[1], usuario=ACTOR
        )
        book_appointment(
            s, paciente_id=scenario.carla_id, slot_id=scenario.slots_general[0], usuario=ACTOR
        )
        s.commit()
        citas = agenda_for_day(s, scenario.fecha_futura)
        assert [c.slot.inicio for c in citas] == sorted(c.slot.inicio for c in citas)
        assert len(citas) == 2

    def test_un_dia_sin_citas_devuelve_vacio(
        self, sessions: Callable[[], Session], scenario: Scenario
    ) -> None:
        assert agenda_for_day(sessions(), date(2000, 1, 3)) == []

    def test_incluye_las_canceladas(
        self, booked_appointment: tuple[Session, int], scenario: Scenario
    ) -> None:
        """The front desk needs to see what was cancelled today, not a clean slate."""
        s, cita_id = booked_appointment
        cancel_appointment(s, cita_id, motivo="x", usuario=ACTOR)
        s.commit()
        citas = agenda_for_day(s, scenario.fecha_futura)
        assert [c.estado for c in citas] == [AppointmentState.CANCELLED]


class TestAuditoriaCompleta:
    def test_cada_transicion_deja_exactamente_una_fila(
        self, booked_appointment: tuple[Session, int]
    ) -> None:
        s, cita_id = booked_appointment
        confirm_appointment(s, cita_id, usuario=ACTOR)
        record_attendance(s, cita_id, AppointmentState.WAITING, usuario=ACTOR)
        record_attendance(s, cita_id, AppointmentState.ATTENDED, usuario=ACTOR)
        s.commit()

        historial = historial_de(s, cita_id)
        assert [h.estado_nuevo for h in historial] == [
            AppointmentState.SCHEDULED,
            AppointmentState.CONFIRMED,
            AppointmentState.WAITING,
            AppointmentState.ATTENDED,
        ]
        assert all(h.usuario == ACTOR for h in historial)

    def test_una_transicion_rechazada_no_deja_rastro(
        self, booked_appointment: tuple[Session, int]
    ) -> None:
        s, cita_id = booked_appointment
        antes = len(historial_de(s, cita_id))
        with pytest.raises(InvalidTransition):
            record_attendance(s, cita_id, AppointmentState.ATTENDED, usuario=ACTOR)
        s.rollback()
        assert len(historial_de(s, cita_id)) == antes

    def test_el_actor_queda_registrado_por_operacion(
        self, booked_appointment: tuple[Session, int]
    ) -> None:
        """An audit trail with the same user in every row is not an audit trail."""
        s, cita_id = booked_appointment
        confirm_appointment(s, cita_id, usuario="ana@clinica.test")
        cancel_appointment(s, cita_id, motivo="x", usuario="jefe@clinica.test")
        s.commit()
        usuarios = [h.usuario for h in historial_de(s, cita_id)]
        assert usuarios == [ACTOR, "ana@clinica.test", "jefe@clinica.test"]

    def test_todo_cargo_derivado_de_una_cita_queda_ligado_a_ella(
        self, booked_appointment: tuple[Session, int]
    ) -> None:
        """A charge produced by a transition must point back at it, so the
        patient can be told *what* they are being billed for. (Standalone
        charges with no appointment are legitimate, imported debt for instance,
        so the assertion is scoped to the generated one.)"""
        s, cita_id = booked_appointment
        confirm_appointment(s, cita_id, usuario=ACTOR)
        result = record_attendance(s, cita_id, AppointmentState.NO_SHOW, usuario=ACTOR)
        s.commit()
        assert result.created_charge is not None
        assert result.created_charge.cita_id == cita_id
        assert all(c.paciente_id is not None for c in s.scalars(select(Charge)))
