"""Marks every test in this package as `integration`.

The shared PostgreSQL fixtures (`sesiones`, `escenario`, `datos_minimos`) live
in the root `tests/conftest.py`, because the contract and security suites need
exactly the same scenario.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration
