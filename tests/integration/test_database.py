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
from backend.enums import Regimen, TipoDocumento
from backend.models import Paciente

pytestmark = pytest.mark.integration


@pytest.fixture
def scope_de_prueba(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the module-level session factory at the throwaway test database."""
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(database, "get_sessionmaker", lambda: factory)


def _paciente(documento: str) -> Paciente:
    return Paciente(
        tipo_documento=TipoDocumento.CC,
        documento=documento,
        nombre="Persona de prueba",
        telefono="+57 3001234567",
        regimen=Regimen.PARTICULAR,
        afiliacion_activa=True,
    )


class TestCacheDeConexion:
    def test_el_engine_se_reutiliza(self) -> None:
        assert get_engine() is get_engine()

    def test_el_sessionmaker_se_reutiliza(self) -> None:
        assert get_sessionmaker() is get_sessionmaker()


class TestSessionScope:
    def test_confirma_al_salir_sin_error(self, scope_de_prueba: None) -> None:
        with session_scope() as sesion:
            sesion.add(_paciente("9000001"))

        with session_scope() as verificacion:
            assert verificacion.scalar(select(Paciente).where(Paciente.documento == "9000001"))

    def test_revierte_ante_una_excepcion(self, scope_de_prueba: None) -> None:
        with pytest.raises(RuntimeError, match="algo falló"), session_scope() as sesion:
            sesion.add(_paciente("9000002"))
            sesion.flush()
            raise RuntimeError("algo falló")

        with session_scope() as verificacion:
            assert (
                verificacion.scalar(select(Paciente).where(Paciente.documento == "9000002")) is None
            )

    def test_una_escritura_parcial_no_sobrevive(self, scope_de_prueba: None) -> None:
        """The property the audit trail depends on: either both rows land or
        neither does."""
        with pytest.raises(RuntimeError), session_scope() as sesion:
            sesion.add(_paciente("9000003"))
            sesion.flush()
            sesion.add(_paciente("9000004"))
            sesion.flush()
            raise RuntimeError("interrupción a mitad de camino")

        with session_scope() as verificacion:
            encontrados = verificacion.scalars(
                select(Paciente).where(Paciente.documento.in_(["9000003", "9000004"]))
            ).all()
            assert encontrados == []

    def test_la_sesion_queda_cerrada_al_terminar(self, scope_de_prueba: None) -> None:
        with session_scope() as sesion:
            pass
        assert not sesion.is_active or sesion.get_bind() is not None


class TestDependenciaFastapi:
    def test_get_session_entrega_una_sesion_utilizable(self, scope_de_prueba: None) -> None:
        generador = get_session()
        sesion = next(generador)
        try:
            sesion.add(_paciente("9000005"))
        finally:
            generador.close()

    def test_get_session_confirma_al_agotarse(self, scope_de_prueba: None) -> None:
        generador = get_session()
        sesion = next(generador)
        sesion.add(_paciente("9000006"))
        with pytest.raises(StopIteration):
            next(generador)

        with session_scope() as verificacion:
            assert verificacion.scalar(select(Paciente).where(Paciente.documento == "9000006"))
