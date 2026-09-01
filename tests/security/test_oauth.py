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
AUDIENCIA = "http://mcp.test"
REDIRECT = "http://localhost:6274/oauth/callback"


@pytest.fixture(scope="module")
def par() -> KeyPair:
    return generate()


@pytest.fixture
def as_(par: KeyPair) -> AuthorizationServer:
    return AuthorizationServer(
        issuer=ISSUER,
        audience=AUDIENCIA,
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
    def test_publica_la_metadata_rfc8414(self, client: TestClient) -> None:
        m = client.get("/.well-known/oauth-authorization-server").json()
        assert m["issuer"] == ISSUER
        assert m["authorization_endpoint"] == f"{ISSUER}/authorize"
        assert m["token_endpoint"] == f"{ISSUER}/token"
        assert m["jwks_uri"] == f"{ISSUER}/jwks.json"

    def test_solo_anuncia_lo_que_oauth21_permite(self, client: TestClient) -> None:
        """OAuth 2.1 removes the implicit and password grants. Advertising them
        would be advertising a vulnerability."""
        m = client.get("/.well-known/oauth-authorization-server").json()
        assert m["grant_types_supported"] == ["authorization_code"]
        assert "implicit" not in m["response_types_supported"]
        assert "password" not in m["grant_types_supported"]

    def test_exige_pkce_s256_y_solo_s256(self, client: TestClient) -> None:
        m = client.get("/.well-known/oauth-authorization-server").json()
        assert m["code_challenge_methods_supported"] == ["S256"]
        assert "plain" not in m["code_challenge_methods_supported"]

    def test_anuncia_los_tres_scopes_del_servidor(self, client: TestClient) -> None:
        m = client.get("/.well-known/oauth-authorization-server").json()
        assert set(m["scopes_supported"]) == {"read", "write", "clinical"}

    def test_declara_preferencia_por_cimd(self, client: TestClient) -> None:
        """Spec 2026-07-28 prefers Client ID Metadata Documents over DCR."""
        m = client.get("/.well-known/oauth-authorization-server").json()
        assert m["client_id_metadata_document_supported"] is True

    def test_el_jwks_expone_la_publica_y_solo_la_publica(self, client: TestClient) -> None:
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
    def test_el_camino_feliz_devuelve_un_codigo(self, client: TestClient) -> None:
        _, response = authorize(client)
        assert response.status_code == 302
        target = urlparse(response.headers["location"])
        assert parse_qs(target.query)["state"] == ["abc"]
        assert codigo_de(response)

    def test_sin_pkce_no_hay_codigo(self, client: TestClient) -> None:
        _, response = authorize(client, code_challenge="", code_challenge_method="")
        assert "error=invalid_request" in response.headers["location"]

    def test_pkce_plain_se_rechaza(self, client: TestClient) -> None:
        """`plain` offers no protection against an intercepted code."""
        _, response = authorize(client, code_challenge_method="plain")
        assert "error=invalid_request" in response.headers["location"]

    def test_una_redirect_uri_no_registrada_no_redirige(self, client: TestClient) -> None:
        """Redirecting to an unregistered URI is an open redirect and an
        authorization-code exfiltration path, so it answers 400 instead."""
        _, response = authorize(client, redirect_uri="https://atacante.test/robar")
        assert response.status_code == 400
        assert "no registrada" in response.json()["error_description"]

    def test_un_cliente_desconocido_se_rechaza(self, client: TestClient) -> None:
        _, response = authorize(client, client_id="cliente-inventado")
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_client"

    def test_un_response_type_no_soportado_se_rechaza(self, client: TestClient) -> None:
        _, response = authorize(client, response_type="token")
        assert "error=unsupported_response_type" in response.headers["location"]

    def test_un_scope_inventado_se_rechaza(self, client: TestClient) -> None:
        _, response = authorize(client, scope="read admin")
        assert "error=invalid_scope" in response.headers["location"]


# --------------------------------------------------------------------------- #
# Token endpoint
# --------------------------------------------------------------------------- #


class TestToken:
    def _canjear(self, client: TestClient, codigo: str, verifier: str, **extra: str) -> Any:
        payload = {
            "grant_type": "authorization_code",
            "code": codigo,
            "code_verifier": verifier,
            "client_id": DEMO_CLIENT,
        }
        payload.update(extra)
        return client.post("/token", data=payload)

    def test_el_canje_devuelve_un_bearer(self, client: TestClient) -> None:
        verifier, response = authorize(client)
        token = self._canjear(client, codigo_de(response), verifier).json()
        assert token["token_type"] == "Bearer"
        assert token["scope"] == "read write"
        assert token["expires_in"] == 900
        assert token["access_token"].count(".") == 2

    def test_un_code_verifier_incorrecto_se_rechaza(self, client: TestClient) -> None:
        """The point of PKCE: a stolen code is useless without the verifier."""
        _, response = authorize(client)
        salida = self._canjear(client, codigo_de(response), "verificador-equivocado")
        assert salida.status_code == 400
        assert salida.json()["error"] == "invalid_grant"

    def test_un_codigo_no_se_puede_canjear_dos_veces(self, client: TestClient) -> None:
        verifier, response = authorize(client)
        codigo = codigo_de(response)
        assert self._canjear(client, codigo, verifier).status_code == 200
        assert self._canjear(client, codigo, verifier).status_code == 400

    def test_otro_cliente_no_puede_canjear_mi_codigo(self, client: TestClient) -> None:
        verifier, response = authorize(client)
        salida = self._canjear(client, codigo_de(response), verifier, client_id="c-otro")
        assert salida.status_code == 400

    def test_un_codigo_inexistente_se_rechaza(self, client: TestClient) -> None:
        assert self._canjear(client, "no-existe", "x").status_code == 400

    def test_otros_grants_no_se_admiten(self, client: TestClient) -> None:
        salida = client.post(
            "/token", data={"grant_type": "password", "username": "a", "password": "b"}
        )
        assert salida.status_code == 400
        assert salida.json()["error"] == "unsupported_grant_type"

    def test_el_token_lleva_las_claims_que_importan(self, client: TestClient, par: KeyPair) -> None:
        verifier, response = authorize(client)
        acceso = self._canjear(client, codigo_de(response), verifier).json()["access_token"]
        claims = jwt.decode(
            acceso, par.public_key, algorithms=["RS256"], audience=AUDIENCIA, issuer=ISSUER
        )
        assert claims["aud"] == AUDIENCIA
        assert claims["iss"] == ISSUER
        assert claims["scope"] == "read write"
        assert claims["sub"]
        assert claims["exp"] > claims["iat"]


class TestPkce:
    def test_el_verificador_acepta_el_par_correcto(self) -> None:
        verifier, challenge = pkce()
        assert verify_pkce(verifier, challenge)

    def test_rechaza_cualquier_otro_verifier(self) -> None:
        _, challenge = pkce()
        assert not verify_pkce("otro-verifier", challenge)


class TestRegistroDinamico:
    def test_registra_un_cliente_nuevo(self, client: TestClient) -> None:
        salida = client.post(
            "/register",
            json={"redirect_uris": ["http://localhost:9999/cb"], "client_name": "prueba"},
        )
        assert salida.status_code == 201
        assert salida.json()["token_endpoint_auth_method"] == "none"

    def test_exige_redirect_uris(self, client: TestClient) -> None:
        assert client.post("/register", json={"client_name": "x"}).status_code == 400

    def test_un_cuerpo_no_json_se_rechaza(self, client: TestClient) -> None:
        assert client.post("/register", content=b"no-json").status_code == 400


# --------------------------------------------------------------------------- #
# Resource-server verification
# --------------------------------------------------------------------------- #


class TestVerificadorDelResourceServer:
    @pytest.fixture
    def verificador(self, par: KeyPair) -> JWTVerifier:
        return JWTVerifier(
            issuer=ISSUER,
            audience=AUDIENCIA,
            jwks_uri=f"{ISSUER}/jwks.json",
            obtener_jwks=lambda _token: par.public_key,
        )

    async def test_acepta_un_token_legitimo(
        self, verificador: JWTVerifier, as_: AuthorizationServer
    ) -> None:
        token = as_.issue_token("dra@clinica.test", ["read", "write"])
        acceso = await verificador.verify_token(token)
        assert acceso is not None
        assert acceso.subject == "dra@clinica.test"
        assert set(acceso.scopes) == {"read", "write"}

    async def test_rechaza_un_token_expirado(
        self, verificador: JWTVerifier, as_: AuthorizationServer
    ) -> None:
        token = as_.issue_token("x", ["read"], ttl=1, now=time.time() - 3600)
        assert await verificador.verify_token(token) is None

    async def test_rechaza_un_token_de_otro_emisor(
        self, verificador: JWTVerifier, par: KeyPair
    ) -> None:
        ajeno = AuthorizationServer(
            issuer="http://otro-as.test", audience=AUDIENCIA, scopes=["read"], par=par
        )
        assert await verificador.verify_token(ajeno.issue_token("x", ["read"])) is None

    async def test_rechaza_un_token_para_otra_audiencia(
        self, verificador: JWTVerifier, as_: AuthorizationServer
    ) -> None:
        """The confused-deputy defence. Same issuer, same signature, different
        resource server, and it must not be replayable here."""
        token = as_.issue_token("x", ["read"], audiencia="http://otro-servicio.test")
        assert await verificador.verify_token(token) is None

    async def test_rechaza_un_token_firmado_con_otra_llave(self, verificador: JWTVerifier) -> None:
        otro = AuthorizationServer(
            issuer=ISSUER, audience=AUDIENCIA, scopes=["read"], par=generate()
        )
        assert await verificador.verify_token(otro.issue_token("x", ["read"])) is None

    async def test_rechaza_un_token_sin_firma_alg_none(self, verificador: JWTVerifier) -> None:
        """The classic JWT attack: swap the algorithm for `none`. The verifier
        pins RS256, so it never gets a chance."""
        crudo = jwt.encode(
            {
                "iss": ISSUER,
                "sub": "atacante",
                "aud": AUDIENCIA,
                "scope": "read write clinical",
                "iat": int(time.time()),
                "exp": int(time.time()) + 3600,
            },
            key="",
            algorithm="none",
        )
        assert await verificador.verify_token(crudo) is None

    @pytest.mark.parametrize("basura", ["", "no-es-un-jwt", "a.b.c", "Bearer algo"])
    async def test_basura_devuelve_none_sin_reventar(
        self, verificador: JWTVerifier, basura: str
    ) -> None:
        assert await verificador.verify_token(basura) is None

    async def test_un_token_sin_scope_no_trae_permisos(
        self, verificador: JWTVerifier, as_: AuthorizationServer
    ) -> None:
        acceso = await verificador.verify_token(as_.issue_token("x", []))
        assert acceso is not None
        assert acceso.scopes == []


class TestLlaves:
    def test_la_llave_efimera_se_marca_como_tal(self, par: KeyPair) -> None:
        assert par.ephemeral is True

    def test_el_jwk_no_contiene_material_privado(self, par: KeyPair) -> None:
        jwk = par.public_jwk()
        assert "d" not in jwk and "p" not in jwk and "q" not in jwk

    def test_la_llave_es_de_al_menos_2048_bits(self, par: KeyPair) -> None:
        assert par.private.key_size >= 2048


class TestPaginaDeInicio:
    def test_hay_una_pagina_que_orienta(self, client: TestClient) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert "oauth-authorization-server" in response.text


class TestFabrica:
    def test_crear_as_toma_los_scopes_del_servidor_mcp(self) -> None:
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
