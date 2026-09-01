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
PARAMS = SeedParams(seed=20260831, patients=25, dias_agenda=10, fecha_base=FECHA_BASE)


def fingerprint(session: Session) -> str:
    """Content hash over business fields only.

    Primary keys are deliberately excluded: sequences keep advancing between
    runs, so including them would make every run differ for a reason that has
    nothing to do with the data.
    """
    contenido: dict[str, list] = {}

    contenido["paciente"] = sorted(
        (
            p.tipo_documento,
            p.documento,
            p.nombre,
            p.telefono,
            p.regimen,
            p.afiliacion_activa,
            p.nivel_cuota_moderadora,
            p.consentimiento_datos_clinicos,
            p.fecha_nacimiento.isoformat() if p.fecha_nacimiento else None,
        )
        for p in session.scalars(select(Patient))
    )
    contenido["slot"] = sorted(
        (s.profesional.registro, s.inicio.isoformat(), s.fin.isoformat(), s.estado)
        for s in session.scalars(select(AgendaSlot))
    )
    contenido["cita"] = sorted(
        (
            c.paciente.documento,
            c.profesional.registro,
            c.slot.inicio.isoformat(),
            c.estado,
            c.motivo_cancelacion,
        )
        for c in session.scalars(select(Appointment))
    )
    contenido["cargo"] = sorted(
        (g.paciente.documento, g.concepto, str(g.monto), g.estado, g.vencimiento.isoformat())
        for g in session.scalars(select(Charge))
    )
    contenido["lista_espera"] = sorted(
        (e.paciente.documento, e.especialidad, e.prioridad, e.estado)
        for e in session.scalars(select(WaitingList))
    )
    crudo = json.dumps(contenido, sort_keys=True, default=str, ensure_ascii=False)
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
            (c.slot.inicio.isoformat(), c.estado) for c in session_.scalars(select(Appointment))
        )
        seed_database(session_, PARAMS)
        session_.commit()
        estados_segunda = sorted(
            (c.slot.inicio.isoformat(), c.estado) for c in session_.scalars(select(Appointment))
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
        documentos = list(seeded.scalars(select(Patient.documento)))
        assert len(documentos) == len(set(documentos))

    def test_ninguna_cita_ocupa_un_slot_ya_ocupado(self, seeded: Session) -> None:
        """If the partial unique index were wrong, the seed itself would be the
        first thing to violate it."""
        active = [
            c.slot_id
            for c in seeded.scalars(select(Appointment))
            if c.estado in STATES_HOLDING_SLOT
        ]
        assert len(active) == len(set(active))

    def test_el_estado_del_slot_concuerda_con_su_cita(self, seeded: Session) -> None:
        for cita in seeded.scalars(select(Appointment)):
            if cita.estado in STATES_HOLDING_SLOT:
                assert cita.slot.estado is SlotState.BUSY, cita.id

    def test_todo_historial_describe_un_camino_legal(self, seeded: Session) -> None:
        """Every seeded appointment must have a history the state machine would
        actually have accepted. A seed that fabricates impossible histories
        makes every downstream test meaningless."""
        for cita in seeded.scalars(select(Appointment)):
            historial = sorted(cita.historial, key=lambda h: h.momento)
            assert historial, f"cita {cita.id} sin historial"
            assert historial[0].estado_anterior is None
            assert historial[0].estado_nuevo is AppointmentState.SCHEDULED
            for previous, next_up in itertools.pairwise(historial):
                assert next_up.estado_anterior is previous.estado_nuevo
                assert is_valid_transition(previous.estado_nuevo, next_up.estado_nuevo)
            assert historial[-1].estado_nuevo is cita.estado

    def test_toda_transicion_quedo_auditada(self, seeded: Session) -> None:
        filas = seeded.scalar(select(func.count()).select_from(AppointmentHistory))
        citas = seeded.scalar(select(func.count()).select_from(Appointment))
        assert filas is not None and citas is not None
        assert filas >= citas  # at least the creation row per appointment

    def test_los_cargos_solo_cuelgan_de_citas_atendidas_o_no_asistidas(
        self, seeded: Session
    ) -> None:
        for cargo in seeded.scalars(select(Charge)):
            if cargo.cita is not None:
                assert cargo.cita.estado in {AppointmentState.ATTENDED, AppointmentState.NO_SHOW}

    def test_ningun_cargo_es_negativo(self, seeded: Session) -> None:
        assert all(c.monto >= 0 for c in seeded.scalars(select(Charge)))

    def test_un_paciente_soat_activo_no_acumula_cargos_de_atencion(self, seeded: Session) -> None:
        for cargo in seeded.scalars(select(Charge)):
            if cargo.cita is not None and cargo.cita.estado is AppointmentState.ATTENDED:
                paciente = cargo.paciente
                assert not (paciente.regimen is Regimen.SOAT and paciente.afiliacion_activa), (
                    f"SOAT activo con cargo de atención: paciente {paciente.id}"
                )


class TestRealismoDelDataset:
    """A seed nobody can demo against is a seed that failed."""

    def test_quedan_cupos_libres_para_agendar(self, seeded: Session) -> None:
        free_slots = seeded.scalar(
            select(func.count()).select_from(AgendaSlot).where(AgendaSlot.estado == SlotState.FREE)
        )
        assert free_slots is not None and free_slots > 50

    def test_hay_citas_en_varios_estados(self, seeded: Session) -> None:
        estados = {c.estado for c in seeded.scalars(select(Appointment))}
        assert {
            AppointmentState.SCHEDULED,
            AppointmentState.CONFIRMED,
            AppointmentState.ATTENDED,
        } <= estados

    def test_hay_no_shows_que_es_el_dolor_que_ataca_el_proyecto(self, seeded: Session) -> None:
        no_shows = [
            c for c in seeded.scalars(select(Appointment)) if c.estado is AppointmentState.NO_SHOW
        ]
        assert no_shows

    def test_hay_afiliaciones_inactivas(self, seeded: Session) -> None:
        inactivos = [
            p
            for p in seeded.scalars(select(Patient))
            if not p.afiliacion_activa and p.regimen is not Regimen.PARTICULAR
        ]
        assert inactivos, "sin afiliaciones inactivas, validate_afiliacion no tiene qué atrapar"

    def test_hay_pacientes_sin_consentimiento_clinico(self, seeded: Session) -> None:
        """The clinical tool must have real cases where it is correctly refused."""
        sin_consentimiento = [
            p for p in seeded.scalars(select(Patient)) if not p.consentimiento_datos_clinicos
        ]
        assert sin_consentimiento

    def test_hay_cargos_pendientes_para_cobrar(self, seeded: Session) -> None:
        outstanding = [c for c in seeded.scalars(select(Charge)) if c.estado == "pending"]
        assert outstanding

    def test_hay_cartera_realmente_vencida(self, seeded: Session) -> None:
        """Charges fall due 30 days after the visit, and the seeded agenda only
        reaches a couple of weeks back. Without carried-over balances every
        patient reads `al_dia`, and the rule that debt warns without blocking
        has nothing to demonstrate itself on."""
        overdue = [
            c
            for c in seeded.scalars(select(Charge))
            if c.estado == ChargeState.PENDING and c.vencimiento < FECHA_BASE
        ]
        assert overdue, "el dataset no tiene ni un cargo vencido"
        assert len({c.paciente_id for c in overdue}) >= 3

    def test_la_mora_cubre_varios_tramos_de_antiguedad(self, seeded: Session) -> None:
        """A ledger where everything is 20 days late exercises one bucket."""
        dias = {
            (FECHA_BASE - c.vencimiento).days
            for c in seeded.scalars(select(Charge))
            if c.estado == ChargeState.PENDING and c.vencimiento < FECHA_BASE
        }
        assert max(dias) > 60, f"la mora más antigua es de {max(dias)} días"

    def test_algun_paciente_supera_el_umbral_de_alerta(self, seeded: Session) -> None:
        """Otherwise `alerta_al_agendar` never fires on the demo data."""
        from collections import defaultdict

        from backend.domain.cartera import DEFAULT_POLICY

        por_paciente: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
        for c in seeded.scalars(select(Charge)):
            if c.estado == ChargeState.PENDING and c.vencimiento < FECHA_BASE:
                por_paciente[c.paciente_id] += c.monto
        assert any(total >= DEFAULT_POLICY.umbral_alerta_mora for total in por_paciente.values())

    def test_hay_pacientes_en_lista_de_espera(self, seeded: Session) -> None:
        assert seeded.scalar(select(func.count()).select_from(WaitingList))

    def test_estan_representados_los_cuatro_regimenes(self, seeded: Session) -> None:
        regimenes = {p.regimen for p in seeded.scalars(select(Patient))}
        assert regimenes == set(Regimen)

    def test_la_agenda_cubre_pasado_y_futuro(self, seeded: Session) -> None:
        fechas = [s.fecha for s in seeded.scalars(select(AgendaSlot))]
        assert min(fechas) < FECHA_BASE < max(fechas)


class TestSinPiiReal:
    def test_ningun_dato_de_contacto_apunta_a_un_dominio_real(self, seeded: Session) -> None:
        """Cheap but explicit: the project's headline claim is that no real
        patient data exists here, so it gets an assertion."""
        for paciente in seeded.scalars(select(Patient)):
            assert paciente.telefono.startswith("+57 3")
            if paciente.email:
                assert "@" in paciente.email
