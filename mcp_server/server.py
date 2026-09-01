"""Assembly of the MCP server.

Thirteen tools, three resources, one prompt. Thirteen is a deliberate ceiling:
model accuracy degrades past roughly 25-30 tools, so a smaller, precisely
described catalogue beats a larger one: the ten good tools get picked
correctly, the thirty mediocre ones do not.

Transport is Streamable HTTP, stateless, on the 2026-07-28 spec. Human approval
rides Multi Round-Trip Requests: a tool that needs a person's answer returns
`input_required`, and the client retries the same call carrying the answer and a
sealed `requestState`. A stateful application does not require a stateful
transport, and this one keeps no session at all.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer, RequestStateSecurity
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from backend.config import Settings, get_settings
from mcp_server import resources
from mcp_server.audit import Auditor, configurar_logging
from mcp_server.auth import Scope, VerificadorJWT
from mcp_server.client import ClienteBackend
from mcp_server.context import Contexto
from mcp_server.rate_limit import LimitadorDePeticiones
from mcp_server.tools import clinical, read, write

REPOSITORIO = "https://github.com/dylanbc1/dental-clinic-mcp-server"

RUTA_METADATA_RECURSO = "/.well-known/oauth-protected-resource"
NOMBRE_METADATA = "metadata_recurso_corregida"

INSTRUCCIONES = """\
MCP server for a dental clinic in Colombia. It exposes the real front-desk
operation: the agenda, afiliación checks, and the cartera.

Two rules govern everything else:

1. Read tools answer directly. Write tools do not execute on the first attempt:
   they return a request for confirmación describing what is about to happen. Your
   client shows that to a person and retries the same call carrying their answer.
   Until then, no data has been modified.
2. Booking is administrative; recording the reason for consultation is clinical
   data, and needs the 'clinical' permission plus the patient's informed consent.

