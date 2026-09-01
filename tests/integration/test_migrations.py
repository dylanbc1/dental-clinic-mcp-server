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


def _real_differences(engine: Engine) -> list[object]:
    with engine.connect() as conn:
        context = MigrationContext.configure(conn, opts={"compare_type": True})
        crudas = compare_metadata(context, Base.metadata)
    real = []
    for difference in crudas:
        text_of = repr(difference)
        if any(expected in text_of for expected in DIFERENCIAS_ESPERADAS):
            continue
        real.append(difference)
    return real


class TestSynchronisation:
    def test_the_migrated_schema_matches_the_models(self, engine: Engine) -> None:
        """The test that catches "I edited the model and forgot the migration"."""
        differences = _real_differences(engine)
        assert differences == [], f"The schema and the models drifted apart: {differences}"


class TestMigrationHistory:
    def test_there_is_a_single_head(self, alembic_config: Config) -> None:
        """Two heads mean a merge nobody performed; upgrades become ambiguous."""
        script = ScriptDirectory.from_config(alembic_config)
        assert len(script.get_heads()) == 1

    def test_every_revision_declares_upgrade_and_downgrade(self, alembic_config: Config) -> None:
        script = ScriptDirectory.from_config(alembic_config)
        revisiones = list(script.walk_revisions())
        assert revisiones
        for revision in revisiones:
            modulo = revision.module
            assert callable(modulo.upgrade)
            assert callable(modulo.downgrade)

    def test_the_database_is_stamped_at_the_head(
        self, engine: Engine, alembic_config: Config
    ) -> None:
        script = ScriptDirectory.from_config(alembic_config)
        with engine.connect() as conn:
            current = MigrationContext.configure(conn).get_current_revision()
        assert current == script.get_current_head()


class TestReversibility:
    def test_downgrade_and_upgrade_rebuild_the_same_schema(
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
        leftover = [t for t in inspect(engine).get_table_names() if t != "alembic_version"]
        assert leftover == [], f"downgrade left orphan tables: {leftover}"

        command.upgrade(alembic_config, "head")
        despues = {
            table: sorted(c["name"] for c in inspect(engine).get_columns(table))
            for table in sorted(inspect(engine).get_table_names())
            if table != "alembic_version"
        }
        assert despues == antes

    def test_after_rebuilding_there_is_still_no_drift(self, engine: Engine) -> None:
        assert _real_differences(engine) == []
