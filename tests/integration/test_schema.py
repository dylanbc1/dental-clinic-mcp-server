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
    "clinica",
    "profesional",
    "paciente",
    "agenda_slot",
    "cita",
    "cita_historial",
    "cargo",
    "lista_espera",
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
    def _paciente(self, documento: str = "1020304050") -> Patient:
        return Patient(
            tipo_documento=DocumentType.CC,
            documento=documento,
            nombre="Ana Gómez",
            telefono="+57 3001234567",
            regimen=Regimen.CONTRIBUTIVO,
            afiliacion_activa=True,
        )

    def test_no_se_repite_el_documento_para_el_mismo_tipo(self, empty_tables: Session) -> None:
        empty_tables.add(self._paciente())
        empty_tables.flush()
        empty_tables.add(self._paciente())
        with pytest.raises(IntegrityError):
            empty_tables.flush()

    def test_el_mismo_numero_con_otro_tipo_si_se_permite(self, empty_tables: Session) -> None:
        """A minor's TI and an adult's CC can legitimately share digits."""
        cc = self._paciente()
        ti = self._paciente()
        ti.tipo_documento = DocumentType.TI
        empty_tables.add_all([cc, ti])
        empty_tables.flush()  # must not raise

    def test_no_se_repite_el_registro_profesional(self, empty_tables: Session) -> None:
        clinica = Clinic(nombre="C", nit="900.1-1", especialidad="Odontología")
        empty_tables.add(clinica)
        empty_tables.flush()
        for _ in range(2):
            empty_tables.add(
                Professional(
                    clinica_id=clinica.id,
                    nombre="Dr. X",
                    registro="RM-DUP",
                    especialidad=Specialty.ORTHODONTICS,
                )
            )
        with pytest.raises(IntegrityError):
            empty_tables.flush()


class TestChecks:
    def test_un_slot_no_puede_terminar_antes_de_empezar(self, empty_tables: Session) -> None:
        clinica = Clinic(nombre="C", nit="900.2-2", especialidad="O")
        empty_tables.add(clinica)
        empty_tables.flush()
        profesional = Professional(
            clinica_id=clinica.id,
            nombre="Dr. Y",
            registro="RM-CHK",
            especialidad=Specialty.ENDODONTICS,
        )
        empty_tables.add(profesional)
        empty_tables.flush()

        inicio = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
        empty_tables.add(
            AgendaSlot(
                profesional_id=profesional.id,
                fecha=date(2026, 9, 1),
                inicio=inicio,
                fin=inicio - timedelta(minutes=30),
            )
        )
        with pytest.raises(IntegrityError):
            empty_tables.flush()

    def test_un_cargo_no_puede_ser_negativo(self, empty_tables: Session) -> None:
        paciente = Patient(
            tipo_documento=DocumentType.CC,
            documento="777",
            nombre="N",
            telefono="+57 3000000000",
            regimen=Regimen.PARTICULAR,
            afiliacion_activa=True,
        )
        empty_tables.add(paciente)
        empty_tables.flush()
        empty_tables.add(
            Charge(
                paciente_id=paciente.id,
                concepto="particular",
                monto=Decimal("-1"),
                estado=ChargeState.PENDING,
                vencimiento=date(2026, 9, 30),
            )
        )
        with pytest.raises(IntegrityError):
            empty_tables.flush()

    def test_el_nivel_de_cuota_moderadora_esta_acotado(self, empty_tables: Session) -> None:
        empty_tables.add(
            Patient(
                tipo_documento=DocumentType.CC,
                documento="888",
                nombre="N",
                telefono="+57 3000000000",
                regimen=Regimen.CONTRIBUTIVO,
                afiliacion_activa=True,
                nivel_cuota_moderadora=7,
            )
        )
        with pytest.raises(IntegrityError):
            empty_tables.flush()

    def test_un_estado_inexistente_es_rechazado_por_el_enum(self, empty_tables: Session) -> None:
        with pytest.raises((DataError, IntegrityError)):
            empty_tables.execute(
                text(
                    "insert into cita (paciente_id, profesional_id, slot_id, estado, "
                    "creada_por, creada_en, actualizada_en) "
                    "values (1, 1, 1, 'inventado', 'x', now(), now())"
                )
            )


class TestListaEsperaUnicidadParcial:
    def _paciente(self, session: Session, documento: str) -> Patient:
        paciente = Patient(
            tipo_documento=DocumentType.CC,
            documento=documento,
            nombre="N",
            telefono="+57 3000000000",
            regimen=Regimen.SUBSIDIADO,
            afiliacion_activa=True,
        )
        session.add(paciente)
        session.flush()
        return paciente

    def test_un_paciente_no_se_inscribe_dos_veces_en_la_misma_especialidad(
        self, empty_tables: Session
    ) -> None:
        paciente = self._paciente(empty_tables, "555")
        for _ in range(2):
            empty_tables.add(
                WaitingList(
                    paciente_id=paciente.id,
                    especialidad=Specialty.ORTHODONTICS,
                    prioridad=WaitingListPriority.SENIORITY,
                    estado=WaitingListState.ACTIVE,
                )
            )
        with pytest.raises(IntegrityError):
            empty_tables.flush()

    def test_puede_reinscribirse_si_la_anterior_ya_no_esta_activa(
        self, empty_tables: Session
    ) -> None:
        """The uniqueness is partial on purpose: a retired entry must not block
        the patient from joining the queue again later."""
        paciente = self._paciente(empty_tables, "556")
        empty_tables.add(
            WaitingList(
                paciente_id=paciente.id,
                especialidad=Specialty.ORTHODONTICS,
                estado=WaitingListState.WITHDRAWN,
            )
        )
        empty_tables.flush()
        empty_tables.add(
            WaitingList(
                paciente_id=paciente.id,
                especialidad=Specialty.ORTHODONTICS,
                estado=WaitingListState.ACTIVE,
            )
        )
        empty_tables.flush()  # must not raise

    def test_la_misma_persona_puede_esperar_en_dos_especialidades(
        self, empty_tables: Session
    ) -> None:
        paciente = self._paciente(empty_tables, "557")
        empty_tables.add_all(
            [
                WaitingList(paciente_id=paciente.id, especialidad=Specialty.ORTHODONTICS),
                WaitingList(paciente_id=paciente.id, especialidad=Specialty.ENDODONTICS),
            ]
        )
        empty_tables.flush()


class TestAuditoria:
    def test_el_historial_admite_estado_anterior_nulo(self, empty_tables: Session) -> None:
        """The very first row of an appointment's history has no predecessor."""
        registro = AppointmentHistory(
            cita_id=1,
            estado_anterior=None,
            estado_nuevo=AppointmentState.SCHEDULED,
            usuario="tester",
        )
        assert registro.estado_anterior is None

    def test_la_tabla_de_historial_no_tiene_columna_de_actualizacion(self) -> None:
        # Append-only by construction: there is nothing to update.
        assert "actualizada_en" not in AppointmentHistory.__table__.columns

    def test_toda_columna_de_historial_es_no_nula_donde_importa(self) -> None:
        columnas = AppointmentHistory.__table__.columns
        assert not columnas["estado_nuevo"].nullable
        assert not columnas["usuario"].nullable
        assert not columnas["momento"].nullable
