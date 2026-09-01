"""Security layers 1 and 2: identity and least privilege.

**Layer 1, OAuth 2.1 + PKCE.** This is a resource server. It issues nothing and
verifies tokens against the authorization server's JWKS. There is no API key in
this project and no config file that could hold one. An unauthenticated call
gets a 401 with `WWW-Authenticate` pointing at
`/.well-known/oauth-protected-resource`, which is how a compliant client finds
where to authenticate.

**Layer 2, per-tool scopes.** Three scopes, one per tool:

* ``read`` for lookups, no side effects.
* ``write`` for anything that mutates the agenda or the ledger.
* ``clinical`` for the single tool that touches clinical data (Res. 2654/2019).

They do not nest. A `write` token cannot read a reason for consultation and a
`clinical` token cannot cancel an appointment, because administrative and
clinical are different kinds of authority, not different amounts of it. That is
the boundary in §2.5, and it is why the SaaStr incident, an agent holding write
permissions it never needed, could not happen here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import jwt
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier

from mcp_server.errors import StructuredToolError, scope_error, unauthenticated_error


class Scope(StrEnum):
    READ = "read"
    WRITE = "write"
    CLINICAL = "clinical"


#: Identity used when authentication is disabled, which happens only in local
#: development and in the contract suite.
ANONYMOUS_ACTOR = "anonimo@local"


@dataclass(frozen=True, slots=True)
class Identity:
    """Who is calling, distilled from the access token."""

    subject: str
    scopes: frozenset[str]
    client: str | None = None

    def has(self, scope: Scope) -> bool:
        return str(scope) in self.scopes


#: Carries every scope on purpose. With authentication off there is no security
#: story to tell, and pretending otherwise would make the gate tests lie.
OPEN_IDENTITY = Identity(
    subject=ANONYMOUS_ACTOR, scopes=frozenset(str(s) for s in Scope), client="dev"
)


class JWTVerifier(TokenVerifier):
    """Validates RS256 access tokens against the authorization server's JWKS.

    Four checks, all of them load-bearing:

    * **signature**, against the public key fetched from JWKS;
    * **expiry**, because a stale token is not a valid token;
    * **issuer**, because a correctly-signed token from the wrong AS is wrong;
    * **audience**, the confused-deputy defence. A token minted for another
      resource server must not be replayable here, same AS or not.
    """

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_uri: str,
        obtener_jwks: Any | None = None,
    ) -> None:
        self.issuer = issuer
        self.audience = audience
        self.jwks_uri = jwks_uri
        self._obtener_jwks = obtener_jwks
        self._cliente_jwks: jwt.PyJWKClient | None = None

    def _jwks(self) -> jwt.PyJWKClient:
        if self._cliente_jwks is None:
            self._cliente_jwks = jwt.PyJWKClient(self.jwks_uri, cache_keys=True)
        return self._cliente_jwks

    def _key(self, token: str) -> Any:
        if self._obtener_jwks is not None:
            return self._obtener_jwks(token)
        return self._jwks().get_signing_key_from_jwt(token).key

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = jwt.decode(
                token,
                self._key(token),
                algorithms=["RS256"],
                issuer=self.issuer,
                audience=self.audience,
                options={"require": ["exp", "iat", "sub", "aud", "iss"]},
            )
        except Exception:
            # Opaque on purpose. Telling an unauthenticated caller whether the
            # token expired or the signature failed is free reconnaissance.
            return None

        alcance = claims.get("scope", "")
        scopes = alcance.split() if isinstance(alcance, str) else list(alcance)
        return AccessToken(
            token=token,
            client_id=str(claims.get("client_id", claims["sub"])),
            scopes=scopes,
            expires_at=int(claims["exp"]),
            resource=self.audience,
            subject=str(claims["sub"]),
            claims=claims,
        )


def current_identity(*, exigir_auth: bool) -> Identity:
    """The caller's identity for this request.

    With authentication disabled the open identity is returned, so local work
    and the contract suite run without an authorization server. With it enabled,
    no token means no identity. Never a permissive default.
    """
    token = get_access_token()
    if token is None:
        if exigir_auth:
            raise unauthenticated_error("")
        return OPEN_IDENTITY
    return Identity(
        subject=token.subject or token.client_id,
        scopes=frozenset(token.scopes),
        client=token.client_id,
    )


def require_scope(identity: Identity, required: Scope, *, tool_name: str) -> Identity:
    """Least privilege, enforced. Raises with an actionable message."""
    if not identity.has(required):
        raise scope_error(tool_name, str(required), sorted(identity.scopes))
    return identity


def scopes_from(claims: dict[str, Any]) -> frozenset[str]:
    alcance = claims.get("scope", "")
    if isinstance(alcance, str):
        return frozenset(alcance.split())
    return frozenset(alcance)


def validate_requested_scopes(solicitados: Sequence[str]) -> list[str]:
    """Reject an undefined scope instead of dropping it silently. A client that
    asks for `admin` should learn it does not exist."""
    conocidos = {str(s) for s in Scope}
    desconocidos = [s for s in solicitados if s not in conocidos]
    if desconocidos:
        raise StructuredToolError(
            "SCOPE_DESCONOCIDO",
            f"Scopes no reconocidos: {', '.join(desconocidos)}.",
            suggestion=f"Los scopes válidos son: {', '.join(sorted(conocidos))}.",
            details={"desconocidos": desconocidos},
        )
    return list(solicitados)
