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


def _patient(document_number: str) -> Patient:
    return Patient(
        document_type=DocumentType.CC,
        document_number=document_number,
        name="Persona de prueba",
        phone="+57 3001234567",
        regimen=Regimen.PARTICULAR,
        affiliation_active=True,
    )


class TestConnectionCache:
    def test_the_engine_is_reused(self) -> None:
        assert get_engine() is get_engine()

    def test_the_sessionmaker_is_reused(self) -> None:
        assert get_sessionmaker() is get_sessionmaker()


class TestSessionScope:
    def test_commits_on_exit_without_error(self, test_scope: None) -> None:
        with session_scope() as session_:
            session_.add(_patient("9000001"))

        with session_scope() as check:
            assert check.scalar(select(Patient).where(Patient.document_number == "9000001"))

    def test_it_rolls_back_on_an_exception(self, test_scope: None) -> None:
        with pytest.raises(RuntimeError, match="algo falló"), session_scope() as session_:
            session_.add(_patient("9000002"))
            session_.flush()
            raise RuntimeError("algo falló")

        with session_scope() as check:
            assert check.scalar(select(Patient).where(Patient.document_number == "9000002")) is None

    def test_a_partial_write_does_not_survive(self, test_scope: None) -> None:
        """The property the audit trail depends on: either both rows land or
        neither does."""
        with pytest.raises(RuntimeError), session_scope() as session_:
            session_.add(_patient("9000003"))
            session_.flush()
            session_.add(_patient("9000004"))
            session_.flush()
            raise RuntimeError("interrupción a mitad de camino")

        with session_scope() as check:
            found = check.scalars(
                select(Patient).where(Patient.document_number.in_(["9000003", "9000004"]))
            ).all()
            assert found == []

    def test_the_session_is_closed_at_the_end(self, test_scope: None) -> None:
        with session_scope() as session_:
            pass
        assert not session_.is_active or session_.get_bind() is not None


class TestFastapiDependency:
    def test_get_session_hands_over_a_usable_session(self, test_scope: None) -> None:
        generator = get_session()
        session_ = next(generator)
        try:
            session_.add(_patient("9000005"))
        finally:
            generator.close()

    def test_get_session_commits_when_exhausted(self, test_scope: None) -> None:
        generator = get_session()
        session_ = next(generator)
        session_.add(_patient("9000006"))
        with pytest.raises(StopIteration):
            next(generator)

        with session_scope() as check:
            assert check.scalar(select(Patient).where(Patient.document_number == "9000006"))
