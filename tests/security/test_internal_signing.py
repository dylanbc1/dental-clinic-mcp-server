"""Security layer 1, inward: the domain API answers only its own MCP server.

The API used to authenticate nothing. It read `X-Actor` and wrote it into the
audit trail, so anything that could open a socket to it could write, without
credentials, and attribute the change to whoever it liked. "Not reachable from
outside" was a property of docker-compose, not of the code, and it stopped
being true the day the backend became a service on a shared private network.

These tests are the ones that would fail if someone removed the middleware to
make a curl work.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from backend import internal_auth
from backend.api import app
from backend.config import get_settings

pytestmark = pytest.mark.security

KEY = "dev-only-internal-api-key-change-me-32b"


@pytest.fixture
def client() -> TestClient:
    get_settings.cache_clear()
    return TestClient(app, raise_server_exceptions=False)


def signed(path: str, *, actor: str = "recepcion@clinica.local", **over: object) -> dict[str, str]:
    headers = internal_auth.sign_request(KEY, method="GET", path=path, actor=actor)
    headers.update({k: str(v) for k, v in over.items()})
    return headers


class TestAnUnsignedCaller:
    def test_a_read_is_refused(self, client: TestClient) -> None:
        assert client.get("/clinic").status_code == 401

    def test_a_write_is_refused(self, client: TestClient) -> None:
        response = client.post("/appointments", json={"patient_id": 1, "slot_id": 1})
        assert response.status_code == 401

    def test_a_forged_actor_alone_buys_nothing(self, client: TestClient) -> None:
        """The header the whole change is about. On its own it is now inert."""
        response = client.post(
            "/appointments",
            json={"patient_id": 1, "slot_id": 1},
            headers={"X-Actor": "la-jefa@clinica.local"},
        )
        assert response.status_code == 401

    def test_the_refusal_says_nothing_about_why(self, client: TestClient) -> None:
        """Naming the failing part tells an attacker whether they have the key,
        the clock or the canonical string wrong."""
        body = client.get("/clinic").json()
        assert body["code"] == "NOT_AUTHENTICATED"
        for leak in ("timestamp", "signature", "key", "skew"):
            assert leak not in body["message"].lower()


class TestASignedCaller:
    def test_is_let_through(self, client: TestClient) -> None:
        assert client.get("/clinic", headers=signed("/clinic")).status_code == 200

    def test_the_probes_need_no_signature(self, client: TestClient) -> None:
        """An orchestrator has to probe before it can hold a key."""
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 200


class TestWhatTheSignatureCovers:
    def test_editing_the_actor_invalidates_it(self, client: TestClient) -> None:
        """The point of signing the request rather than issuing a token: the
        actor is inside the signature, so it cannot be swapped in flight."""
        headers = signed("/clinic")
        headers["X-Actor"] = "otra-persona@clinica.local"
        assert client.get("/clinic", headers=headers).status_code == 401

    def test_replaying_it_on_another_path_fails(self, client: TestClient) -> None:
        headers = signed("/clinic")
        assert client.get("/patients?name=a", headers=headers).status_code == 401

    def test_editing_the_body_invalidates_it(self, client: TestClient) -> None:
        body = b'{"patient_id": 1, "slot_id": 1}'
        headers = internal_auth.sign_request(
            KEY, method="POST", path="/appointments", actor="a@b.c", body=body
        )
        headers["content-type"] = "application/json"
        tampered = b'{"patient_id": 2, "slot_id": 1}'
        assert client.post("/appointments", content=tampered, headers=headers).status_code == 401

    def test_editing_the_query_invalidates_it(self, client: TestClient) -> None:
        headers = internal_auth.sign_request(
            KEY, method="GET", path="/patients", query="name=ana", actor="a@b.c"
        )
        assert client.get("/patients?name=carlos", headers=headers).status_code == 401


class TestFreshness:
    def test_an_old_signature_is_refused(self, client: TestClient) -> None:
        """A captured request must stop working, not work forever."""
        stale = int(time.time()) - 4000
        headers = internal_auth.sign_request(
            KEY, method="GET", path="/clinic", actor="a@b.c", timestamp=stale
        )
        assert client.get("/clinic", headers=headers).status_code == 401

    def test_a_timestamp_from_the_future_is_refused(self, client: TestClient) -> None:
        ahead = int(time.time()) + 4000
        headers = internal_auth.sign_request(
            KEY, method="GET", path="/clinic", actor="a@b.c", timestamp=ahead
        )
        assert client.get("/clinic", headers=headers).status_code == 401

    def test_a_junk_timestamp_does_not_crash_the_server(self, client: TestClient) -> None:
        headers = signed("/clinic")
        headers[internal_auth.TIMESTAMP_HEADER] = "not-a-number"
        assert client.get("/clinic", headers=headers).status_code == 401


class TestTheKeyRing:
    def test_any_key_in_the_ring_verifies(self) -> None:
        """Rotation without downtime: ship [old, new], then [new, old], then
        [new]. A signature made with either must verify throughout."""
        message = internal_auth.canonical_request(
            method="GET", path="/clinic", query="", actor="a@b.c", body=b"", timestamp=1000
        )
        for key in ("old-key", "new-key"):
            assert internal_auth.verify(
                ["new-key", "old-key"],
                message,
                internal_auth.sign(key, message),
                timestamp=1000,
                now=1000.0,
                skew_seconds=300,
            )

    def test_a_key_outside_the_ring_does_not(self) -> None:
        message = internal_auth.canonical_request(
            method="GET", path="/clinic", query="", actor="a@b.c", body=b"", timestamp=1000
        )
        assert not internal_auth.verify(
            ["the-real-key"],
            message,
            internal_auth.sign("a-guess", message),
            timestamp=1000,
            now=1000.0,
            skew_seconds=300,
        )
