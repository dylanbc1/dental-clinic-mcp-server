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
    def scope_falso() -> Iterator[Session]:
        yield session_
        session_.commit()

    monkeypatch.setattr(mod, "session_scope", scope_falso)
    return lambda: session_


class TestArgumentos:
    def test_los_valores_por_defecto_vienen_de_settings(self) -> None:
        args = mod._parse_args([])
        assert args.seed > 0
        assert args.patients > 0
        assert args.agenda_days > 0
        assert args.base_date is None
        assert args.if_empty is False

    def test_se_pueden_sobreescribir(self) -> None:
        args = mod._parse_args(
            ["--seed", "7", "--patients", "3", "--agenda-days", "2", "--base-date", "2026-01-05"]
        )
        assert args.seed == 7
        assert args.patients == 3
        assert args.agenda_days == 2
        assert args.base_date == date(2026, 1, 5)

    def test_una_fecha_invalida_se_rechaza(self) -> None:
        with pytest.raises(SystemExit):
            mod._parse_args(["--base-date", "no-es-fecha"])


class TestEjecucion:
    ARGUMENTOS: ClassVar[list[str]] = [
        "--patients",
        "8",
        "--agenda-days",
        "3",
        "--base-date",
        "2026-08-31",
    ]

    def test_siembra_y_reporta(
        self, cli: Callable[[], Session], capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(self.ARGUMENTOS) == 0
        salida = capsys.readouterr().out
        assert "Seed done" in salida
        assert "paciente" in salida
        assert cli().scalar(select(func.count()).select_from(Patient)) == 8

    def test_if_empty_no_toca_una_base_con_datos(
        self, cli: Callable[[], Session], capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(self.ARGUMENTOS)
        session_ = cli()
        documentos_antes = sorted(session_.scalars(select(Patient.documento)))

        assert main([*self.ARGUMENTOS, "--if-empty"]) == 0
        assert "skipped" in capsys.readouterr().out
        assert sorted(session_.scalars(select(Patient.documento))) == documentos_antes

    def test_if_empty_si_siembra_una_base_vacia(
        self, cli: Callable[[], Session], empty_tables: Session
    ) -> None:
        assert main([*self.ARGUMENTOS, "--if-empty"]) == 0
        assert cli().scalar(select(func.count()).select_from(Patient)) == 8


class TestParametros:
    def test_la_historia_nunca_baja_de_una_semana(self) -> None:
        """Without past days the seed produces no attended appointments, and
        therefore no accounts receivable to demo."""
        assert SeedParams(1, 10, 2, date(2026, 8, 31)).history_days == 7

    def test_la_historia_escala_con_la_agenda(self) -> None:
        assert SeedParams(1, 10, 40, date(2026, 8, 31)).history_days == 20
