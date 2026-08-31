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

from mcp_server.auth import Scope, VerificadorJWT
from mcp_server.oauth.llaves import ParDeLlaves, generar
from mcp_server.oauth.servidor import CLIENTE_DEMO, AuthorizationServer, verificar_pkce

pytestmark = pytest.mark.security

ISSUER = "http://as.test"
AUDIENCIA = "http://mcp.test"
REDIRECT = "http://localhost:6274/oauth/callback"


@pytest.fixture(scope="module")
def par() -> ParDeLlaves:
    return generar()


@pytest.fixture
def as_(par: ParDeLlaves) -> AuthorizationServer:
    return AuthorizationServer(
        issuer=ISSUER,
        audience=AUDIENCIA,
        scopes=[str(s) for s in Scope],
        ttl_token=900,
        par=par,
    )


@pytest.fixture
def cliente(as_: AuthorizationServer) -> TestClient:
    return TestClient(as_.app(), follow_redirects=False)


def pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(b"x" * 48).decode().rstrip("=")
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    return verifier, challenge


def autorizar(cliente: TestClient, **extra: str) -> Any:
    verifier, challenge = pkce()
    params = {
        "response_type": "code",
        "client_id": CLIENTE_DEMO,
        "redirect_uri": REDIRECT,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": "read write",
        "state": "abc",
    }
    params.update(extra)
    return verifier, cliente.get("/authorize", params=params)


def codigo_de(respuesta: Any) -> str:
    destino = urlparse(respuesta.headers["location"])
    return parse_qs(destino.query)["code"][0]


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


class TestMetadata:
    def test_publica_la_metadata_rfc8414(self, cliente: TestClient) -> None:
        m = cliente.get("/.well-known/oauth-authorization-server").json()
        assert m["issuer"] == ISSUER
        assert m["authorization_endpoint"] == f"{ISSUER}/authorize"
        assert m["token_endpoint"] == f"{ISSUER}/token"
        assert m["jwks_uri"] == f"{ISSUER}/jwks.json"

    def test_solo_anuncia_lo_que_oauth21_permite(self, cliente: TestClient) -> None:
        """OAuth 2.1 removes the implicit and password grants. Advertising them
        would be advertising a vulnerability."""
        m = cliente.get("/.well-known/oauth-authorization-server").json()
        assert m["grant_types_supported"] == ["authorization_code"]
        assert "implicit" not in m["response_types_supported"]
        assert "password" not in m["grant_types_supported"]

    def test_exige_pkce_s256_y_solo_s256(self, cliente: TestClient) -> None:
        m = cliente.get("/.well-known/oauth-authorization-server").json()
        assert m["code_challenge_methods_supported"] == ["S256"]
        assert "plain" not in m["code_challenge_methods_supported"]

    def test_anuncia_los_tres_scopes_del_servidor(self, cliente: TestClient) -> None:
        m = cliente.get("/.well-known/oauth-authorization-server").json()
        assert set(m["scopes_supported"]) == {"read", "write", "clinical"}

    def test_declara_preferencia_por_cimd(self, cliente: TestClient) -> None:
        """Spec 2026-07-28 prefers Client ID Metadata Documents over DCR."""
        m = cliente.get("/.well-known/oauth-authorization-server").json()
        assert m["client_id_metadata_document_supported"] is True

    def test_el_jwks_expone_la_publica_y_solo_la_publica(self, cliente: TestClient) -> None:
        jwks = cliente.get("/jwks.json").json()
        assert len(jwks["keys"]) == 1
        clave = jwks["keys"][0]
        assert clave["kty"] == "RSA"
        assert clave["alg"] == "RS256"
        assert set(clave) == {"kty", "use", "alg", "kid", "n", "e"}
        assert "d" not in clave  # never the private exponent


# --------------------------------------------------------------------------- #
# Authorization endpoint
# --------------------------------------------------------------------------- #


