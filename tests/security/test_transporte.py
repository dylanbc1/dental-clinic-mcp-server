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
from mcp_server.aprobacion import GestorDeAprobaciones
from mcp_server.auditoria import Auditor
from mcp_server.cliente import ClienteBackend
from mcp_server.contexto import Contexto
from mcp_server.limites import LimitadorDePeticiones, VentanaDeslizante
from mcp_server.server import construir_app

pytestmark = pytest.mark.security

PUBLICA = "http://localhost:8080"


@pytest.fixture
def ajustes() -> Settings:
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
def ctx_sin_backend() -> Contexto:
    """The transport guards run before any tool, so the backend is never reached."""
    return Contexto(
        cliente=ClienteBackend("http://backend-inexistente"),
        aprobaciones=GestorDeAprobaciones("clave-de-pruebas"),
        auditor=Auditor(),
        exigir_auth=True,
    )


@pytest.fixture
def cliente(ctx_sin_backend: Contexto, ajustes: Settings) -> Iterator[TestClient]:
    """The production shape: authentication on."""
    app = construir_app(ctx_sin_backend, config=ajustes, con_auth=True)
    with TestClient(app, base_url="http://localhost:8080") as c:
        yield c


@pytest.fixture
def cliente_sin_auth(ctx_sin_backend: Contexto, ajustes: Settings) -> Iterator[TestClient]:
    """The local-development shape: no authorization server.

    This is the configuration in which DNS rebinding is exploitable: a page in
    the user's browser resolving to 127.0.0.1 and driving a server that trusts
    whoever reaches it. So the Host/Origin guard is asserted here.
    """
    ctx_sin_backend.exigir_auth = False
    app = construir_app(ctx_sin_backend, config=ajustes, con_auth=False)
    with TestClient(app, base_url="http://localhost:8080") as c:
        yield c


CABECERAS_MCP = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
INICIALIZAR = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "prueba", "version": "1"},
    },
}


# --------------------------------------------------------------------------- #
# Layer 1 on the wire
# --------------------------------------------------------------------------- #


class TestAutenticacion:
    def test_sin_token_responde_401(self, cliente: TestClient) -> None:
        respuesta = cliente.post("/mcp", json=INICIALIZAR, headers=CABECERAS_MCP)
        assert respuesta.status_code == 401

    def test_el_401_dice_donde_autenticarse(self, cliente: TestClient) -> None:
        """A 401 without WWW-Authenticate leaves a compliant client with nowhere
        to go: it cannot discover the authorization server."""
        respuesta = cliente.post("/mcp", json=INICIALIZAR, headers=CABECERAS_MCP)
        cabecera = respuesta.headers.get("www-authenticate", "")
        assert cabecera.lower().startswith("bearer")
        assert "resource_metadata" in cabecera

    def test_un_token_basura_tambien_es_401(self, cliente: TestClient) -> None:
        respuesta = cliente.post(
            "/mcp",
            json=INICIALIZAR,
            headers={**CABECERAS_MCP, "Authorization": "Bearer no-es-un-jwt"},
        )
        assert respuesta.status_code == 401

    def test_publica_la_metadata_del_recurso_protegido(self, cliente: TestClient) -> None:
        respuesta = cliente.get("/.well-known/oauth-protected-resource")
        assert respuesta.status_code == 200
        cuerpo = respuesta.json()
        assert cuerpo["resource"].rstrip("/") == PUBLICA
        assert "http://localhost:9000" in [s.rstrip("/") for s in cuerpo["authorization_servers"]]

    def test_la_metadata_no_exige_autenticacion(self, cliente: TestClient) -> None:
        """Requiring a token to discover where to get a token is a deadlock."""
        assert cliente.get("/.well-known/oauth-protected-resource").status_code == 200


# --------------------------------------------------------------------------- #
# DNS rebinding
# --------------------------------------------------------------------------- #


