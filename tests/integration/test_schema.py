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

EXPECTED_TABLES = {
    "clinic",
    "professional",
    "patient",
    "agenda_slot",
    "appointment",
    "appointment_history",
    "charge",
    "waiting_list",
}


class TestSchemaShape:
    def test_the_models_eight_tables_exist(self, engine: object) -> None:
        names = set(inspect(engine).get_table_names())  # type: ignore[arg-type]
        assert names >= EXPECTED_TABLES

    def test_the_metadata_declares_exactly_those_tables(self) -> None:
        assert set(Base.metadata.tables) == EXPECTED_TABLES

    def test_the_timestamps_carry_a_timezone(self, engine: object) -> None:
        """A `timestamp without time zone` column is a five-hour bug waiting.

        Checked on the reflected type's own flag rather than on its rendered
        SQL: ``str()`` uses the generic compiler, which drops the qualifier.
        """
        inspector = inspect(engine)  # type: ignore[arg-type]
        checked = 0
        for table in EXPECTED_TABLES:
            for column in inspector.get_columns(table):
                kind = column["type"]
                if isinstance(kind, DateTime):
                    assert kind.timezone is True, f"{table}.{column['name']} has no timezone"
                    checked += 1
        assert checked >= 20, "no time column was inspected at all"

    def test_the_enums_are_native_postgres_types(self, session: Session) -> None:
        types = (
            session.execute(text("select typname from pg_type where typtype = 'e'")).scalars().all()
        )
        assert "appointment_state_enum" in types
        assert "regimen_enum" in types


class TestUniqueness:
    def _patient(self, document_number: str = "1020304050") -> Patient:
        return Patient(
            document_type=DocumentType.CC,
            document_number=document_number,
            name="Ana Gómez",
            phone="+57 3001234567",
            regimen=Regimen.CONTRIBUTIVO,
            affiliation_active=True,
        )

    def test_the_document_is_not_repeated_for_the_same_type(self, empty_tables: Session) -> None:
        empty_tables.add(self._patient())
        empty_tables.flush()
        empty_tables.add(self._patient())
        with pytest.raises(IntegrityError):
            empty_tables.flush()

    def test_the_same_number_with_another_type_is_allowed(self, empty_tables: Session) -> None:
        """A minor's TI and an adult's CC can legitimately share digits."""
        cc = self._patient()
        ti = self._patient()
        ti.document_type = DocumentType.TI
        empty_tables.add_all([cc, ti])
        empty_tables.flush()  # must not raise

    def test_the_professional_license_is_not_repeated(self, empty_tables: Session) -> None:
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
    def test_a_slot_cannot_end_before_it_starts(self, empty_tables: Session) -> None:
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

    def test_a_charge_cannot_be_negative(self, empty_tables: Session) -> None:
        patient = Patient(
            document_type=DocumentType.CC,
            document_number="777",
            name="N",
            phone="+57 3000000000",
            regimen=Regimen.PARTICULAR,
            affiliation_active=True,
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

    def test_the_cuota_moderadora_level_is_bounded(self, empty_tables: Session) -> None:
        empty_tables.add(
            Patient(
                document_type=DocumentType.CC,
                document_number="888",
                name="N",
                phone="+57 3000000000",
                regimen=Regimen.CONTRIBUTIVO,
                affiliation_active=True,
                cuota_moderadora_level=7,
            )
        )
        with pytest.raises(IntegrityError):
            empty_tables.flush()

    def test_a_nonexistent_state_is_refused_by_the_enum(self, empty_tables: Session) -> None:
        with pytest.raises((DataError, IntegrityError)):
            empty_tables.execute(
                text(
                    "insert into appointment (patient_id, professional_id, slot_id, status, "
                    "created_by, created_at, updated_at) "
                    "values (1, 1, 1, 'inventado', 'x', now(), now())"
                )
            )


class TestWaitingListPartialUniqueness:
    def _patient(self, session: Session, document_number: str) -> Patient:
        patient = Patient(
            document_type=DocumentType.CC,
            document_number=document_number,
            name="N",
            phone="+57 3000000000",
            regimen=Regimen.SUBSIDIADO,
            affiliation_active=True,
        )
        session.add(patient)
        session.flush()
        return patient

    def test_a_patient_does_not_enrol_twice_in_the_same_specialty(
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

    def test_re_enrolment_is_allowed_once_the_previous_one_is_inactive(
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

    def test_the_same_person_can_wait_in_two_specialties(self, empty_tables: Session) -> None:
        patient = self._patient(empty_tables, "557")
        empty_tables.add_all(
            [
                WaitingList(patient_id=patient.id, specialty=Specialty.ORTHODONTICS),
                WaitingList(patient_id=patient.id, specialty=Specialty.ENDODONTICS),
            ]
        )
        empty_tables.flush()


class TestAudit:
    def test_the_history_allows_a_null_previous_status(self, empty_tables: Session) -> None:
        """The very first row of an appointment's history has no predecessor."""
        license_number = AppointmentHistory(
            appointment_id=1,
            previous_status=None,
            new_status=AppointmentState.SCHEDULED,
            user="tester",
        )
        assert license_number.previous_status is None

    def test_the_history_table_has_no_updated_column(self) -> None:
        # Append-only by construction: there is nothing to update.
        assert "updated_at" not in AppointmentHistory.__table__.columns

    def test_every_history_column_is_not_null_where_it_matters(self) -> None:
        columns = AppointmentHistory.__table__.columns
        assert not columns["new_status"].nullable
        # `user` is reserved in PostgreSQL, so the column is `changed_by`.
        assert not columns["changed_by"].nullable
        assert not columns["occurred_at"].nullable
