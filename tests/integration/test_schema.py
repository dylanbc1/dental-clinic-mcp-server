"""Schema-level guarantees.

These assertions belong in the database, not in application code: a check that
only exists in Python is a check a second process can walk straight past.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import DateTime, inspect, text
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session

from backend.domain.time import UTC
from backend.enums import (
    AppointmentState,
    ChargeState,
    DocumentType,
    Regimen,
    Specialty,
    WaitingListPriority,
    WaitingListState,
)
from backend.models import (
    AgendaSlot,
    AppointmentHistory,
    Base,
    Charge,
    Clinic,
    Patient,
    Professional,
    WaitingList,
)

pytestmark = pytest.mark.integration

TABLAS_ESPERADAS = {
    "clinic",
    "professional",
    "patient",
    "agenda_slot",
    "appointment",
    "appointment_history",
    "charge",
    "waiting_list",
}


class TestFormaDelEsquema:
    def test_existen_las_ocho_tablas_del_modelo(self, engine: object) -> None:
        names = set(inspect(engine).get_table_names())  # type: ignore[arg-type]
        assert names >= TABLAS_ESPERADAS

    def test_los_metadatos_declaran_exactamente_esas_tablas(self) -> None:
        assert set(Base.metadata.tables) == TABLAS_ESPERADAS

    def test_los_timestamps_llevan_zona_horaria(self, engine: object) -> None:
        """A `timestamp without time zone` column is a five-hour bug waiting.

        Checked on the reflected type's own flag rather than on its rendered
        SQL: ``str()`` uses the generic compiler, which drops the qualifier.
        """
        inspector = inspect(engine)  # type: ignore[arg-type]
        revisadas = 0
        for table in TABLAS_ESPERADAS:
            for columna in inspector.get_columns(table):
                tipo = columna["type"]
                if isinstance(tipo, DateTime):
                    assert tipo.timezone is True, f"{table}.{columna['name']} sin zona"
                    revisadas += 1
        assert revisadas >= 20, "no se inspeccionó ninguna columna de tiempo"

    def test_los_enums_son_nativos_de_postgres(self, session: Session) -> None:
        tipos = (
            session.execute(text("select typname from pg_type where typtype = 'e'")).scalars().all()
        )
        assert "appointment_state_enum" in tipos
        assert "regimen_enum" in tipos


class TestUnicidad:
    def _patient(self, document_number: str = "1020304050") -> Patient:
        return Patient(
            document_type=DocumentType.CC,
            document_number=document_number,
            name="Ana Gómez",
            phone="+57 3001234567",
            regimen=Regimen.CONTRIBUTIVO,
            afiliacion_active=True,
        )

    def test_no_se_repite_el_documento_para_el_mismo_tipo(self, empty_tables: Session) -> None:
        empty_tables.add(self._patient())
        empty_tables.flush()
        empty_tables.add(self._patient())
        with pytest.raises(IntegrityError):
            empty_tables.flush()

    def test_el_mismo_numero_con_otro_tipo_si_se_permite(self, empty_tables: Session) -> None:
        """A minor's TI and an adult's CC can legitimately share digits."""
        cc = self._patient()
        ti = self._patient()
        ti.document_type = DocumentType.TI
        empty_tables.add_all([cc, ti])
        empty_tables.flush()  # must not raise

    def test_no_se_repite_el_registro_profesional(self, empty_tables: Session) -> None:
        clinic = Clinic(name="C", nit="900.1-1", specialty="Odontología")
        empty_tables.add(clinic)
        empty_tables.flush()
        for _ in range(2):
            empty_tables.add(
                Professional(
                    clinic_id=clinic.id,
                    name="Dr. X",
                    license_number="RM-DUP",
                    specialty=Specialty.ORTHODONTICS,
                )
            )
        with pytest.raises(IntegrityError):
            empty_tables.flush()


class TestChecks:
    def test_un_slot_no_puede_terminar_antes_de_empezar(self, empty_tables: Session) -> None:
        clinic = Clinic(name="C", nit="900.2-2", specialty="O")
        empty_tables.add(clinic)
        empty_tables.flush()
        professional = Professional(
            clinic_id=clinic.id,
            name="Dr. Y",
            license_number="RM-CHK",
            specialty=Specialty.ENDODONTICS,
        )
        empty_tables.add(professional)
        empty_tables.flush()

        start = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
        empty_tables.add(
            AgendaSlot(
                professional_id=professional.id,
                day=date(2026, 9, 1),
                start=start,
                end=start - timedelta(minutes=30),
            )
        )
        with pytest.raises(IntegrityError):
            empty_tables.flush()

    def test_un_cargo_no_puede_ser_negativo(self, empty_tables: Session) -> None:
        patient = Patient(
            document_type=DocumentType.CC,
            document_number="777",
            name="N",
            phone="+57 3000000000",
            regimen=Regimen.PARTICULAR,
            afiliacion_active=True,
        )
        empty_tables.add(patient)
        empty_tables.flush()
        empty_tables.add(
            Charge(
                patient_id=patient.id,
                concept="particular",
                amount=Decimal("-1"),
                status=ChargeState.PENDING,
                due_date=date(2026, 9, 30),
            )
        )
        with pytest.raises(IntegrityError):
            empty_tables.flush()

    def test_el_nivel_de_cuota_moderadora_esta_acotado(self, empty_tables: Session) -> None:
        empty_tables.add(
            Patient(
                document_type=DocumentType.CC,
                document_number="888",
                name="N",
                phone="+57 3000000000",
                regimen=Regimen.CONTRIBUTIVO,
                afiliacion_active=True,
                cuota_moderadora_level=7,
            )
        )
        with pytest.raises(IntegrityError):
            empty_tables.flush()

    def test_un_estado_inexistente_es_rechazado_por_el_enum(self, empty_tables: Session) -> None:
        with pytest.raises((DataError, IntegrityError)):
            empty_tables.execute(
                text(
                    "insert into appointment (patient_id, professional_id, slot_id, status, "
                    "created_by, created_at, updated_at) "
                    "values (1, 1, 1, 'inventado', 'x', now(), now())"
                )
            )


class TestListaEsperaUnicidadParcial:
    def _patient(self, session: Session, document_number: str) -> Patient:
        patient = Patient(
            document_type=DocumentType.CC,
            document_number=document_number,
            name="N",
            phone="+57 3000000000",
            regimen=Regimen.SUBSIDIADO,
            afiliacion_active=True,
        )
        session.add(patient)
        session.flush()
        return patient

    def test_un_paciente_no_se_inscribe_dos_veces_en_la_misma_especialidad(
        self, empty_tables: Session
    ) -> None:
        patient = self._patient(empty_tables, "555")
        for _ in range(2):
            empty_tables.add(
                WaitingList(
                    patient_id=patient.id,
                    specialty=Specialty.ORTHODONTICS,
                    priority=WaitingListPriority.SENIORITY,
                    status=WaitingListState.ACTIVE,
                )
            )
        with pytest.raises(IntegrityError):
            empty_tables.flush()

    def test_puede_reinscribirse_si_la_anterior_ya_no_esta_activa(
        self, empty_tables: Session
    ) -> None:
        """The uniqueness is partial on purpose: a retired entry must not block
        the patient from joining the queue again later."""
        patient = self._patient(empty_tables, "556")
        empty_tables.add(
            WaitingList(
                patient_id=patient.id,
                specialty=Specialty.ORTHODONTICS,
                status=WaitingListState.WITHDRAWN,
            )
        )
        empty_tables.flush()
        empty_tables.add(
            WaitingList(
                patient_id=patient.id,
                specialty=Specialty.ORTHODONTICS,
                status=WaitingListState.ACTIVE,
            )
        )
        empty_tables.flush()  # must not raise

    def test_la_misma_persona_puede_esperar_en_dos_especialidades(
        self, empty_tables: Session
    ) -> None:
        patient = self._patient(empty_tables, "557")
        empty_tables.add_all(
            [
                WaitingList(patient_id=patient.id, specialty=Specialty.ORTHODONTICS),
                WaitingList(patient_id=patient.id, specialty=Specialty.ENDODONTICS),
            ]
        )
        empty_tables.flush()


class TestAuditoria:
    def test_el_historial_admite_estado_anterior_nulo(self, empty_tables: Session) -> None:
        """The very first row of an appointment's history has no predecessor."""
        license_number = AppointmentHistory(
            appointment_id=1,
            previous_status=None,
            new_status=AppointmentState.SCHEDULED,
            user="tester",
        )
        assert license_number.previous_status is None

    def test_la_tabla_de_historial_no_tiene_columna_de_actualizacion(self) -> None:
        # Append-only by construction: there is nothing to update.
        assert "updated_at" not in AppointmentHistory.__table__.columns

    def test_toda_columna_de_historial_es_no_nula_donde_importa(self) -> None:
        columnas = AppointmentHistory.__table__.columns
        assert not columnas["new_status"].nullable
        # `user` is reserved in PostgreSQL, so the column is `changed_by`.
        assert not columnas["changed_by"].nullable
        assert not columnas["occurred_at"].nullable
