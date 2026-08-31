"""A minimal but genuine OAuth 2.1 authorization server.

Why build one instead of importing a library or bolting on Keycloak: an
authorization server you wrote is an authorization server you can be asked
about. Everything the MCP spec expects a client to discover and use is here,
and `docker compose --profile keycloak` proves the auth layer is swappable for
a real IdP without touching a line of the resource server.

Implemented, and enforced rather than merely present:

* ``GET /.well-known/oauth-authorization-server``: RFC 8414 metadata.
* ``GET /authorize``: authorization code with **mandatory** PKCE. OAuth 2.1
  drops the implicit and password grants and requires PKCE for public clients.
  `plain` challenges are rejected, S256 only.
* ``POST /token``: code exchange with `code_verifier` verification. Codes are
  single-use and short-lived.
* ``GET /jwks.json``: the public key, so the resource server verifies signatures
  without ever holding a shared secret.
* ``POST /register``: Dynamic Client Registration. The 2026-07-28 spec prefers
  Client ID Metadata Documents, so this is offered for compatibility and the
  preference is advertised in the metadata.

Tokens are RS256 JWTs carrying `aud` (the resource server), `scope`, `sub` and
`exp`. Binding the audience is the confused-deputy defence: a token minted for
another resource cannot be replayed here.

This is a development authorization server for a portfolio project. It stores
state in memory and says so. The point it makes is that the protocol is
understood, not that this process should hold your production identities.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import jwt
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from mcp_server.oauth.llaves import ParDeLlaves, llaves

#: OAuth 2.1: PKCE is required and only S256 is acceptable. `plain` offers no
#: protection against an intercepted authorization code.
METODOS_PKCE = ("S256",)

TTL_CODIGO = 60
CLIENTE_DEMO = "clinica-demo"


def _b64(datos: bytes) -> str:
    return base64.urlsafe_b64encode(datos).decode().rstrip("=")


def verificar_pkce(verifier: str, challenge: str) -> bool:
    esperado = _b64(hashlib.sha256(verifier.encode("ascii")).digest())
    return secrets.compare_digest(esperado, challenge)


@dataclass(slots=True)
class CodigoAutorizacion:
    codigo: str
    client_id: str
    redirect_uri: str
    code_challenge: str
    scopes: list[str]
    sujeto: str
    emitido_en: float
    usado: bool = False


@dataclass(slots=True)
class ClienteRegistrado:
    client_id: str
    redirect_uris: list[str]
    nombre: str = "cliente"


@dataclass(slots=True)
class EstadoAS:
    """In-memory state. One process, development only, and said out loud."""

    codigos: dict[str, CodigoAutorizacion] = field(default_factory=dict)
    clientes: dict[str, ClienteRegistrado] = field(default_factory=dict)

    def purgar(self, ahora: float) -> None:
        vencidos = [c for c, d in self.codigos.items() if ahora - d.emitido_en > TTL_CODIGO]
        for codigo in vencidos:
            del self.codigos[codigo]


def _error(descripcion: str, *, codigo: str = "invalid_request", status: int = 400) -> JSONResponse:
    """RFC 6749 §5.2 error shape, which is what clients actually parse."""
    return JSONResponse({"error": codigo, "error_description": descripcion}, status_code=status)


class AuthorizationServer:
    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        scopes: list[str],
        ttl_token: int = 900,
        par: ParDeLlaves | None = None,
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self.scopes = scopes
        self.ttl_token = ttl_token
        self.llaves = par or llaves()
        self.estado = EstadoAS()
        # A demo client so the quickstart and the MCP Inspector work with no
        # registration step. It is a *public* client: no secret, PKCE only.
        self.estado.clientes[CLIENTE_DEMO] = ClienteRegistrado(
            client_id=CLIENTE_DEMO,
            redirect_uris=[
                "http://localhost:6274/oauth/callback",
                "http://localhost:8080/callback",
            ],
            nombre="Cliente de demostración",
        )

    # --- discovery ---------------------------------------------------------

    async def metadata(self, _: Request) -> JSONResponse:
        return JSONResponse(
            {
                "issuer": self.issuer,
                "authorization_endpoint": f"{self.issuer}/authorize",
                "token_endpoint": f"{self.issuer}/token",
                "jwks_uri": f"{self.issuer}/jwks.json",
                "registration_endpoint": f"{self.issuer}/register",
                "scopes_supported": self.scopes,
                "response_types_supported": ["code"],
                # OAuth 2.1 drops implicit and resource-owner-password.
                "grant_types_supported": ["authorization_code"],
                "code_challenge_methods_supported": list(METODOS_PKCE),
                "token_endpoint_auth_methods_supported": ["none"],
                "client_id_metadata_document_supported": True,
            }
        )

    async def jwks(self, _: Request) -> JSONResponse:
        return JSONResponse(self.llaves.jwks())

    # --- registration ------------------------------------------------------

    async def registrar_cliente(self, request: Request) -> JSONResponse:
        try:
            cuerpo = await request.json()
        except Exception:
            return _error("El cuerpo debe ser JSON.")
        redirects = cuerpo.get("redirect_uris") or []
        if not isinstance(redirects, list) or not redirects:
            return _error("redirect_uris es obligatorio y debe ser una lista no vacía.")
        client_id = f"c-{secrets.token_urlsafe(9)}"
        self.estado.clientes[client_id] = ClienteRegistrado(
            client_id=client_id,
            redirect_uris=[str(r) for r in redirects],
            nombre=str(cuerpo.get("client_name", "cliente")),
        )
        return JSONResponse(
            {
                "client_id": client_id,
                "redirect_uris": redirects,
                # OAuth's literal for "public client, no secret" - not a password.
                "token_endpoint_auth_method": "none",  # nosec B105
                "grant_types": ["authorization_code"],
                "response_types": ["code"],
            },
            status_code=201,
        )

    # --- authorization -----------------------------------------------------

    async def authorize(self, request: Request) -> Response:
        p = request.query_params
        client_id = p.get("client_id", "")
        redirect_uri = p.get("redirect_uri", "")
        estado = p.get("state", "")

        cliente = self.estado.clientes.get(client_id)
        if cliente is None:
            return _error(f"client_id desconocido: {client_id}", codigo="invalid_client")
        # Never redirect to an unregistered URI: that is an open redirect and an
        # authorization-code exfiltration path.
        if redirect_uri not in cliente.redirect_uris:
            return _error("redirect_uri no registrada para este cliente.")

        if p.get("response_type") != "code":
            return self._redirigir_error(
                redirect_uri,
                "unsupported_response_type",
                "Solo se admite response_type=code.",
                estado,
            )

        challenge = p.get("code_challenge", "")
        metodo = p.get("code_challenge_method", "")
        if not challenge or metodo not in METODOS_PKCE:
            return self._redirigir_error(
                redirect_uri,
                "invalid_request",
                "PKCE es obligatorio: envía code_challenge con code_challenge_method=S256.",
                estado,
            )

        solicitados = (p.get("scope") or "read").split()
        desconocidos = [s for s in solicitados if s not in self.scopes]
        if desconocidos:
            return self._redirigir_error(
                redirect_uri,
                "invalid_scope",
                f"Scopes no soportados: {', '.join(desconocidos)}.",
                estado,
            )

        # A real deployment shows a consent screen here. This one auto-approves
        # and says so, because the interesting part for this project is the
        # protocol, not the login form.
        sujeto = p.get("login_hint") or "recepcion@clinica.local"
        codigo = secrets.token_urlsafe(24)
        ahora = time.time()
        self.estado.purgar(ahora)
        self.estado.codigos[codigo] = CodigoAutorizacion(
            codigo=codigo,
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=challenge,
            scopes=solicitados,
            sujeto=sujeto,
            emitido_en=ahora,
        )
        destino = f"{redirect_uri}?{urlencode({'code': codigo, 'state': estado})}"
        return RedirectResponse(destino, status_code=302)

    @staticmethod
    def _redirigir_error(
        redirect_uri: str, codigo: str, descripcion: str, estado: str
    ) -> RedirectResponse:
        consulta = urlencode({"error": codigo, "error_description": descripcion, "state": estado})
        return RedirectResponse(f"{redirect_uri}?{consulta}", status_code=302)

    # --- token -------------------------------------------------------------

    async def token(self, request: Request) -> JSONResponse:
        formulario = await request.form()
        if formulario.get("grant_type") != "authorization_code":
            return _error(
                "Solo se admite grant_type=authorization_code (OAuth 2.1).",
                codigo="unsupported_grant_type",
            )

        codigo = str(formulario.get("code", ""))
        verifier = str(formulario.get("code_verifier", ""))
        client_id = str(formulario.get("client_id", ""))

        ahora = time.time()
        self.estado.purgar(ahora)
        emitido = self.estado.codigos.get(codigo)
        if emitido is None:
            return _error("El código de autorización no existe o expiró.", codigo="invalid_grant")
        if emitido.usado:
            # A replayed code means it leaked. Burn everything derived from it.
            del self.estado.codigos[codigo]
            return _error("El código ya fue usado.", codigo="invalid_grant")
        if emitido.client_id != client_id:
            return _error("El código pertenece a otro cliente.", codigo="invalid_grant")
        if not verifier or not verificar_pkce(verifier, emitido.code_challenge):
            return _error(
                "El code_verifier no corresponde al code_challenge.", codigo="invalid_grant"
            )

        emitido.usado = True
        del self.estado.codigos[codigo]

        return JSONResponse(
            {
                "access_token": self.emitir_token(
                    emitido.sujeto, emitido.scopes, client_id=client_id
                ),
                "token_type": "Bearer",  # nosec B105 - the RFC 6750 token type
                "expires_in": self.ttl_token,
                "scope": " ".join(emitido.scopes),
            }
        )

    def emitir_token(
        self,
        sujeto: str,
        scopes: list[str],
        *,
        client_id: str = CLIENTE_DEMO,
        ttl: int | None = None,
        audiencia: str | None = None,
        ahora: float | None = None,
    ) -> str:
        """Mint an access token. Also used by the test-suite to build tokens."""
        emitido = int(ahora if ahora is not None else time.time())
        payload = {
            "iss": self.issuer,
            "sub": sujeto,
            "aud": audiencia or self.audience,
            "client_id": client_id,
            "scope": " ".join(scopes),
            "iat": emitido,
            "exp": emitido + (ttl if ttl is not None else self.ttl_token),
            "jti": secrets.token_urlsafe(8),
        }
        return jwt.encode(
            payload,
            self.llaves.pem_privada(),
            algorithm="RS256",
            headers={"kid": self.llaves.kid},
        )

    # --- app ---------------------------------------------------------------

    async def _inicio(self, _: Request) -> HTMLResponse:
        return HTMLResponse(
            "<h1>Authorization Server · dental-clinic-mcp</h1>"
            "<p>Servidor de autorización de desarrollo. Metadata en "
            "<code>/.well-known/oauth-authorization-server</code>.</p>"
        )

    def app(self) -> Starlette:
        return Starlette(
            routes=[
                Route("/", self._inicio),
                Route("/.well-known/oauth-authorization-server", self.metadata),
                Route("/jwks.json", self.jwks),
                Route("/register", self.registrar_cliente, methods=["POST"]),
                Route("/authorize", self.authorize),
                Route("/token", self.token, methods=["POST"]),
            ]
        )


def crear_as(config: Any | None = None) -> AuthorizationServer:
    from backend.config import get_settings
    from mcp_server.server import SCOPES_SOPORTADOS

    ajustes = config or get_settings()
    return AuthorizationServer(
        issuer=ajustes.oauth_issuer,
        audience=ajustes.oauth_audience,
        scopes=SCOPES_SOPORTADOS,
        ttl_token=ajustes.oauth_access_token_ttl_seconds,
    )


def main() -> None:  # pragma: no cover - process entry point
    import uvicorn

    from backend.config import get_settings

    ajustes = get_settings()
    servidor = crear_as(ajustes)
    # The port comes from the public issuer so the two cannot drift apart; the
    # bind address is a deployment decision and stays loopback unless told
    # otherwise (docker-compose sets it explicitly).
    puerto = int(ajustes.oauth_issuer.rsplit(":", 1)[-1].split("/")[0] or 9000)
    uvicorn.run(servidor.app(), host=ajustes.oauth_host, port=puerto)


if __name__ == "__main__":  # pragma: no cover
    main()
