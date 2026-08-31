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
from backend.models import Paciente
from backend.seed import ParametrosSeed, main

pytestmark = pytest.mark.integration


@pytest.fixture
def cli(sesiones: Callable[[], Session], monkeypatch: pytest.MonkeyPatch) -> Callable[[], Session]:
    """Run the CLI against the test database instead of the configured one."""
    sesion = sesiones()

    @contextmanager
    def scope_falso() -> Iterator[Session]:
        yield sesion
        sesion.commit()

    monkeypatch.setattr(mod, "session_scope", scope_falso)
    return lambda: sesion


class TestArgumentos:
    def test_los_valores_por_defecto_vienen_de_settings(self) -> None:
        args = mod._parse_args([])
        assert args.seed > 0
        assert args.pacientes > 0
        assert args.dias_agenda > 0
        assert args.fecha_base is None
        assert args.if_empty is False

    def test_se_pueden_sobreescribir(self) -> None:
        args = mod._parse_args(
            ["--seed", "7", "--pacientes", "3", "--dias-agenda", "2", "--fecha-base", "2026-01-05"]
        )
        assert args.seed == 7
        assert args.pacientes == 3
        assert args.dias_agenda == 2
        assert args.fecha_base == date(2026, 1, 5)

    def test_una_fecha_invalida_se_rechaza(self) -> None:
        with pytest.raises(SystemExit):
            mod._parse_args(["--fecha-base", "no-es-fecha"])


class TestEjecucion:
    ARGUMENTOS: ClassVar[list[str]] = [
        "--pacientes",
        "8",
        "--dias-agenda",
        "3",
        "--fecha-base",
        "2026-08-31",
    ]

    def test_siembra_y_reporta(
        self, cli: Callable[[], Session], capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(self.ARGUMENTOS) == 0
        salida = capsys.readouterr().out
        assert "Seed listo" in salida
        assert "paciente" in salida
        assert cli().scalar(select(func.count()).select_from(Paciente)) == 8

    def test_if_empty_no_toca_una_base_con_datos(
        self, cli: Callable[[], Session], capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(self.ARGUMENTOS)
        sesion = cli()
        documentos_antes = sorted(sesion.scalars(select(Paciente.documento)))

        assert main([*self.ARGUMENTOS, "--if-empty"]) == 0
        assert "omitido" in capsys.readouterr().out
        assert sorted(sesion.scalars(select(Paciente.documento))) == documentos_antes

    def test_if_empty_si_siembra_una_base_vacia(
        self, cli: Callable[[], Session], tablas_vacias: Session
    ) -> None:
        assert main([*self.ARGUMENTOS, "--if-empty"]) == 0
        assert cli().scalar(select(func.count()).select_from(Paciente)) == 8


class TestParametros:
    def test_la_historia_nunca_baja_de_una_semana(self) -> None:
        """Without past days the seed produces no attended appointments, and
        therefore no accounts receivable to demo."""
        assert ParametrosSeed(1, 10, 2, date(2026, 8, 31)).dias_historia == 7

    def test_la_historia_escala_con_la_agenda(self) -> None:
        assert ParametrosSeed(1, 10, 40, date(2026, 8, 31)).dias_historia == 20
