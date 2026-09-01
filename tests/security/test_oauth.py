"""Security layer 1: OAuth 2.1 + PKCE, end to end.

Two halves are exercised. First the authorization server: does it actually
*enforce* what OAuth 2.1 requires, or merely advertise it? Then the resource
server's token verifier: does it reject the tokens it should?

The second half matters most. A verifier that checks the signature but not the
audience is the confused deputy: any service sharing an authorization server can
mint a token and replay it here.
"""

from __future__ import annotations

import base64
import hashlib
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from starlette.testclient import TestClient

from mcp_server.auth import JWTVerifier, Scope
from mcp_server.oauth.keys import KeyPair, generate
from mcp_server.oauth.server import DEMO_CLIENT, AuthorizationServer, verify_pkce

pytestmark = pytest.mark.security

ISSUER = "http://as.test"
AUDIENCE = "http://mcp.test"
REDIRECT = "http://localhost:6274/oauth/callback"


@pytest.fixture(scope="module")
def par() -> KeyPair:
    return generate()


@pytest.fixture
def as_(par: KeyPair) -> AuthorizationServer:
    return AuthorizationServer(
        issuer=ISSUER,
        audience=AUDIENCE,
        scopes=[str(s) for s in Scope],
        ttl_token=900,
        par=par,
    )


@pytest.fixture
def client(as_: AuthorizationServer) -> TestClient:
    return TestClient(as_.app(), follow_redirects=False)


def pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(b"x" * 48).decode().rstrip("=")
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    return verifier, challenge


def authorize(client: TestClient, **extra: str) -> Any:
    verifier, challenge = pkce()
    params = {
        "response_type": "code",
        "client_id": DEMO_CLIENT,
        "redirect_uri": REDIRECT,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": "read write",
        "state": "abc",
    }
    params.update(extra)
    return verifier, client.get("/authorize", params=params)


def codigo_de(response: Any) -> str:
    target = urlparse(response.headers["location"])
    return parse_qs(target.query)["code"][0]


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


class TestMetadata:
    def test_it_publishes_the_rfc8414_metadata(self, client: TestClient) -> None:
        m = client.get("/.well-known/oauth-authorization-server").json()
        assert m["issuer"] == ISSUER
        assert m["authorization_endpoint"] == f"{ISSUER}/authorize"
        assert m["token_endpoint"] == f"{ISSUER}/token"
        assert m["jwks_uri"] == f"{ISSUER}/jwks.json"

    def test_it_only_advertises_what_oauth21_allows(self, client: TestClient) -> None:
        """OAuth 2.1 removes the implicit and password grants. Advertising them
        would be advertising a vulnerability."""
        m = client.get("/.well-known/oauth-authorization-server").json()
        assert m["grant_types_supported"] == ["authorization_code"]
        assert "implicit" not in m["response_types_supported"]
        assert "password" not in m["grant_types_supported"]

    def test_requires_pkce_s256_and_only_s256(self, client: TestClient) -> None:
        m = client.get("/.well-known/oauth-authorization-server").json()
        assert m["code_challenge_methods_supported"] == ["S256"]
        assert "plain" not in m["code_challenge_methods_supported"]

    def test_announces_the_servers_three_scopes(self, client: TestClient) -> None:
        m = client.get("/.well-known/oauth-authorization-server").json()
        assert set(m["scopes_supported"]) == {"read", "write", "clinical"}

    def test_declares_a_preference_for_cimd(self, client: TestClient) -> None:
        """Spec 2026-07-28 prefers Client ID Metadata Documents over DCR."""
        m = client.get("/.well-known/oauth-authorization-server").json()
        assert m["client_id_metadata_document_supported"] is True

    def test_the_jwks_exposes_the_public_key_and_only_that(self, client: TestClient) -> None:
        jwks = client.get("/jwks.json").json()
        assert len(jwks["keys"]) == 1
        key = jwks["keys"][0]
        assert key["kty"] == "RSA"
        assert key["alg"] == "RS256"
        assert set(key) == {"kty", "use", "alg", "kid", "n", "e"}
        assert "d" not in key  # never the private exponent


# --------------------------------------------------------------------------- #
# Authorization endpoint
# --------------------------------------------------------------------------- #