class TestAuthorize:
    def test_el_camino_feliz_devuelve_un_codigo(self, cliente: TestClient) -> None:
        _, respuesta = autorizar(cliente)
        assert respuesta.status_code == 302
        destino = urlparse(respuesta.headers["location"])
        assert parse_qs(destino.query)["state"] == ["abc"]
        assert codigo_de(respuesta)

    def test_sin_pkce_no_hay_codigo(self, cliente: TestClient) -> None:
        _, respuesta = autorizar(cliente, code_challenge="", code_challenge_method="")
        assert "error=invalid_request" in respuesta.headers["location"]

    def test_pkce_plain_se_rechaza(self, cliente: TestClient) -> None:
        """`plain` offers no protection against an intercepted code."""
        _, respuesta = autorizar(cliente, code_challenge_method="plain")
        assert "error=invalid_request" in respuesta.headers["location"]

    def test_una_redirect_uri_no_registrada_no_redirige(self, cliente: TestClient) -> None:
        """Redirecting to an unregistered URI is an open redirect and an
        authorization-code exfiltration path, so it answers 400 instead."""
        _, respuesta = autorizar(cliente, redirect_uri="https://atacante.test/robar")
        assert respuesta.status_code == 400
        assert "no registrada" in respuesta.json()["error_description"]

    def test_un_cliente_desconocido_se_rechaza(self, cliente: TestClient) -> None:
        _, respuesta = autorizar(cliente, client_id="cliente-inventado")
        assert respuesta.status_code == 400
        assert respuesta.json()["error"] == "invalid_client"

    def test_un_response_type_no_soportado_se_rechaza(self, cliente: TestClient) -> None:
        _, respuesta = autorizar(cliente, response_type="token")
        assert "error=unsupported_response_type" in respuesta.headers["location"]

    def test_un_scope_inventado_se_rechaza(self, cliente: TestClient) -> None:
        _, respuesta = autorizar(cliente, scope="read admin")
        assert "error=invalid_scope" in respuesta.headers["location"]


# --------------------------------------------------------------------------- #
# Token endpoint
# --------------------------------------------------------------------------- #


class TestToken:
    def _canjear(self, cliente: TestClient, codigo: str, verifier: str, **extra: str) -> Any:
        datos = {
            "grant_type": "authorization_code",
            "code": codigo,
            "code_verifier": verifier,
            "client_id": CLIENTE_DEMO,
        }
        datos.update(extra)
        return cliente.post("/token", data=datos)

    def test_el_canje_devuelve_un_bearer(self, cliente: TestClient) -> None:
        verifier, respuesta = autorizar(cliente)
        token = self._canjear(cliente, codigo_de(respuesta), verifier).json()
        assert token["token_type"] == "Bearer"
        assert token["scope"] == "read write"
        assert token["expires_in"] == 900
        assert token["access_token"].count(".") == 2

    def test_un_code_verifier_incorrecto_se_rechaza(self, cliente: TestClient) -> None:
        """The point of PKCE: a stolen code is useless without the verifier."""
        _, respuesta = autorizar(cliente)
        salida = self._canjear(cliente, codigo_de(respuesta), "verificador-equivocado")
        assert salida.status_code == 400
        assert salida.json()["error"] == "invalid_grant"

    def test_un_codigo_no_se_puede_canjear_dos_veces(self, cliente: TestClient) -> None:
        verifier, respuesta = autorizar(cliente)
        codigo = codigo_de(respuesta)
        assert self._canjear(cliente, codigo, verifier).status_code == 200
        assert self._canjear(cliente, codigo, verifier).status_code == 400

    def test_otro_cliente_no_puede_canjear_mi_codigo(self, cliente: TestClient) -> None:
        verifier, respuesta = autorizar(cliente)
        salida = self._canjear(cliente, codigo_de(respuesta), verifier, client_id="c-otro")
        assert salida.status_code == 400

    def test_un_codigo_inexistente_se_rechaza(self, cliente: TestClient) -> None:
        assert self._canjear(cliente, "no-existe", "x").status_code == 400

    def test_otros_grants_no_se_admiten(self, cliente: TestClient) -> None:
        salida = cliente.post(
            "/token", data={"grant_type": "password", "username": "a", "password": "b"}
        )
        assert salida.status_code == 400
        assert salida.json()["error"] == "unsupported_grant_type"

    def test_el_token_lleva_las_claims_que_importan(
        self, cliente: TestClient, par: ParDeLlaves
    ) -> None:
        verifier, respuesta = autorizar(cliente)
        acceso = self._canjear(cliente, codigo_de(respuesta), verifier).json()["access_token"]
        claims = jwt.decode(
            acceso, par.publica, algorithms=["RS256"], audience=AUDIENCIA, issuer=ISSUER
        )
        assert claims["aud"] == AUDIENCIA
        assert claims["iss"] == ISSUER
        assert claims["scope"] == "read write"
        assert claims["sub"]
        assert claims["exp"] > claims["iat"]


class TestPkce:
    def test_el_verificador_acepta_el_par_correcto(self) -> None:
        verifier, challenge = pkce()
        assert verificar_pkce(verifier, challenge)

    def test_rechaza_cualquier_otro_verifier(self) -> None:
        _, challenge = pkce()
        assert not verificar_pkce("otro-verifier", challenge)


