"""Security layer 5 (transport): the guards in front of the protocol.

Three controls, all of them on the HTTP surface rather than inside a tool:

* **Authentication**: an unauthenticated call gets a 401 and, crucially, a
  `WWW-Authenticate` header naming where to authenticate. Without it a compliant
  client cannot discover the authorization server and simply fails.
* **DNS-rebinding protection**: Host and Origin validation. Without it, a page
  the user visits in a browser can reach a server bound to localhost and drive
  it with the user's own credentials. This is the attack that makes "it only
  listens on 127.0.0.1" a false comfort.
* **Rate limiting**: protects the database from an agent stuck in a retry loop.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from backend.config import Settings
from mcp_server.audit import Auditor
from mcp_server.client import BackendClient
from mcp_server.context import ToolContext
from mcp_server.rate_limit import RequestLimiter, SlidingWindow
from mcp_server.server import build_app

pytestmark = pytest.mark.security

PUBLICA = "http://localhost:8080"


@pytest.fixture
def settings_() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        app_env="test",
        mcp_public_url=PUBLICA,
        oauth_issuer="http://localhost:9000",
        oauth_audience=PUBLICA,
        mcp_allowed_hosts="localhost,127.0.0.1",
        mcp_allowed_origins="http://localhost:8080",
    )


@pytest.fixture
def ctx_without_backend() -> ToolContext:
    """The transport guards run before any tool, so the backend is never reached."""
    return ToolContext(
        client=BackendClient("http://backend-inexistente"),
        auditor=Auditor(),
        exigir_auth=True,
    )


@pytest.fixture
def client(ctx_without_backend: ToolContext, settings_: Settings) -> Iterator[TestClient]:
    """The production shape: authentication on."""
    app = build_app(ctx_without_backend, config=settings_, con_auth=True)
    with TestClient(app, base_url="http://localhost:8080") as c:
        yield c


@pytest.fixture
def client_without_auth(
    ctx_without_backend: ToolContext, settings_: Settings
) -> Iterator[TestClient]:
    """The local-development shape: no authorization server.

    This is the configuration in which DNS rebinding is exploitable: a page in
    the user's browser resolving to 127.0.0.1 and driving a server that trusts
    whoever reaches it. So the Host/Origin guard is asserted here.
    """
    ctx_without_backend.exigir_auth = False
    app = build_app(ctx_without_backend, config=settings_, con_auth=False)
    with TestClient(app, base_url="http://localhost:8080") as c:
        yield c


MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "probe-client", "version": "1"},
    },
}


# --------------------------------------------------------------------------- #
# Layer 1 on the wire
# --------------------------------------------------------------------------- #


class TestAuthentication:
    def test_without_a_token_it_answers_401(self, client: TestClient) -> None:
        response = client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)
        assert response.status_code == 401

    def test_the_401_says_where_to_authenticate(self, client: TestClient) -> None:
        """A 401 without WWW-Authenticate leaves a compliant client with nowhere
        to go: it cannot discover the authorization server."""
        response = client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)
        cabecera = response.headers.get("www-authenticate", "")
        assert cabecera.lower().startswith("bearer")
        assert "resource_metadata" in cabecera

    def test_a_garbage_token_is_401_too(self, client: TestClient) -> None:
        response = client.post(
            "/mcp",
            json=INITIALIZE,
            headers={**MCP_HEADERS, "Authorization": "Bearer no-es-un-jwt"},
        )
        assert response.status_code == 401

    def test_it_publishes_the_protected_resource_metadata(self, client: TestClient) -> None:
        response = client.get("/.well-known/oauth-protected-resource")
        assert response.status_code == 200
        body = response.json()
        assert body["resource"].rstrip("/") == PUBLICA
        assert "http://localhost:9000" in [s.rstrip("/") for s in body["authorization_servers"]]

    def test_the_metadata_does_not_require_authentication(self, client: TestClient) -> None:
        """Requiring a token to discover where to get a token is a deadlock."""
        assert client.get("/.well-known/oauth-protected-resource").status_code == 200


# --------------------------------------------------------------------------- #
# DNS rebinding
# --------------------------------------------------------------------------- #


class TestTransportGuards:
    @pytest.mark.parametrize(
        "origen",
        [
            "http://evil.test",
            "https://atacante.example",
            "http://localhost:9999",
            "null",
        ],
    )
    def test_an_origin_that_is_not_allowed_is_refused(
        self, client_without_auth: TestClient, origen: str
    ) -> None:
        response = client_without_auth.post(
            "/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "Origin": origen}
        )
        # A bad Origin is a forbidden *caller* (403); a bad Host is a request
        # that arrived at the wrong server (421). The SDK distinguishes them.
        assert response.status_code == 403, (
            f"Origin '{origen}' no fue refused: una página web podría manejar "
            "este servidor con las credenciales del usuario"
        )

    def test_an_allowed_origin_passes_the_guard(self, client_without_auth: TestClient) -> None:
        response = client_without_auth.post(
            "/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "Origin": PUBLICA}
        )
        assert response.status_code == 200, (
            "el Origin permitido debería atravesar la guarda; si falla, la lista "
            "blanca no coincide con el Host real que envía el cliente"
        )

    def test_the_host_with_a_port_passes_the_guard(self, client_without_auth: TestClient) -> None:
        """The header a browser sends is `localhost:8080`, not `localhost`. A
        bare allow-list matches nothing and rejects every legitimate request."""
        response = client_without_auth.post(
            "/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "Host": "localhost:8080"}
        )
        assert response.status_code == 200

    @pytest.mark.parametrize("host", ["evil.test", "attacker.example:8080"])
    def test_a_host_that_is_not_allowed_is_refused(
        self, client_without_auth: TestClient, host: str
    ) -> None:
        response = client_without_auth.post(
            "/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "Host": host}
        )
        assert response.status_code == 421

    def test_with_auth_on_an_anonymous_rebinding_is_still_cut(self, client: TestClient) -> None:
        """With authentication on, the auth middleware answers first. The attack
        is still blocked, just at 401 instead of 421."""
        response = client.post(
            "/mcp", json=INITIALIZE, headers={**MCP_HEADERS, "Origin": "http://evil.test"}
        )
        assert response.status_code == 401

    def test_the_allow_list_comes_from_config_and_is_expanded(self, settings_: Settings) -> None:
        assert settings_.mcp_allowed_hosts == [
            "localhost",
            "localhost:8080",
            "127.0.0.1",
            "127.0.0.1:8080",
        ]
        assert settings_.mcp_allowed_origins == ["http://localhost:8080"]

    def test_there_are_no_wildcards_in_the_allow_list(self, settings_: Settings) -> None:
        """A `*` here silently disables the guard while looking configured."""
        assert "*" not in settings_.mcp_allowed_hosts
        assert "*" not in settings_.mcp_allowed_origins


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #


class TestSlidingWindow:
    def test_it_allows_up_to_the_limit(self) -> None:
        ventana = SlidingWindow(limit=3, ventana=60)
        assert [ventana.allow("a", now=0)[0] for _ in range(3)] == [True] * 3
        assert ventana.allow("a", now=0)[0] is False

    def test_the_suggested_wait_is_useful(self) -> None:
        ventana = SlidingWindow(limit=1, ventana=60)
        ventana.allow("a", now=100)
        allowed, espera = ventana.allow("a", now=110)
        assert allowed is False
        assert 49 <= espera <= 50

    def test_it_slides_rather_than_using_a_fixed_window(self) -> None:
        """A fixed window lets a caller fire the whole budget at the seam and
        again immediately after: twice the intended rate."""
        ventana = SlidingWindow(limit=2, ventana=10)
        ventana.allow("a", now=0)
        ventana.allow("a", now=9)
        assert ventana.allow("a", now=9.5)[0] is False
        assert ventana.allow("a", now=10.1)[0] is True

    def test_every_key_has_its_own_budget(self) -> None:
        """Limiting purely by IP would let one agent starve everyone behind the
        same NAT."""
        ventana = SlidingWindow(limit=1, ventana=60)
        assert ventana.allow("sub:ana", now=0)[0] is True
        assert ventana.allow("sub:bruno", now=0)[0] is True
        assert ventana.allow("sub:ana", now=0)[0] is False


class TestRateLimitMiddleware:
    @pytest.fixture
    def rate_limited_app(self) -> Starlette:
        reloj = itertools.count(0, 0)  # frozen clock: every call is "now"

        async def ok(_: Request) -> PlainTextResponse:
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/ping", ok)])
        app.add_middleware(RequestLimiter, limit=3, ventana_segundos=60, reloj=lambda: next(reloj))
        return app

    def test_cuts_off_past_the_limit(self, rate_limited_app: Starlette) -> None:
        with TestClient(rate_limited_app) as c:
            codigos = [c.get("/ping").status_code for _ in range(5)]
        assert codigos == [200, 200, 200, 429, 429]

    def test_the_429_explains_how_long_to_wait(self, rate_limited_app: Starlette) -> None:
        with TestClient(rate_limited_app) as c:
            for _ in range(3):
                c.get("/ping")
            response = c.get("/ping")
        assert response.status_code == 429
        assert response.headers["retry-after"] == "60"
        body = response.json()
        assert body["code"] == "RATE_LIMIT_EXCEEDED"
        # It must tell an agent in a retry loop to stop looping.
        assert "bucle de reintentos" in body["suggestion"]

    def test_the_error_uses_the_same_structured_envelope(self, rate_limited_app: Starlette) -> None:
        with TestClient(rate_limited_app) as c:
            for _ in range(4):
                response = c.get("/ping")
        body = response.json()
        assert set(body) >= {"error", "code", "message", "suggestion"}

    async def test_lets_through_traffic_that_is_not_http(self) -> None:
        """A websocket or lifespan scope must not be counted or blocked."""
        visto: list[str] = []

        async def app(scope: Any, receive: Any, send: Any) -> None:
            visto.append(scope["type"])

        limitador = RequestLimiter(app, limit=0, ventana_segundos=60)
        await limitador({"type": "lifespan"}, None, None)  # type: ignore[arg-type]
        assert visto == ["lifespan"]


class TestBackendUnreachable:
    def test_the_mcp_server_does_not_expose_the_domain_api(self, client: TestClient) -> None:
        """The MCP surface is /mcp and the discovery documents. Nothing else,
        and in particular no path through to the internal REST API."""
        for path in ("/appointments/1", "/patients", "/health", "/docs", "/openapi.json"):
            assert client.get(path).status_code in {404, 405}, path


class TestBackendClientOnFailure:
    async def test_a_backend_that_is_down_gives_an_actionable_error(self) -> None:
        transporte = httpx.MockTransport(
            lambda _: (_ for _ in ()).throw(httpx.ConnectError("sin ruta al host"))
        )
        async with httpx.AsyncClient(transport=transporte, base_url="http://backend") as http:
            client = BackendClient("http://backend", client=http)
            with pytest.raises(Exception) as exc:
                await client.get_object("/clinic")
        message = str(exc.value)
        assert "BACKEND_UNAVAILABLE" in message
        assert "do not retry in a loop" in message

    async def test_a_response_with_an_unexpected_shape_is_detected(self) -> None:
        transporte = httpx.MockTransport(
            lambda _: httpx.Response(200, json=["no", "es", "un", "objeto"])
        )
        async with httpx.AsyncClient(transport=transporte, base_url="http://backend") as http:
            client = BackendClient("http://backend", client=http)
            with pytest.raises(Exception) as exc:
                await client.get_object("/clinic")
        assert "RESPUESTA_INESPERADA" in str(exc.value)

    async def test_an_error_without_an_envelope_does_not_blow_up(self) -> None:
        transporte = httpx.MockTransport(lambda _: httpx.Response(502, text="bad gateway"))
        async with httpx.AsyncClient(transport=transporte, base_url="http://backend") as http:
            client = BackendClient("http://backend", client=http)
            with pytest.raises(Exception) as exc:
                await client.get_object("/clinic")
        assert "502" in str(exc.value)


class TestProtectedResourceMetadata:
    """RFC 9728 discovery, the document a new client reads first."""

    def test_it_names_the_three_scopes(self, client: TestClient) -> None:
        """The SDK derives this field from `required_scopes`, which must stay
        empty so no scope is globally required. Left alone it advertises an
        empty list, telling a client no scopes are used here. That is false."""
        body = client.get("/.well-known/oauth-protected-resource").json()
        assert body["scopes_supported"] == ["read", "write", "clinical"]

    def test_points_at_the_authorization_server(self, client: TestClient) -> None:
        body = client.get("/.well-known/oauth-protected-resource").json()
        assert body["authorization_servers"] == ["http://localhost:9000"]
        assert body["resource"] == PUBLICA

    def test_the_documentation_points_at_something_that_exists(self, client: TestClient) -> None:
        """It must not point at a path on this server: the MCP surface is /mcp
        and the discovery documents, nothing else."""
        body = client.get("/.well-known/oauth-protected-resource").json()
        assert not body["resource_documentation"].startswith(PUBLICA)
        assert body["resource_documentation"].startswith("https://")

    def test_there_is_a_single_handler_on_that_route(
        self, ctx_without_backend: ToolContext, settings_: Settings
    ) -> None:
        """Starlette matches the first route that fits. Two handlers on the same
        path means the corrected document is one refactor away from unreachable."""
        app = build_app(ctx_without_backend, config=settings_, con_auth=True)
        rutas = [
            r
            for r in app.routes
            if getattr(r, "path", None) == "/.well-known/oauth-protected-resource"
        ]
        assert len(rutas) == 1

    def test_the_401_points_at_this_document(self, client: TestClient) -> None:
        response = client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)
        cabecera = response.headers["www-authenticate"]
        assert "/.well-known/oauth-protected-resource" in cabecera


class TestStatelessTransport:
    """Stateful application, stateless transport.

    Nothing here needs a session. Identity comes from the OAuth token and a
    pending approval travels inside its own signed token, so the transport has
    no conversation to preserve. Keeping one would mean building infrastructure
    for state the application never asked for, and pinning every client to the
    replica that happens to hold it.
    """

    def test_it_issues_no_session_identifier(self, client_without_auth: TestClient) -> None:
        response = client_without_auth.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)
        assert response.status_code == 200
        assert "mcp-session-id" not in {k.lower() for k in response.headers}

    def test_a_cold_call_works_with_no_prior_initialize(
        self, client_without_auth: TestClient
    ) -> None:
        """The property that lets any replica serve any request."""
        response = client_without_auth.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 7, "method": "tools/list"},
            headers=MCP_HEADERS,
        )
        assert response.status_code == 200
        assert "search_patients" in response.text

    def test_it_does_not_require_carrying_a_session_between_calls(
        self, client_without_auth: TestClient
    ) -> None:
        """A stateful transport answers the second call with 400 unless it
        carries the session it issued on the first."""
        client_without_auth.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)
        second = client_without_auth.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 8, "method": "tools/list"},
            headers=MCP_HEADERS,
        )
        assert second.status_code == 200

    def test_the_configuration_makes_it_explicit(self, settings_: Settings) -> None:
        assert settings_.mcp_stateless is True

    def test_stateful_mode_is_still_reachable_if_something_needed_it(
        self, ctx_without_backend: ToolContext, settings_: Settings
    ) -> None:
        """Configurable rather than hard-coded: a future feature needing
        resumability should not require a rewrite."""
        with_session = settings_.model_copy(update={"mcp_stateless": False})
        app = build_app(ctx_without_backend, config=with_session, con_auth=False)
        with TestClient(app, base_url=PUBLICA) as c:
            response = c.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)
        assert "mcp-session-id" in {k.lower() for k in response.headers}
