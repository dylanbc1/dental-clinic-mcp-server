"""F3: values chosen to break something, and what the caller is told.

The database is safe from all of these, because SQLAlchemy parameterises and no
query in this project is built by concatenation. What was not safe was the
answer: two of them came back as INTERNAL_ERROR, which reads as "the server
broke", invites a retry of a call that will never work, and hides that the fix
belongs to whoever sent it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.api import app
from backend.database import get_session
from tests.conftest import sign_like_the_mcp_server

pytestmark = pytest.mark.security


@pytest.fixture
def client(sessions: Callable[[], Session]) -> Iterator[TestClient]:
    """A signed client on a real schema: these failures happen in the database,
    so a mocked session would not produce them."""
    session_ = sessions()

    def override() -> Iterator[Session]:
        yield session_
        session_.commit()

    app.dependency_overrides[get_session] = override
    with TestClient(app, raise_server_exceptions=False) as c:
        c.auth = sign_like_the_mcp_server
        yield c
    app.dependency_overrides.clear()


class TestAValueTheDatabaseCannotHold:
    def test_an_id_beyond_a_four_byte_integer_is_a_400(self, client: TestClient) -> None:
        """PostgreSQL raises NumericValueOutOfRange, which used to surface as a
        500 for what is plainly a bad identifier."""
        response = client.get("/appointments/1000000000000000000")
        assert response.status_code == 400
        assert response.json()["code"] == "INVALID_INPUT"

    def test_a_nul_byte_in_text_is_a_400(self, client: TestClient) -> None:
        """ "PostgreSQL text fields cannot contain NUL (0x00) bytes" is a fact
        about the caller's string, not about this server."""
        response = client.get("/patients", params={"name": "ana\x00"})
        assert response.status_code == 400
        assert response.json()["code"] == "INVALID_INPUT"

    def test_the_refusal_describes_no_schema(self, client: TestClient) -> None:
        """Naming the column or the type would hand over the schema for free."""
        body = client.get("/appointments/1000000000000000000").json()
        blob = (body["message"] + body["suggestion"]).lower()
        for leak in ("column", "integer out of range", "psycopg", "sqlalchemy", "varchar"):
            assert leak not in blob


class TestInjectionShapedText:
    @pytest.mark.parametrize(
        "payload",
        ["' OR '1'='1", "x'; DROP TABLE patient;--", "1' UNION SELECT NULL--", "../../etc/passwd"],
    )
    def test_it_is_treated_as_a_name_and_nothing_else(
        self, client: TestClient, payload: str
    ) -> None:
        """Parameterised queries make this boring, which is the point. It
        answers 200 with no matches rather than erroring, and the table is
        still there afterwards."""
        response = client.get("/patients", params={"name": payload})
        assert response.status_code == 200
        assert response.json() == []
        assert client.get("/patients", params={"name": "a"}).status_code == 200
