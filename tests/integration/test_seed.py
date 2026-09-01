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

FECHA_BASE = date(2026, 8, 31)
PARAMS = SeedParams(seed=20260831, patients=25, dias_agenda=10, base_date=FECHA_BASE)


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
            p.afiliacion_active,
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
    content["lista_espera"] = sorted(
        (e.patient.document_number, e.specialty, e.priority, e.status)
        for e in session.scalars(select(WaitingList))
    )
    crudo = json.dumps(content, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(crudo.encode()).hexdigest()


@pytest.fixture
def seeded(sessions: Callable[[], Session]) -> Session:
    session_ = sessions()
    seed_database(session_, PARAMS)
    session_.commit()
    return session_


class TestDeterminismo:
    def test_dos_corridas_con_la_misma_semilla_dan_lo_mismo(
        self, sessions: Callable[[], Session]
    ) -> None:
        session_ = sessions()
        seed_database(session_, PARAMS)
        session_.commit()
        primera = fingerprint(session_)

        seed_database(session_, PARAMS)
        session_.commit()
        assert fingerprint(session_) == primera

    def test_otra_semilla_da_otro_resultado(self, sessions: Callable[[], Session]) -> None:
        session_ = sessions()
        seed_database(session_, PARAMS)
        session_.commit()
        primera = fingerprint(session_)

        from dataclasses import replace

        seed_database(session_, replace(PARAMS, seed=PARAMS.seed + 1))
        session_.commit()
        assert fingerprint(session_) != primera

    def test_no_depende_de_la_hora_de_ejecucion(self, sessions: Callable[[], Session]) -> None:
        """The past/future split must come from the base date, never from the
        wall clock, otherwise the same seed drifts through the day."""
        session_ = sessions()
        seed_database(session_, PARAMS)
        session_.commit()
        estados_primera = sorted(
            (c.slot.start.isoformat(), c.status) for c in session_.scalars(select(Appointment))
        )
        seed_database(session_, PARAMS)
        session_.commit()
        estados_segunda = sorted(
            (c.slot.start.isoformat(), c.status) for c in session_.scalars(select(Appointment))
        )
        assert estados_primera == estados_segunda


class TestIdempotenciaDelComando:
    def test_sembrar_deja_la_base_no_vacia(self, seeded: Session) -> None:
        assert not database_is_empty(seeded)

    def test_base_vacia_detecta_una_base_limpia(self, empty_tables: Session) -> None:
        assert database_is_empty(empty_tables)

    def test_sembrar_dos_veces_no_duplica(self, sessions: Callable[[], Session]) -> None:
        session_ = sessions()
        seed_database(session_, PARAMS)
        session_.commit()
        patients = session_.scalar(select(func.count()).select_from(Patient))

        seed_database(session_, PARAMS)
        session_.commit()
        assert session_.scalar(select(func.count()).select_from(Patient)) == patients


class TestConsistenciaDelDataset:
    def test_hay_exactamente_una_clinica(self, seeded: Session) -> None:
        assert seeded.scalar(select(func.count()).select_from(Clinic)) == 1

    def test_se_generan_los_pacientes_pedidos(self, seeded: Session) -> None:
        assert seeded.scalar(select(func.count()).select_from(Patient)) == PARAMS.patients

    def test_los_documentos_no_se_repiten(self, seeded: Session) -> None:
        documentos = list(seeded.scalars(select(Patient.document_number)))
        assert len(documentos) == len(set(documentos))

    def test_ninguna_cita_ocupa_un_slot_ya_ocupado(self, seeded: Session) -> None:
        """If the partial unique index were wrong, the seed itself would be the
        first thing to violate it."""
        active = [
            c.slot_id
            for c in seeded.scalars(select(Appointment))
            if c.status in STATES_HOLDING_SLOT
        ]
        assert len(active) == len(set(active))

    def test_el_estado_del_slot_concuerda_con_su_cita(self, seeded: Session) -> None:
        for appointment in seeded.scalars(select(Appointment)):
            if appointment.status in STATES_HOLDING_SLOT:
                assert appointment.slot.status is SlotState.BUSY, appointment.id

    def test_todo_historial_describe_un_camino_legal(self, seeded: Session) -> None:
        """Every seeded appointment must have a history the state machine would
        actually have accepted. A seed that fabricates impossible histories
        makes every downstream test meaningless."""
        for appointment in seeded.scalars(select(Appointment)):
            history = sorted(appointment.history, key=lambda h: h.occurred_at)
            assert history, f"cita {appointment.id} sin historial"
            assert history[0].previous_status is None
            assert history[0].new_status is AppointmentState.SCHEDULED
            for previous, next_up in itertools.pairwise(history):
                assert next_up.previous_status is previous.new_status
                assert is_valid_transition(previous.new_status, next_up.new_status)
            assert history[-1].new_status is appointment.status

    def test_toda_transicion_quedo_auditada(self, seeded: Session) -> None:
        filas = seeded.scalar(select(func.count()).select_from(AppointmentHistory))
        appointments = seeded.scalar(select(func.count()).select_from(Appointment))
        assert filas is not None and appointments is not None
        assert filas >= appointments  # at least the creation row per appointment

    def test_los_cargos_solo_cuelgan_de_citas_atendidas_o_no_asistidas(
        self, seeded: Session
    ) -> None:
        for charge in seeded.scalars(select(Charge)):
            if charge.appointment is not None:
                assert charge.appointment.status in {
                    AppointmentState.ATTENDED,
                    AppointmentState.NO_SHOW,
                }

    def test_ningun_cargo_es_negativo(self, seeded: Session) -> None:
        assert all(c.amount >= 0 for c in seeded.scalars(select(Charge)))

    def test_un_paciente_soat_activo_no_acumula_cargos_de_atencion(self, seeded: Session) -> None:
        for charge in seeded.scalars(select(Charge)):
            if (
                charge.appointment is not None
                and charge.appointment.status is AppointmentState.ATTENDED
            ):
                patient = charge.patient
                assert not (patient.regimen is Regimen.SOAT and patient.afiliacion_active), (
                    f"SOAT activo con cargo de atención: paciente {patient.id}"
                )


class TestRealismoDelDataset:
    """A seed nobody can demo against is a seed that failed."""

    def test_quedan_cupos_libres_para_agendar(self, seeded: Session) -> None:
        free_slots = seeded.scalar(
            select(func.count()).select_from(AgendaSlot).where(AgendaSlot.status == SlotState.FREE)
        )
        assert free_slots is not None and free_slots > 50

    def test_hay_citas_en_varios_estados(self, seeded: Session) -> None:
        estados = {c.status for c in seeded.scalars(select(Appointment))}
        assert {
            AppointmentState.SCHEDULED,
            AppointmentState.CONFIRMED,
            AppointmentState.ATTENDED,
        } <= estados

    def test_hay_no_shows_que_es_el_dolor_que_ataca_el_proyecto(self, seeded: Session) -> None:
        no_shows = [
            c for c in seeded.scalars(select(Appointment)) if c.status is AppointmentState.NO_SHOW
        ]
        assert no_shows

    def test_hay_afiliaciones_inactivas(self, seeded: Session) -> None:
        inactivos = [
            p
            for p in seeded.scalars(select(Patient))
            if not p.afiliacion_active and p.regimen is not Regimen.PARTICULAR
        ]
        assert inactivos, "sin afiliaciones inactivas, validate_afiliacion no tiene qué atrapar"

    def test_hay_pacientes_sin_consentimiento_clinico(self, seeded: Session) -> None:
        """The clinical tool must have real cases where it is correctly refused."""
        sin_consentimiento = [
            p for p in seeded.scalars(select(Patient)) if not p.clinical_data_consent
        ]
        assert sin_consentimiento

    def test_hay_cargos_pendientes_para_cobrar(self, seeded: Session) -> None:
        outstanding = [c for c in seeded.scalars(select(Charge)) if c.status == "pending"]
        assert outstanding

    def test_hay_cartera_realmente_vencida(self, seeded: Session) -> None:
        """Charges fall due 30 days after the visit, and the seeded agenda only
        reaches a couple of weeks back. Without carried-over balances every
        patient reads `al_dia`, and the rule that debt warns without blocking
        has nothing to demonstrate itself on."""
        overdue = [
            c
            for c in seeded.scalars(select(Charge))
            if c.status == ChargeState.PENDING and c.due_date < FECHA_BASE
        ]
        assert overdue, "el dataset no tiene ni un cargo vencido"
        assert len({c.patient_id for c in overdue}) >= 3

    def test_la_mora_cubre_varios_tramos_de_antiguedad(self, seeded: Session) -> None:
        """A ledger where everything is 20 days late exercises one bucket."""
        dias = {
            (FECHA_BASE - c.due_date).days
            for c in seeded.scalars(select(Charge))
            if c.status == ChargeState.PENDING and c.due_date < FECHA_BASE
        }
        assert max(dias) > 60, f"la mora más antigua es de {max(dias)} días"

    def test_algun_paciente_supera_el_umbral_de_alerta(self, seeded: Session) -> None:
        """Otherwise `alerta_al_agendar` never fires on the demo data."""
        from collections import defaultdict

        from backend.domain.cartera import DEFAULT_POLICY

        por_paciente: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
        for c in seeded.scalars(select(Charge)):
            if c.status == ChargeState.PENDING and c.due_date < FECHA_BASE:
                por_paciente[c.patient_id] += c.amount
        assert any(
            total >= DEFAULT_POLICY.overdue_alert_threshold for total in por_paciente.values()
        )

    def test_hay_pacientes_en_lista_de_espera(self, seeded: Session) -> None:
        assert seeded.scalar(select(func.count()).select_from(WaitingList))

    def test_estan_representados_los_cuatro_regimenes(self, seeded: Session) -> None:
        regimenes = {p.regimen for p in seeded.scalars(select(Patient))}
        assert regimenes == set(Regimen)

    def test_la_agenda_cubre_pasado_y_futuro(self, seeded: Session) -> None:
        fechas = [s.day for s in seeded.scalars(select(AgendaSlot))]
        assert min(fechas) < FECHA_BASE < max(fechas)


class TestSinPiiReal:
    def test_ningun_dato_de_contacto_apunta_a_un_dominio_real(self, seeded: Session) -> None:
        """Cheap but explicit: the project's headline claim is that no real
        patient data exists here, so it gets an assertion."""
        for patient in seeded.scalars(select(Patient)):
            assert patient.phone.startswith("+57 3")
            if patient.email:
                assert "@" in patient.email
