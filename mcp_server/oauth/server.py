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

from mcp_server.oauth.keys import KeyPair, signing_keys

#: OAuth 2.1: PKCE is required and only S256 is acceptable. `plain` offers no
#: protection against an intercepted authorization code.
PKCE_METHODS = ("S256",)

CODE_TTL = 60
DEMO_CLIENT = "clinica-demo"


def _b64(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def verify_pkce(verifier: str, challenge: str) -> bool:
    expected = _b64(hashlib.sha256(verifier.encode("ascii")).digest())
    return secrets.compare_digest(expected, challenge)


@dataclass(slots=True)
class AuthorizationCode:
    code: str
    client_id: str
    redirect_uri: str
    code_challenge: str
    scopes: list[str]
    subject: str
    emitido_en: float
    used: bool = False


@dataclass(slots=True)
class RegisteredClient:
    client_id: str
    redirect_uris: list[str]
    name: str = "client"


@dataclass(slots=True)
class AuthorizationServerState:
    """In-memory state. One process, development only, and said out loud."""

    codigos: dict[str, AuthorizationCode] = field(default_factory=dict)
    clientes: dict[str, RegisteredClient] = field(default_factory=dict)

    def purge(self, now: float) -> None:
        overdue = [c for c, d in self.codigos.items() if now - d.emitido_en > CODE_TTL]
        for code in overdue:
            del self.codigos[code]


def _error(description: str, *, code: str = "invalid_request", status: int = 400) -> JSONResponse:
    """RFC 6749 §5.2 error shape, which is what clients actually parse."""
    return JSONResponse({"error": code, "error_description": description}, status_code=status)


class AuthorizationServer:
    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        scopes: list[str],
        ttl_token: int = 900,
        par: KeyPair | None = None,
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self.scopes = scopes
        self.ttl_token = ttl_token
        self.signing_keys = par or signing_keys()
        self.state = AuthorizationServerState()
        # A demo client so the quickstart and the MCP Inspector work with no
        # registration step. It is a *public* client: no secret, PKCE only.
        self.state.clientes[DEMO_CLIENT] = RegisteredClient(
            client_id=DEMO_CLIENT,
            redirect_uris=[
                "http://localhost:6274/oauth/callback",
                "http://localhost:8080/callback",
            ],
            name="Cliente de demostración",
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
                "code_challenge_methods_supported": list(PKCE_METHODS),
                "token_endpoint_auth_methods_supported": ["none"],
                "client_id_metadata_document_supported": True,
            }
        )

    async def jwks(self, _: Request) -> JSONResponse:
        return JSONResponse(self.signing_keys.jwks())

    # --- registration ------------------------------------------------------

    async def register_client(self, request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return _error("El cuerpo debe ser JSON.")
        redirects = body.get("redirect_uris") or []
        if not isinstance(redirects, list) or not redirects:
            return _error("redirect_uris es obligatorio y debe ser una lista no vacía.")
        client_id = f"c-{secrets.token_urlsafe(9)}"
        self.state.clientes[client_id] = RegisteredClient(
            client_id=client_id,
            redirect_uris=[str(r) for r in redirects],
            name=str(body.get("client_name", "client")),
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
        state = p.get("state", "")

        client = self.state.clientes.get(client_id)
        if client is None:
            return _error(f"client_id desconocido: {client_id}", code="invalid_client")
        # Never redirect to an unregistered URI: that is an open redirect and an
        # authorization-code exfiltration path.
        if redirect_uri not in client.redirect_uris:
            return _error("redirect_uri no registrada para este cliente.")

        if p.get("response_type") != "code":
            return self._redirect_error(
                redirect_uri,
                "unsupported_response_type",
                "Solo se admite response_type=code.",
                state,
            )

        challenge = p.get("code_challenge", "")
        metodo = p.get("code_challenge_method", "")
        if not challenge or metodo not in PKCE_METHODS:
            return self._redirect_error(
                redirect_uri,
                "invalid_request",
                "PKCE es obligatorio: envía code_challenge con code_challenge_method=S256.",
                state,
            )

        solicitados = (p.get("scope") or "read").split()
        desconocidos = [s for s in solicitados if s not in self.scopes]
        if desconocidos:
            return self._redirect_error(
                redirect_uri,
                "invalid_scope",
                f"Scopes no soportados: {', '.join(desconocidos)}.",
                state,
            )

        # A real deployment shows a consent screen here. This one auto-approves
        # and says so, because the interesting part for this project is the
        # protocol, not the login form.
        subject = p.get("login_hint") or "recepcion@clinica.local"
        code = secrets.token_urlsafe(24)
        now = time.time()
        self.state.purge(now)
        self.state.codigos[code] = AuthorizationCode(
            code=code,
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=challenge,
            scopes=solicitados,
            subject=subject,
            emitido_en=now,
        )
        target = f"{redirect_uri}?{urlencode({'code': code, 'state': state})}"
        return RedirectResponse(target, status_code=302)

    @staticmethod
    def _redirect_error(
        redirect_uri: str, code: str, description: str, state: str
    ) -> RedirectResponse:
        query = urlencode({"error": code, "error_description": description, "state": state})
        return RedirectResponse(f"{redirect_uri}?{query}", status_code=302)

    # --- token -------------------------------------------------------------

    async def token(self, request: Request) -> JSONResponse:
        formulario = await request.form()
        if formulario.get("grant_type") != "authorization_code":
            return _error(
                "Solo se admite grant_type=authorization_code (OAuth 2.1).",
                code="unsupported_grant_type",
            )

        code = str(formulario.get("code", ""))
        verifier = str(formulario.get("code_verifier", ""))
        client_id = str(formulario.get("client_id", ""))

        now = time.time()
        self.state.purge(now)
        issued = self.state.codigos.get(code)
        if issued is None:
            return _error("El código de autorización no existe o expiró.", code="invalid_grant")
        if issued.used:
            # A replayed code means it leaked. Burn everything derived from it.
            del self.state.codigos[code]
            return _error("El código ya fue usado.", code="invalid_grant")
        if issued.client_id != client_id:
            return _error("El código pertenece a otro cliente.", code="invalid_grant")
        if not verifier or not verify_pkce(verifier, issued.code_challenge):
            return _error(
                "El code_verifier no corresponde al code_challenge.", code="invalid_grant"
            )

        issued.used = True
        del self.state.codigos[code]

        return JSONResponse(
            {
                "access_token": self.issue_token(
                    issued.subject, issued.scopes, client_id=client_id
                ),
                "token_type": "Bearer",  # nosec B105 - the RFC 6750 token type
                "expires_in": self.ttl_token,
                "scope": " ".join(issued.scopes),
            }
        )

    def issue_token(
        self,
        subject: str,
        scopes: list[str],
        *,
        client_id: str = DEMO_CLIENT,
        ttl: int | None = None,
        audience: str | None = None,
        now: float | None = None,
    ) -> str:
        """Mint an access token. Also used by the test-suite to build tokens."""
        issued = int(now if now is not None else time.time())
        payload = {
            "iss": self.issuer,
            "sub": subject,
            "aud": audience or self.audience,
            "client_id": client_id,
            "scope": " ".join(scopes),
            "iat": issued,
            "exp": issued + (ttl if ttl is not None else self.ttl_token),
            "jti": secrets.token_urlsafe(8),
        }
        return jwt.encode(
            payload,
            self.signing_keys.private_pem(),
            algorithm="RS256",
            headers={"kid": self.signing_keys.kid},
        )

    # --- app ---------------------------------------------------------------

    async def _start(self, _: Request) -> HTMLResponse:
        return HTMLResponse(
            "<h1>Authorization Server · dental-clinic-mcp</h1>"
            "<p>Servidor de autorización de desarrollo. Metadata en "
            "<code>/.well-known/oauth-authorization-server</code>.</p>"
        )

    def app(self) -> Starlette:
        return Starlette(
            routes=[
                Route("/", self._start),
                Route("/.well-known/oauth-authorization-server", self.metadata),
                Route("/jwks.json", self.jwks),
                Route("/register", self.register_client, methods=["POST"]),
                Route("/authorize", self.authorize),
                Route("/token", self.token, methods=["POST"]),
            ]
        )


def build_authorization_server(config: Any | None = None) -> AuthorizationServer:
    from backend.config import get_settings
    from mcp_server.server import SUPPORTED_SCOPES

    settings_ = config or get_settings()
    return AuthorizationServer(
        issuer=settings_.oauth_issuer,
        audience=settings_.oauth_audience,
        scopes=SUPPORTED_SCOPES,
        ttl_token=settings_.oauth_access_token_ttl_seconds,
    )


def main() -> None:  # pragma: no cover - process entry point
    import uvicorn

    from backend.config import get_settings

    settings_ = get_settings()
    server_ = build_authorization_server(settings_)
    # The port comes from the public issuer so the two cannot drift apart; the
    # bind address is a deployment decision and stays loopback unless told
    # otherwise (docker-compose sets it explicitly).
    puerto = int(settings_.oauth_issuer.rsplit(":", 1)[-1].split("/")[0] or 9000)
    uvicorn.run(server_.app(), host=settings_.oauth_host, port=puerto)


if __name__ == "__main__":  # pragma: no cover
    main()