class TestRegistroDinamico:
    def test_registra_un_cliente_nuevo(self, cliente: TestClient) -> None:
        salida = cliente.post(
            "/register",
            json={"redirect_uris": ["http://localhost:9999/cb"], "client_name": "prueba"},
        )
        assert salida.status_code == 201
        assert salida.json()["token_endpoint_auth_method"] == "none"

    def test_exige_redirect_uris(self, cliente: TestClient) -> None:
        assert cliente.post("/register", json={"client_name": "x"}).status_code == 400

    def test_un_cuerpo_no_json_se_rechaza(self, cliente: TestClient) -> None:
        assert cliente.post("/register", content=b"no-json").status_code == 400


# --------------------------------------------------------------------------- #
# Resource-server verification
# --------------------------------------------------------------------------- #


class TestVerificadorDelResourceServer:
    @pytest.fixture
    def verificador(self, par: ParDeLlaves) -> VerificadorJWT:
        return VerificadorJWT(
            issuer=ISSUER,
            audience=AUDIENCIA,
            jwks_uri=f"{ISSUER}/jwks.json",
            obtener_jwks=lambda _token: par.publica,
        )

    async def test_acepta_un_token_legitimo(
        self, verificador: VerificadorJWT, as_: AuthorizationServer
    ) -> None:
        token = as_.emitir_token("dra@clinica.test", ["read", "write"])
        acceso = await verificador.verify_token(token)
        assert acceso is not None
        assert acceso.subject == "dra@clinica.test"
        assert set(acceso.scopes) == {"read", "write"}

    async def test_rechaza_un_token_expirado(
        self, verificador: VerificadorJWT, as_: AuthorizationServer
    ) -> None:
        token = as_.emitir_token("x", ["read"], ttl=1, ahora=time.time() - 3600)
        assert await verificador.verify_token(token) is None

    async def test_rechaza_un_token_de_otro_emisor(
        self, verificador: VerificadorJWT, par: ParDeLlaves
    ) -> None:
        ajeno = AuthorizationServer(
            issuer="http://otro-as.test", audience=AUDIENCIA, scopes=["read"], par=par
        )
        assert await verificador.verify_token(ajeno.emitir_token("x", ["read"])) is None

    async def test_rechaza_un_token_para_otra_audiencia(
        self, verificador: VerificadorJWT, as_: AuthorizationServer
    ) -> None:
        """The confused-deputy defence. Same issuer, same signature, different
        resource server, and it must not be replayable here."""
        token = as_.emitir_token("x", ["read"], audiencia="http://otro-servicio.test")
        assert await verificador.verify_token(token) is None

    async def test_rechaza_un_token_firmado_con_otra_llave(
        self, verificador: VerificadorJWT
    ) -> None:
        otro = AuthorizationServer(
            issuer=ISSUER, audience=AUDIENCIA, scopes=["read"], par=generar()
        )
        assert await verificador.verify_token(otro.emitir_token("x", ["read"])) is None

    async def test_rechaza_un_token_sin_firma_alg_none(self, verificador: VerificadorJWT) -> None:
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
        self, verificador: VerificadorJWT, basura: str
    ) -> None:
        assert await verificador.verify_token(basura) is None

    async def test_un_token_sin_scope_no_trae_permisos(
        self, verificador: VerificadorJWT, as_: AuthorizationServer
    ) -> None:
        acceso = await verificador.verify_token(as_.emitir_token("x", []))
        assert acceso is not None
        assert acceso.scopes == []


class TestLlaves:
    def test_la_llave_efimera_se_marca_como_tal(self, par: ParDeLlaves) -> None:
        assert par.efimera is True

    def test_el_jwk_no_contiene_material_privado(self, par: ParDeLlaves) -> None:
        jwk = par.jwk_publica()
        assert "d" not in jwk and "p" not in jwk and "q" not in jwk

    def test_la_llave_es_de_al_menos_2048_bits(self, par: ParDeLlaves) -> None:
        assert par.privada.key_size >= 2048


class TestPaginaDeInicio:
    def test_hay_una_pagina_que_orienta(self, cliente: TestClient) -> None:
        respuesta = cliente.get("/")
        assert respuesta.status_code == 200
        assert "oauth-authorization-server" in respuesta.text


class TestFabrica:
    def test_crear_as_toma_los_scopes_del_servidor_mcp(self) -> None:
        """One definition of the scope list, not three."""
        from backend.config import Settings
        from mcp_server.oauth.servidor import crear_as
        from mcp_server.server import SCOPES_SOPORTADOS

        ajustes = Settings(  # type: ignore[call-arg]
            _env_file=None,
            oauth_issuer="http://as.local:9000",
            oauth_audience="http://mcp.local:8080",
        )
        servidor = crear_as(ajustes)
        assert servidor.scopes == SCOPES_SOPORTADOS
        assert servidor.issuer == "http://as.local:9000"
        assert servidor.audience == "http://mcp.local:8080"
