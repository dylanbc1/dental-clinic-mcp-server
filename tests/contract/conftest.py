"""Marks the MCP contract suite.

The server fixtures and helpers live in the root `tests/conftest.py`, because
the security suite drives exactly the same server.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.contract
