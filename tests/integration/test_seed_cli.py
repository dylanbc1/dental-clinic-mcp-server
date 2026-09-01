"""Command-line surface of the seed.

The `--if-empty` flag is what `docker compose up` relies on: rerunning the
stack must not wipe and regenerate a database somebody was working with.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date
from typing import ClassVar

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend import seed as mod
from backend.models import Patient
from backend.seed import SeedParams, main

pytestmark = pytest.mark.integration


@pytest.fixture
def cli(sessions: Callable[[], Session], monkeypatch: pytest.MonkeyPatch) -> Callable[[], Session]:
    """Run the CLI against the test database instead of the configured one."""
    session_ = sessions()

    @contextmanager
    def fake_scope() -> Iterator[Session]:
        yield session_
        session_.commit()

    monkeypatch.setattr(mod, "session_scope", fake_scope)
    return lambda: session_


class TestArguments:
    def test_the_defaults_come_from_settings(self) -> None:
        args = mod._parse_args([])
        assert args.seed > 0
        assert args.patients > 0
        assert args.agenda_days > 0
        assert args.base_date is None
        assert args.if_empty is False

    def test_they_can_be_overridden(self) -> None:
        args = mod._parse_args(
            ["--seed", "7", "--patients", "3", "--agenda-days", "2", "--base-date", "2026-01-05"]
        )
        assert args.seed == 7
        assert args.patients == 3
        assert args.agenda_days == 2
        assert args.base_date == date(2026, 1, 5)

    def test_an_invalid_date_is_refused(self) -> None:
        with pytest.raises(SystemExit):
            mod._parse_args(["--base-date", "no-es-fecha"])


class TestExecution:
    ARGUMENTS: ClassVar[list[str]] = [
        "--patients",
        "8",
        "--agenda-days",
        "3",
        "--base-date",
        "2026-08-31",
    ]

    def test_it_seeds_and_reports(
        self, cli: Callable[[], Session], capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(self.ARGUMENTS) == 0
        output = capsys.readouterr().out
        assert "Seed done" in output
        assert "patient" in output
        assert cli().scalar(select(func.count()).select_from(Patient)) == 8

    def test_if_empty_does_not_touch_a_database_with_data(
        self, cli: Callable[[], Session], capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(self.ARGUMENTS)
        session_ = cli()
        documents_before = sorted(session_.scalars(select(Patient.document_number)))

        assert main([*self.ARGUMENTS, "--if-empty"]) == 0
        assert "skipped" in capsys.readouterr().out
        assert sorted(session_.scalars(select(Patient.document_number))) == documents_before

    def test_if_empty_does_seed_an_empty_database(
        self, cli: Callable[[], Session], empty_tables: Session
    ) -> None:
        assert main([*self.ARGUMENTS, "--if-empty"]) == 0
        assert cli().scalar(select(func.count()).select_from(Patient)) == 8


class TestParameters:
    def test_the_history_never_drops_below_a_week(self) -> None:
        """Without past days the seed produces no attended appointments, and
        therefore no accounts receivable to demo."""
        assert SeedParams(1, 10, 2, date(2026, 8, 31)).history_days == 7

    def test_the_history_scales_with_the_agenda(self) -> None:
        assert SeedParams(1, 10, 40, date(2026, 8, 31)).history_days == 20