class TestGuardasDeTransporte:
    @pytest.mark.parametrize(
        "origen",
        [
            "http://evil.test",
            "https://atacante.example",
            "http://localhost:9999",
            "null",
        ],
    )
    def test_un_origin_no_permitido_se_rechaza(
        self, cliente_sin_auth: TestClient, origen: str
    ) -> None:
        respuesta = cliente_sin_auth.post(
            "/mcp", json=INICIALIZAR, headers={**CABECERAS_MCP, "Origin": origen}
        )
        # A bad Origin is a forbidden *caller* (403); a bad Host is a request
        # that arrived at the wrong server (421). The SDK distinguishes them.
        assert respuesta.status_code == 403, (
            f"Origin '{origen}' no fue rechazado: una página web podría manejar "
            "este servidor con las credenciales del usuario"
        )

    def test_el_origin_permitido_pasa_la_guarda(self, cliente_sin_auth: TestClient) -> None:
        respuesta = cliente_sin_auth.post(
            "/mcp", json=INICIALIZAR, headers={**CABECERAS_MCP, "Origin": PUBLICA}
        )
        assert respuesta.status_code == 200, (
            "el Origin permitido debería atravesar la guarda; si falla, la lista "
            "blanca no coincide con el Host real que envía el cliente"
        )

    def test_el_host_con_puerto_pasa_la_guarda(self, cliente_sin_auth: TestClient) -> None:
        """The header a browser sends is `localhost:8080`, not `localhost`. A
        bare allow-list matches nothing and rejects every legitimate request."""
        respuesta = cliente_sin_auth.post(
            "/mcp", json=INICIALIZAR, headers={**CABECERAS_MCP, "Host": "localhost:8080"}
        )
        assert respuesta.status_code == 200

    @pytest.mark.parametrize("host", ["evil.test", "attacker.example:8080"])
    def test_un_host_no_permitido_se_rechaza(self, cliente_sin_auth: TestClient, host: str) -> None:
        respuesta = cliente_sin_auth.post(
            "/mcp", json=INICIALIZAR, headers={**CABECERAS_MCP, "Host": host}
        )
        assert respuesta.status_code == 421

    def test_con_auth_activa_un_rebinding_anonimo_se_corta_igual(self, cliente: TestClient) -> None:
        """With authentication on, the auth middleware answers first. The attack
        is still blocked, just at 401 instead of 421."""
        respuesta = cliente.post(
            "/mcp", json=INICIALIZAR, headers={**CABECERAS_MCP, "Origin": "http://evil.test"}
        )
        assert respuesta.status_code == 401

    def test_la_lista_blanca_viene_de_la_configuracion_y_se_expande(
        self, ajustes: Settings
    ) -> None:
        assert ajustes.mcp_allowed_hosts == [
            "localhost",
            "localhost:8080",
            "127.0.0.1",
            "127.0.0.1:8080",
        ]
        assert ajustes.mcp_allowed_origins == ["http://localhost:8080"]

    def test_no_hay_comodines_en_la_lista_blanca(self, ajustes: Settings) -> None:
        """A `*` here silently disables the guard while looking configured."""
        assert "*" not in ajustes.mcp_allowed_hosts
        assert "*" not in ajustes.mcp_allowed_origins


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #


class TestVentanaDeslizante:
    def test_permite_hasta_el_limite(self) -> None:
        ventana = VentanaDeslizante(limite=3, ventana=60)
        assert [ventana.permitir("a", ahora=0)[0] for _ in range(3)] == [True] * 3
        assert ventana.permitir("a", ahora=0)[0] is False

    def test_la_espera_sugerida_es_util(self) -> None:
        ventana = VentanaDeslizante(limite=1, ventana=60)
        ventana.permitir("a", ahora=100)
        permitido, espera = ventana.permitir("a", ahora=110)
        assert permitido is False
        assert 49 <= espera <= 50

    def test_es_deslizante_no_de_ventana_fija(self) -> None:
        """A fixed window lets a caller fire the whole budget at the seam and
        again immediately after: twice the intended rate."""
        ventana = VentanaDeslizante(limite=2, ventana=10)
        ventana.permitir("a", ahora=0)
        ventana.permitir("a", ahora=9)
        assert ventana.permitir("a", ahora=9.5)[0] is False
        assert ventana.permitir("a", ahora=10.1)[0] is True

    def test_cada_clave_tiene_su_propio_presupuesto(self) -> None:
        """Limiting purely by IP would let one agent starve everyone behind the
        same NAT."""
        ventana = VentanaDeslizante(limite=1, ventana=60)
        assert ventana.permitir("sub:ana", ahora=0)[0] is True
        assert ventana.permitir("sub:bruno", ahora=0)[0] is True
        assert ventana.permitir("sub:ana", ahora=0)[0] is False


