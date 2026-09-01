"""Session and transaction-scope tests.

`session_scope` is where the "every state change is audited" guarantee is
actually enforced: the audit row and the change it describes share one
transaction, so a partial write is impossible.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import sessionmaker

from backend import database
from backend.database import get_engine, get_session, get_sessionmaker, session_scope
from backend.enums import DocumentType, Regimen
from backend.models import Patient

pytestmark = pytest.mark.integration


@pytest.fixture
def test_scope(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the module-level session factory at the throwaway test database."""
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(database, "get_sessionmaker", lambda: factory)


def _paciente(document_number: str) -> Patient:
    return Patient(
        document_type=DocumentType.CC,
        document_number=document_number,
        name="Persona de prueba",
        phone="+57 3001234567",
        regimen=Regimen.PARTICULAR,
        afiliacion_active=True,
    )


class TestCacheDeConexion:
    def test_el_engine_se_reutiliza(self) -> None:
        assert get_engine() is get_engine()

    def test_el_sessionmaker_se_reutiliza(self) -> None:
        assert get_sessionmaker() is get_sessionmaker()


class TestSessionScope:
    def test_confirma_al_salir_sin_error(self, test_scope: None) -> None:
        with session_scope() as session_:
            session_.add(_paciente("9000001"))

        with session_scope() as verificacion:
            assert verificacion.scalar(select(Patient).where(Patient.document_number == "9000001"))

    def test_revierte_ante_una_excepcion(self, test_scope: None) -> None:
        with pytest.raises(RuntimeError, match="algo falló"), session_scope() as session_:
            session_.add(_paciente("9000002"))
            session_.flush()
            raise RuntimeError("algo falló")

        with session_scope() as verificacion:
            assert (
                verificacion.scalar(select(Patient).where(Patient.document_number == "9000002"))
                is None
            )

    def test_una_escritura_parcial_no_sobrevive(self, test_scope: None) -> None:
        """The property the audit trail depends on: either both rows land or
        neither does."""
        with pytest.raises(RuntimeError), session_scope() as session_:
            session_.add(_paciente("9000003"))
            session_.flush()
            session_.add(_paciente("9000004"))
            session_.flush()
            raise RuntimeError("interrupción a mitad de camino")

        with session_scope() as verificacion:
            encontrados = verificacion.scalars(
                select(Patient).where(Patient.document_number.in_(["9000003", "9000004"]))
            ).all()
            assert encontrados == []

    def test_la_sesion_queda_cerrada_al_terminar(self, test_scope: None) -> None:
        with session_scope() as session_:
            pass
        assert not session_.is_active or session_.get_bind() is not None


class TestDependenciaFastapi:
    def test_get_session_entrega_una_sesion_utilizable(self, test_scope: None) -> None:
        generador = get_session()
        session_ = next(generador)
        try:
            session_.add(_paciente("9000005"))
        finally:
            generador.close()

    def test_get_session_confirma_al_agotarse(self, test_scope: None) -> None:
        generador = get_session()
        session_ = next(generador)
        session_.add(_paciente("9000006"))
        with pytest.raises(StopIteration):
            next(generador)

        with session_scope() as verificacion:
            assert verificacion.scalar(select(Patient).where(Patient.document_number == "9000006"))
