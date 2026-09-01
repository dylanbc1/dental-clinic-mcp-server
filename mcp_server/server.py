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
from mcp_server.audit import Auditor, configure_logging
from mcp_server.auth import JWTVerifier, Scope
from mcp_server.client import BackendClient
from mcp_server.context import ToolContext
from mcp_server.rate_limit import RequestLimiter
from mcp_server.tools import clinical, read, write

REPOSITORY = "https://github.com/dylanbc1/dental-clinic-mcp-server"

RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource"
METADATA_ROUTE_NAME = "metadata_recurso_corregida"

INSTRUCTIONS = """\
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


def build_context(
    settings_: Settings | None = None,
    *,
    client: BackendClient | None = None,
    exigir_auth: bool | None = None,
) -> ToolContext:
    config = settings_ or get_settings()
    return ToolContext(
        client=client or BackendClient(config.backend_base_url),
        auditor=Auditor(),
        # One switch drives both the HTTP middleware and the per-tool identity
        # check, and it defaults to on. Deriving it from the environment name
        # would mean a misconfigured APP_ENV silently disables authentication.
        exigir_auth=config.mcp_auth_enabled if exigir_auth is None else exigir_auth,
    )


def build_auth(config: Settings) -> tuple[AuthSettings, JWTVerifier]:
    """Resource-server side of OAuth 2.1.

    `required_scopes` is left empty on purpose: a blanket requirement would make
    every tool need every scope, which is the opposite of least privilege. Each
    tool checks its own.
    """
    settings_ = AuthSettings(
        issuer_url=AnyHttpUrl(config.oauth_issuer),
        resource_server_url=AnyHttpUrl(config.mcp_public_url),
        required_scopes=[],
    )
    verificador = JWTVerifier(
        issuer=config.oauth_issuer,
        audience=config.oauth_audience,
        jwks_uri=config.jwks_url,
    )
    return settings_, verificador


def build_server(
    ctx: ToolContext,
    *,
    config: Settings | None = None,
    con_auth: bool = True,
) -> MCPServer[Any]:
    settings_ = config or get_settings()
    auth_settings: AuthSettings | None = None
    verificador: JWTVerifier | None = None
    if con_auth:
        auth_settings, verificador = build_auth(settings_)

    server_: MCPServer[Any] = MCPServer(
        name="clinica-odontologica",
        title="Clínica Odontológica · MCP",
        version="0.1.0",
        instructions=INSTRUCTIONS,
        auth=auth_settings,
        token_verifier=verificador,
        website_url=REPOSITORY,
        # Seals the paused operation a client carries back with the human's
        # answer. AES-256-GCM, bound to the request, the audience and the
        # authenticated principal, so an approval cannot be moved onto another
        # operation or another user. `keys[0]` seals, every key unseals, which
        # is what makes rotation zero-downtime.
        request_state_security=RequestStateSecurity(
            keys=list(settings_.request_state_keys),
            ttl=settings_.request_state_ttl_seconds,
        ),
    )

    if auth_settings is not None:
        _publish_resource_metadata(server_, settings_)

    read.register(server_, ctx)
    write.register(server_, ctx)
    clinical.register(server_, ctx)
    resources.register(server_, ctx)
    return server_


def _publish_resource_metadata(server_: MCPServer[Any], config: Settings) -> None:
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
    document_number = {
        "resource": config.mcp_public_url,
        "authorization_servers": [config.oauth_issuer],
        "scopes_supported": SUPPORTED_SCOPES,
        "bearer_methods_supported": ["header"],
        "resource_name": "Clínica Odontológica · MCP",
        # The repository, not a path on this server: /docs does not exist here.
        "resource_documentation": REPOSITORY,
    }

    @server_.custom_route(  # type: ignore[untyped-decorator]
        RESOURCE_METADATA_PATH, methods=["GET"], include_in_schema=False, name=METADATA_ROUTE_NAME
    )
    async def resource_metadata(_: Request) -> JSONResponse:
        return JSONResponse(document_number)


def _prefer_our_metadata(app: Starlette) -> None:
    """Leave exactly one handler on the protected-resource path: ours.

    Starlette matches the first route that fits, and the SDK's was registered
    first, so without this the corrected document is unreachable.
    """
    nuestras = [
        r
        for r in app.routes
        if getattr(r, "path", None) == RESOURCE_METADATA_PATH
        and getattr(r, "name", None) == METADATA_ROUTE_NAME
    ]
    if not nuestras:
        return
    resto = [r for r in app.routes if getattr(r, "path", None) != RESOURCE_METADATA_PATH]
    app.router.routes[:] = nuestras + resto


def build_app(
    ctx: ToolContext, *, config: Settings | None = None, con_auth: bool = True
) -> Starlette:
    """ASGI app with the transport guards of layer 5 switched on.

    DNS-rebinding protection matters more than it looks: without Host and Origin
    validation, a page the user visits in a browser can reach a server bound to
    localhost and drive it with the user's own credentials.
    """
    settings_ = config or get_settings()
    server_ = build_server(ctx, config=settings_, con_auth=con_auth)
    app = server_.streamable_http_app(
        streamable_http_path="/mcp",
        # Stateless on purpose. A stateful application does not require a
        # stateful transport, and this one carries everything it needs in the
        # request: the OAuth token identifies the caller, and the approval token
        # carries the pending operation. Keeping a session would mean building
        # infrastructure to preserve a conversation the application never asked
        # for, and it pins every client to one replica.
        stateless_http=settings_.mcp_stateless,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings_.mcp_allowed_hosts,
            allowed_origins=settings_.mcp_allowed_origins,
        ),
    )
    if con_auth:
        _prefer_our_metadata(app)
    app.add_middleware(
        RequestLimiter,
        limit=settings_.mcp_rate_limite,
        ventana_segundos=settings_.mcp_rate_ventana_segundos,
    )
    return app


@contextmanager
def managed_context(**kwargs: Any) -> Iterator[ToolContext]:
    ctx = build_context(**kwargs)
    try:
        yield ctx
    finally:  # pragma: no cover - process teardown
        pass


#: Scopes this server understands, exported for the authorization server and the
#: documentation so there is one definition rather than three.
SUPPORTED_SCOPES: list[str] = [str(s) for s in Scope]


def main() -> None:  # pragma: no cover - process entry point
    import uvicorn

    config = get_settings()
    configure_logging(config.log_level)
    ctx = build_context(config)
    uvicorn.run(
        build_app(ctx, config=config, con_auth=config.mcp_auth_enabled),
        host=config.mcp_host,
        port=config.mcp_port,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