class TestMiddlewareDeLimite:
    @pytest.fixture
    def app_limitada(self) -> Starlette:
        reloj = itertools.count(0, 0)  # frozen clock: every call is "now"

        async def ok(_: Request) -> PlainTextResponse:
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/ping", ok)])
        app.add_middleware(
            LimitadorDePeticiones, limite=3, ventana_segundos=60, reloj=lambda: next(reloj)
        )
        return app

    def test_corta_pasado_el_limite(self, app_limitada: Starlette) -> None:
        with TestClient(app_limitada) as c:
            codigos = [c.get("/ping").status_code for _ in range(5)]
        assert codigos == [200, 200, 200, 429, 429]

    def test_el_429_explica_cuanto_esperar(self, app_limitada: Starlette) -> None:
        with TestClient(app_limitada) as c:
            for _ in range(3):
                c.get("/ping")
            respuesta = c.get("/ping")
        assert respuesta.status_code == 429
        assert respuesta.headers["retry-after"] == "60"
        cuerpo = respuesta.json()
        assert cuerpo["codigo"] == "RATE_LIMIT_EXCEDIDO"
        # It must tell an agent in a retry loop to stop looping.
        assert "bucle de reintentos" in cuerpo["sugerencia"]

    def test_el_error_usa_la_misma_envoltura_estructurada(self, app_limitada: Starlette) -> None:
        with TestClient(app_limitada) as c:
            for _ in range(4):
                respuesta = c.get("/ping")
        cuerpo = respuesta.json()
        assert set(cuerpo) >= {"error", "codigo", "mensaje", "sugerencia"}

    async def test_deja_pasar_el_trafico_que_no_es_http(self) -> None:
        """A websocket or lifespan scope must not be counted or blocked."""
        visto: list[str] = []

        async def app(scope: Any, receive: Any, send: Any) -> None:
            visto.append(scope["type"])

        limitador = LimitadorDePeticiones(app, limite=0, ventana_segundos=60)
        await limitador({"type": "lifespan"}, None, None)  # type: ignore[arg-type]
        assert visto == ["lifespan"]


class TestElBackendNoEsAlcanzable:
    def test_el_servidor_mcp_no_expone_la_api_de_dominio(self, cliente: TestClient) -> None:
        """The MCP surface is /mcp and the discovery documents. Nothing else,
        and in particular no path through to the internal REST API."""
        for ruta in ("/citas/1", "/pacientes", "/salud", "/docs", "/openapi.json"):
            assert cliente.get(ruta).status_code in {404, 405}, ruta


class TestClienteBackendAnteFallos:
    async def test_un_backend_caido_da_un_error_accionable(self) -> None:
        transporte = httpx.MockTransport(
            lambda _: (_ for _ in ()).throw(httpx.ConnectError("sin ruta al host"))
        )
        async with httpx.AsyncClient(transport=transporte, base_url="http://backend") as http:
            cliente = ClienteBackend("http://backend", cliente=http)
            with pytest.raises(Exception) as exc:
                await cliente.obtener("/clinica")
        mensaje = str(exc.value)
        assert "BACKEND_NO_DISPONIBLE" in mensaje
        assert "no reintentes en bucle" in mensaje

    async def test_una_respuesta_con_forma_inesperada_se_detecta(self) -> None:
        transporte = httpx.MockTransport(
            lambda _: httpx.Response(200, json=["no", "es", "un", "objeto"])
        )
        async with httpx.AsyncClient(transport=transporte, base_url="http://backend") as http:
            cliente = ClienteBackend("http://backend", cliente=http)
            with pytest.raises(Exception) as exc:
                await cliente.obtener("/clinica")
        assert "RESPUESTA_INESPERADA" in str(exc.value)

    async def test_un_error_sin_envoltura_no_revienta(self) -> None:
        transporte = httpx.MockTransport(lambda _: httpx.Response(502, text="bad gateway"))
        async with httpx.AsyncClient(transport=transporte, base_url="http://backend") as http:
            cliente = ClienteBackend("http://backend", cliente=http)
            with pytest.raises(Exception) as exc:
                await cliente.obtener("/clinica")
        assert "502" in str(exc.value)
