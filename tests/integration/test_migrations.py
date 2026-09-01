"""Migration tests.

Migrations rot silently: someone edits a model, `create_all` in the test-suite
picks it up, and the migration that production actually runs quietly falls
behind. These tests make that impossible: the whole suite runs on a schema built
by `alembic upgrade head`, and this module asserts it still matches the models.
"""

from __future__ import annotations

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect

from backend.models import Base

pytestmark = pytest.mark.integration

#: Differences alembic reports that are not real drift. A functional index
#: (`lower(nombre)`) has no metadata counterpart alembic can compare, so it is
#: reported as "remove" on every autogenerate run.
DIFERENCIAS_ESPERADAS = {"ix_paciente_nombre_lower"}


def _diferencias_reales(engine: Engine) -> list[object]:
    with engine.connect() as conn:
        context = MigrationContext.configure(conn, opts={"compare_type": True})
        crudas = compare_metadata(context, Base.metadata)
    reales = []
    for diferencia in crudas:
        text_of = repr(diferencia)
        if any(esperada in text_of for esperada in DIFERENCIAS_ESPERADAS):
            continue
        reales.append(diferencia)
    return reales


class TestSincronizacion:
    def test_el_esquema_migrado_coincide_con_los_modelos(self, engine: Engine) -> None:
        """The test that catches "I edited the model and forgot the migration"."""
        diferencias = _diferencias_reales(engine)
        assert diferencias == [], f"El esquema y los modelos divergieron: {diferencias}"


class TestHistorialDeMigraciones:
    def test_hay_una_sola_cabeza(self, alembic_config: Config) -> None:
        """Two heads mean a merge nobody performed; upgrades become ambiguous."""
        script = ScriptDirectory.from_config(alembic_config)
        assert len(script.get_heads()) == 1

    def test_toda_revision_declara_upgrade_y_downgrade(self, alembic_config: Config) -> None:
        script = ScriptDirectory.from_config(alembic_config)
        revisiones = list(script.walk_revisions())
        assert revisiones
        for revision in revisiones:
            modulo = revision.module
            assert callable(modulo.upgrade)
            assert callable(modulo.downgrade)

    def test_la_base_esta_marcada_en_la_cabeza(
        self, engine: Engine, alembic_config: Config
    ) -> None:
        script = ScriptDirectory.from_config(alembic_config)
        with engine.connect() as conn:
            current = MigrationContext.configure(conn).get_current_revision()
        assert current == script.get_current_head()


class TestReversibilidad:
    def test_downgrade_y_upgrade_reconstruyen_el_mismo_esquema(
        self, engine: Engine, alembic_config: Config
    ) -> None:
        """A migration you cannot roll back is a migration you cannot deploy
        with confidence. This test also leaves the schema exactly as it found
        it, so the rest of the suite is unaffected."""
        antes = {
            table: sorted(c["name"] for c in inspect(engine).get_columns(table))
            for table in sorted(inspect(engine).get_table_names())
            if table != "alembic_version"
        }

        command.downgrade(alembic_config, "base")
        vacio = [t for t in inspect(engine).get_table_names() if t != "alembic_version"]
        assert vacio == [], f"downgrade dejó tablas huérfanas: {vacio}"

        command.upgrade(alembic_config, "head")
        despues = {
            table: sorted(c["name"] for c in inspect(engine).get_columns(table))
            for table in sorted(inspect(engine).get_table_names())
            if table != "alembic_version"
        }
        assert despues == antes

    def test_tras_reconstruir_sigue_sin_diferencias(self, engine: Engine) -> None:
        assert _diferencias_reales(engine) == []