class TestAuthorize:
    def test_the_happy_path_returns_a_code(self, client: TestClient) -> None:
        _, response = authorize(client)
        assert response.status_code == 302
        target = urlparse(response.headers["location"])
        assert parse_qs(target.query)["state"] == ["abc"]
        assert codigo_de(response)

    def test_without_pkce_there_is_no_code(self, client: TestClient) -> None:
        _, response = authorize(client, code_challenge="", code_challenge_method="")
        assert "error=invalid_request" in response.headers["location"]

    def test_pkce_plain_is_refused(self, client: TestClient) -> None:
        """`plain` offers no protection against an intercepted code."""
        _, response = authorize(client, code_challenge_method="plain")
        assert "error=invalid_request" in response.headers["location"]

    def test_an_unregistered_redirect_uri_does_not_redirect(self, client: TestClient) -> None:
        """Redirecting to an unregistered URI is an open redirect and an
        authorization-code exfiltration path, so it answers 400 instead."""
        _, response = authorize(client, redirect_uri="https://atacante.test/robar")
        assert response.status_code == 400
        assert "no registrada" in response.json()["error_description"]

    def test_an_unknown_client_is_refused(self, client: TestClient) -> None:
        _, response = authorize(client, client_id="cliente-inventado")
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_client"

    def test_an_unsupported_response_type_is_refused(self, client: TestClient) -> None:
        _, response = authorize(client, response_type="token")
        assert "error=unsupported_response_type" in response.headers["location"]

    def test_an_invented_scope_is_refused(self, client: TestClient) -> None:
        _, response = authorize(client, scope="read admin")
        assert "error=invalid_scope" in response.headers["location"]


# --------------------------------------------------------------------------- #
# Token endpoint
# --------------------------------------------------------------------------- #


class TestToken:
    def _canjear(self, client: TestClient, code: str, verifier: str, **extra: str) -> Any:
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": DEMO_CLIENT,
        }
        payload.update(extra)
        return client.post("/token", data=payload)

    def test_the_exchange_returns_a_bearer(self, client: TestClient) -> None:
        verifier, response = authorize(client)
        token = self._canjear(client, codigo_de(response), verifier).json()
        assert token["token_type"] == "Bearer"
        assert token["scope"] == "read write"
        assert token["expires_in"] == 900
        assert token["access_token"].count(".") == 2

    def test_a_wrong_code_verifier_is_refused(self, client: TestClient) -> None:
        """The point of PKCE: a stolen code is useless without the verifier."""
        _, response = authorize(client)
        output = self._canjear(client, codigo_de(response), "verificador-equivocado")
        assert output.status_code == 400
        assert output.json()["error"] == "invalid_grant"

    def test_a_code_cannot_be_redeemed_twice(self, client: TestClient) -> None:
        verifier, response = authorize(client)
        code = codigo_de(response)
        assert self._canjear(client, code, verifier).status_code == 200
        assert self._canjear(client, code, verifier).status_code == 400

    def test_another_client_cannot_redeem_my_code(self, client: TestClient) -> None:
        verifier, response = authorize(client)
        output = self._canjear(client, codigo_de(response), verifier, client_id="c-otro")
        assert output.status_code == 400

    def test_a_nonexistent_code_is_refused(self, client: TestClient) -> None:
        assert self._canjear(client, "no-existe", "x").status_code == 400

    def test_other_grants_are_not_accepted(self, client: TestClient) -> None:
        output = client.post(
            "/token", data={"grant_type": "password", "username": "a", "password": "b"}
        )
        assert output.status_code == 400
        assert output.json()["error"] == "unsupported_grant_type"

    def test_the_token_carries_the_claims_that_matter(
        self, client: TestClient, par: KeyPair
    ) -> None:
        verifier, response = authorize(client)
        acceso = self._canjear(client, codigo_de(response), verifier).json()["access_token"]
        claims = jwt.decode(
            acceso, par.public_key, algorithms=["RS256"], audience=AUDIENCE, issuer=ISSUER
        )
        assert claims["aud"] == AUDIENCE
        assert claims["iss"] == ISSUER
        assert claims["scope"] == "read write"
        assert claims["sub"]
        assert claims["exp"] > claims["iat"]


class TestPkce:
    def test_the_verifier_accepts_the_right_pair(self) -> None:
        verifier, challenge = pkce()
        assert verify_pkce(verifier, challenge)

    def test_it_refuses_any_other_verifier(self) -> None:
        _, challenge = pkce()
        assert not verify_pkce("otro-verifier", challenge)


class TestDynamicRegistration:
    def test_it_registers_a_new_client(self, client: TestClient) -> None:
        output = client.post(
            "/register",
            json={
                "redirect_uris": ["http://localhost:9999/cb"],
                "client_name": "cliente de prueba",
            },
        )
        assert output.status_code == 201
        assert output.json()["token_endpoint_auth_method"] == "none"

    def test_requires_redirect_uris(self, client: TestClient) -> None:
        assert client.post("/register", json={"client_name": "x"}).status_code == 400

    def test_a_non_json_body_is_refused(self, client: TestClient) -> None:
        assert client.post("/register", content=b"no-json").status_code == 400


