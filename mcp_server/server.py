"""Assembly of the MCP server.

Fourteen tools, three resources, one prompt. Fourteen is a deliberate ceiling:
model accuracy degrades past roughly 25-30 tools, so a smaller, precisely
described catalogue beats a larger one: the ten good tools get picked
correctly, the thirty mediocre ones do not.

Transport is Streamable HTTP. SSE is deprecated for production and is not
offered here.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.applications import Starlette

from backend.config import Settings, get_settings
from mcp_server import recursos
from mcp_server.aprobacion import GestorDeAprobaciones
from mcp_server.auditoria import Auditor, configurar_logging
from mcp_server.auth import Scope, VerificadorJWT
from mcp_server.cliente import ClienteBackend
from mcp_server.contexto import Contexto
from mcp_server.limites import LimitadorDePeticiones
from mcp_server.tools import clinical, confirmacion, read, write

INSTRUCCIONES = """\
Servidor MCP de una clínica odontológica en Colombia. Expone la operación real de
recepción: agenda, validación de afiliación y cartera.

Dos reglas gobiernan todo lo demás:

1. Las herramientas de lectura responden directo. Las de escritura NO ejecutan nada:
   devuelven una propuesta con un token que una persona debe aprobar, y solo entonces
   confirmar_operacion la ejecuta.
2. Agendar es administrativo; registrar el motivo de consulta es dato clínico y exige
   el permiso 'clinical' más consentimiento informado del paciente.

Empieza por el prompt 'recepcionista_odontologia' y por el recurso 'clinica://info'.
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
        aprobaciones=GestorDeAprobaciones(
            config.approval_signing_key, ttl_segundos=config.approval_ttl_seconds
        ),
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
        website_url="https://github.com/dylanbc1/dental-clinic-mcp-server",
    )

    read.registrar(servidor, ctx)
    write.registrar(servidor, ctx)
    clinical.registrar(servidor, ctx)
    confirmacion.registrar(servidor, ctx)
    recursos.registrar(servidor, ctx)
    return servidor


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
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=ajustes.mcp_allowed_hosts,
            allowed_origins=ajustes.mcp_allowed_origins,
        ),
    )
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
