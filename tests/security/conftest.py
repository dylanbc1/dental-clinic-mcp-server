"""Marks every test in this package as a security control."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.security