Start with the 'recepcionista_odontologia' prompt and the 'clinica://info'
resource. The prompt is in Spanish on purpose: it is how the agent should speak
to a Colombian patient.
"""


def crear_contexto(
    ajustes: Settings | None = None,
    *,
    cliente: ClienteBackend | None = None,
    exigir_auth: bool | None = None,
) -> Contexto:
    config = ajustes or get_settings()
    return Contexto(
        cliente=cliente or ClienteBackend(config.backend_base_url),
        auditor=Auditor(),
        # One switch drives both the HTTP middleware and the per-tool identity
        # check, and it defaults to on. Deriving it from the environment name
        # would mean a misconfigured APP_ENV silently disables authentication.
        exigir_auth=config.mcp_auth_enabled if exigir_auth is None else exigir_auth,
    )


def construir_auth(config: Settings) -> tuple[AuthSettings, VerificadorJWT]:
    """Resource-server side of OAuth 2.1.

    `required_scopes` is left empty on purpose: a blanket requirement would make
    every tool need every scope, which is the opposite of least privilege. Each
    tool checks its own.
    """
    ajustes = AuthSettings(
        issuer_url=AnyHttpUrl(config.oauth_issuer),
        resource_server_url=AnyHttpUrl(config.mcp_public_url),
        required_scopes=[],
    )
    verificador = VerificadorJWT(
        issuer=config.oauth_issuer,
        audience=config.oauth_audience,
        jwks_uri=config.jwks_url,
    )
    return ajustes, verificador


def crear_servidor(
    ctx: Contexto,
    *,
    config: Settings | None = None,
    con_auth: bool = True,
) -> MCPServer[Any]:
    ajustes = config or get_settings()
    auth_settings: AuthSettings | None = None
    verificador: VerificadorJWT | None = None
    if con_auth:
        auth_settings, verificador = construir_auth(ajustes)

    servidor: MCPServer[Any] = MCPServer(
        name="clinica-odontologica",
        title="Clínica Odontológica · MCP",
        version="0.1.0",
        instructions=INSTRUCCIONES,
        auth=auth_settings,
        token_verifier=verificador,
        website_url=REPOSITORIO,
        # Seals the paused operation a client carries back with the human's
        # answer. AES-256-GCM, bound to the request, the audience and the
        # authenticated principal, so an approval cannot be moved onto another
        # operation or another user. `keys[0]` seals, every key unseals, which
        # is what makes rotation zero-downtime.
        request_state_security=RequestStateSecurity(
            keys=list(ajustes.request_state_keys),
            ttl=ajustes.request_state_ttl_seconds,
        ),
    )

    if auth_settings is not None:
        _publicar_metadata_del_recurso(servidor, ajustes)

    read.registrar(servidor, ctx)
    write.registrar(servidor, ctx)
    clinical.registrar(servidor, ctx)
    resources.registrar(servidor, ctx)
    return servidor


def _publicar_metadata_del_recurso(servidor: MCPServer[Any], config: Settings) -> None:
    """Serve RFC 9728 metadata that actually names the scopes.

    The SDK derives `scopes_supported` from `required_scopes`, and that has to
    stay empty: a blanket requirement would make every tool need every scope,
    which is the opposite of least privilege. The result is a discovery document
    advertising an empty scope list, telling a client that no scopes are used
    here. That is false and unhelpful, so this route answers with the truth:
    three scopes exist, none of them is globally required, and which one a call
    needs depends on the tool.

    The SDK registers its own route for this path first, so `_preferir_nuestra_metadata`
    drops it once the app is built and leaves exactly one handler here.
    """
    documento = {
        "resource": config.mcp_public_url,
        "authorization_servers": [config.oauth_issuer],
        "scopes_supported": SCOPES_SOPORTADOS,
        "bearer_methods_supported": ["header"],
        "resource_name": "Clínica Odontológica · MCP",
        # The repository, not a path on this server: /docs does not exist here.
        "resource_documentation": REPOSITORIO,
    }

    @servidor.custom_route(  # type: ignore[untyped-decorator]
        RUTA_METADATA_RECURSO, methods=["GET"], include_in_schema=False, name=NOMBRE_METADATA
    )
    async def metadata_recurso(_: Request) -> JSONResponse:
        return JSONResponse(documento)


def _preferir_nuestra_metadata(app: Starlette) -> None:
    """Leave exactly one handler on the protected-resource path: ours.

    Starlette matches the first route that fits, and the SDK's was registered
    first, so without this the corrected document is unreachable.
    """
    nuestras = [
        r
        for r in app.routes
        if getattr(r, "path", None) == RUTA_METADATA_RECURSO
        and getattr(r, "name", None) == NOMBRE_METADATA
    ]
    if not nuestras:
        return
    resto = [r for r in app.routes if getattr(r, "path", None) != RUTA_METADATA_RECURSO]
    app.router.routes[:] = nuestras + resto


def construir_app(
    ctx: Contexto, *, config: Settings | None = None, con_auth: bool = True
) -> Starlette:
    """ASGI app with the transport guards of layer 5 switched on.

    DNS-rebinding protection matters more than it looks: without Host and Origin
    validation, a page the user visits in a browser can reach a server bound to
    localhost and drive it with the user's own credentials.
    """
    ajustes = config or get_settings()
    servidor = crear_servidor(ctx, config=ajustes, con_auth=con_auth)
    app = servidor.streamable_http_app(
        streamable_http_path="/mcp",
        # Stateless on purpose. A stateful application does not require a
        # stateful transport, and this one carries everything it needs in the
        # request: the OAuth token identifies the caller, and the approval token
        # carries the pending operation. Keeping a session would mean building
        # infrastructure to preserve a conversation the application never asked
        # for, and it pins every client to one replica.
        stateless_http=ajustes.mcp_stateless,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=ajustes.mcp_allowed_hosts,
            allowed_origins=ajustes.mcp_allowed_origins,
        ),
    )
    if con_auth:
        _preferir_nuestra_metadata(app)
    app.add_middleware(
        LimitadorDePeticiones,
        limite=ajustes.mcp_rate_limite,
        ventana_segundos=ajustes.mcp_rate_ventana_segundos,
    )
    return app


@contextmanager
def contexto_gestionado(**kwargs: Any) -> Iterator[Contexto]:
    ctx = crear_contexto(**kwargs)
    try:
        yield ctx
    finally:  # pragma: no cover - process teardown
        pass


#: Scopes this server understands, exported for the authorization server and the
#: documentation so there is one definition rather than three.
SCOPES_SOPORTADOS: list[str] = [str(s) for s in Scope]


def main() -> None:  # pragma: no cover - process entry point
    import uvicorn

    config = get_settings()
    configurar_logging(config.log_level)
    ctx = crear_contexto(config)
    uvicorn.run(
        construir_app(ctx, config=config, con_auth=config.mcp_auth_enabled),
        host=config.mcp_host,
        port=config.mcp_port,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