# --------------------------------------------------------------------------- #
# Resource-server verification
# --------------------------------------------------------------------------- #


class TestResourceServerVerifier:
    @pytest.fixture
    def verificador(self, par: KeyPair) -> JWTVerifier:
        return JWTVerifier(
            issuer=ISSUER,
            audience=AUDIENCE,
            jwks_uri=f"{ISSUER}/jwks.json",
            obtener_jwks=lambda _token: par.public_key,
        )

    async def test_accepts_a_legitimate_token(
        self, verificador: JWTVerifier, as_: AuthorizationServer
    ) -> None:
        token = as_.issue_token("dra@clinica.test", ["read", "write"])
        acceso = await verificador.verify_token(token)
        assert acceso is not None
        assert acceso.subject == "dra@clinica.test"
        assert set(acceso.scopes) == {"read", "write"}

    async def test_it_refuses_an_expired_token(
        self, verificador: JWTVerifier, as_: AuthorizationServer
    ) -> None:
        token = as_.issue_token("x", ["read"], ttl=1, now=time.time() - 3600)
        assert await verificador.verify_token(token) is None

    async def test_it_refuses_a_token_from_another_issuer(
        self, verificador: JWTVerifier, par: KeyPair
    ) -> None:
        ajeno = AuthorizationServer(
            issuer="http://otro-as.test", audience=AUDIENCE, scopes=["read"], par=par
        )
        assert await verificador.verify_token(ajeno.issue_token("x", ["read"])) is None

    async def test_it_refuses_a_token_for_another_audience(
        self, verificador: JWTVerifier, as_: AuthorizationServer
    ) -> None:
        """The confused-deputy defence. Same issuer, same signature, different
        resource server, and it must not be replayable here."""
        token = as_.issue_token("x", ["read"], audience="http://otro-servicio.test")
        assert await verificador.verify_token(token) is None

    async def test_it_refuses_a_token_signed_with_another_key(
        self, verificador: JWTVerifier
    ) -> None:
        otro = AuthorizationServer(
            issuer=ISSUER, audience=AUDIENCE, scopes=["read"], par=generate()
        )
        assert await verificador.verify_token(otro.issue_token("x", ["read"])) is None

    async def test_it_refuses_an_unsigned_token_alg_none(self, verificador: JWTVerifier) -> None:
        """The classic JWT attack: swap the algorithm for `none`. The verifier
        pins RS256, so it never gets a chance."""
        crudo = jwt.encode(
            {
                "iss": ISSUER,
                "sub": "atacante",
                "aud": AUDIENCE,
                "scope": "read write clinical",
                "iat": int(time.time()),
                "exp": int(time.time()) + 3600,
            },
            key="",
            algorithm="none",
        )
        assert await verificador.verify_token(crudo) is None

    @pytest.mark.parametrize("basura", ["", "no-es-un-jwt", "a.b.c", "Bearer algo"])
    async def test_garbage_returns_none_without_blowing_up(
        self, verificador: JWTVerifier, basura: str
    ) -> None:
        assert await verificador.verify_token(basura) is None

    async def test_a_token_with_no_scope_carries_no_permissions(
        self, verificador: JWTVerifier, as_: AuthorizationServer
    ) -> None:
        acceso = await verificador.verify_token(as_.issue_token("x", []))
        assert acceso is not None
        assert acceso.scopes == []


class TestKeys:
    def test_the_ephemeral_key_is_marked_as_such(self, par: KeyPair) -> None:
        assert par.ephemeral is True

    def test_the_jwk_carries_no_private_material(self, par: KeyPair) -> None:
        jwk = par.public_jwk()
        assert "d" not in jwk and "p" not in jwk and "q" not in jwk

    def test_the_key_is_at_least_2048_bits(self, par: KeyPair) -> None:
        assert par.private.key_size >= 2048


class TestLandingPage:
    def test_there_is_a_page_that_orients_you(self, client: TestClient) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert "oauth-authorization-server" in response.text


class TestFactory:
    def test_building_the_as_takes_the_scopes_from_the_mcp_server(self) -> None:
        """One definition of the scope list, not three."""
        from backend.config import Settings
        from mcp_server.oauth.server import build_authorization_server
        from mcp_server.server import SUPPORTED_SCOPES

        settings_ = Settings(  # type: ignore[call-arg]
            _env_file=None,
            oauth_issuer="http://as.local:9000",
            oauth_audience="http://mcp.local:8080",
        )
        server_ = build_authorization_server(settings_)
        assert server_.scopes == SUPPORTED_SCOPES
        assert server_.issuer == "http://as.local:9000"
        assert server_.audience == "http://mcp.local:8080"
