"""Shared test fixtures for every suite.

Three layers of fixture live here, in this order:

* PostgreSQL: a migrated, disposable database (`engine`, `session`, `sesiones`);
* the domain scenario: a small, fully controlled clinic (`escenario`);
* the MCP server: the real server wired to the real API over the real database
  (`ctx`, `servidor`), plus the `como(...)` helper that runs a block as a caller
  holding an exact set of scopes.

Database strategy: integration tests run against a **real PostgreSQL**, never
SQLite. Partial unique indexes, native enums and timezone-aware timestamps do
not exist there, so testing against SQLite would be testing a different system.

Where that database comes from:

* ``TEST_DATABASE_URL`` if set (this is what CI uses, via a service container);
* otherwise a throwaway container started by testcontainers.

If neither is available the integration tests skip with a clear reason rather
than failing, so ``make test-unit`` works on a machine without Docker.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from alembic import command
from alembic.config import Config
from asgi_lifespan import LifespanManager
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from backend.api import app as backend_app
from backend.config import Settings
from backend.database import get_session
from backend.domain.tiempo import UTC, a_local, slots_del_dia
from backend.enums import ConceptoCargo, Especialidad, EstadoCargo, Regimen, TipoDocumento
from backend.models import AgendaSlot, Base, Cargo, Clinica, Paciente, Profesional
from mcp_server.auditoria import Auditor
from mcp_server.cliente import ClienteBackend
from mcp_server.contexto import Contexto
from mcp_server.server import construir_app, crear_servidor

FECHA_BASE_TESTS = date(2026, 8, 31)


def _url_desde_contenedor() -> tuple[str, object] | None:
    """Start a disposable PostgreSQL, or return None if Docker is unavailable."""
    try:
        # Moved package in testcontainers 4.x; keep the old path as a fallback
        # so the suite runs on either version.
        from testcontainers.community.postgres import PostgresContainer
    except ImportError:  # pragma: no cover - older testcontainers
        try:
            from testcontainers.postgres import PostgresContainer
        except ImportError:  # pragma: no cover - dev extra missing
            return None
    try:
        contenedor = PostgresContainer("postgres:16-alpine", driver="psycopg")
        contenedor.start()
    except Exception:  # pragma: no cover - no docker daemon on this machine
        return None
    return contenedor.get_connection_url(), contenedor


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    url = os.getenv("TEST_DATABASE_URL")
    if url:
        yield url
        return

    resultado = _url_desde_contenedor()
    if resultado is None:
        pytest.skip(
            "No hay PostgreSQL para pruebas de integración. "
            "Define TEST_DATABASE_URL o levanta Docker.",
            allow_module_level=False,
        )
    url, contenedor = resultado
    try:
        yield url
    finally:
        contenedor.stop()  # type: ignore[attr-defined]


@pytest.fixture(scope="session")
def alembic_config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _es_base_de_pruebas(url: str) -> bool:
    """Guard for the schema drop below.

    The engine fixture wipes `public`. Accept only a database whose name marks
    it as disposable, so a mistyped TEST_DATABASE_URL cannot destroy a dev or,
    far worse, a production database.
    """
    nombre = urlsplit(url).path.lstrip("/").lower()
    return any(marca in nombre for marca in ("test", "_ci", "pytest")) or nombre.startswith("tc-")


@pytest.fixture(scope="session")
def engine(database_url: str, alembic_config: Config) -> Iterator[Engine]:
    """Engine over a schema built by the *migrations*, not by ``create_all``.

    Testing against ``create_all`` would let the migrations rot silently; here a
    broken migration fails the whole suite.
    """
    if not _es_base_de_pruebas(database_url):
        pytest.fail(
            "TEST_DATABASE_URL apunta a una base que no parece de pruebas "
            f"({urlsplit(database_url).path}). El fixture borra el esquema público: "
            "usa una base cuyo nombre contenga 'test'."
        )
    eng = create_engine(database_url, future=True, poolclass=None)
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    command.upgrade(alembic_config, "head")
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """Function-scoped session wrapped in a transaction that is always rolled
    back, so tests are isolated without paying to rebuild the schema."""
    connection = engine.connect()
    transaction = connection.begin()
    factory = sessionmaker(bind=connection, expire_on_commit=False, future=True)
    sesion = factory()
    try:
        yield sesion
    finally:
        sesion.close()
        # A test that deliberately triggers an IntegrityError leaves the
        # transaction already unwound; rolling back again is a no-op, not a
        # failure, so it must not surface as a warning-shaped test result.
        if transaction.is_active:
            transaction.rollback()
        connection.close()


@pytest.fixture
def tablas_vacias(session: Session) -> Session:
    """A session whose tables are empty, for tests that assert on counts."""
    for tabla in reversed(Base.metadata.sorted_tables):
        session.execute(tabla.delete())
    session.flush()
    return session


@pytest.fixture
def sesiones(engine: Engine) -> Iterator[Callable[[], Session]]:
    """Factory of independent, *committing* sessions.

    Two agents racing for the same slot need two real connections; the
    rollback-wrapped `session` fixture cannot express that. Everything created
    here is wiped on teardown.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    abiertas: list[Session] = []

    def abrir() -> Session:
        sesion = factory()
        abiertas.append(sesion)
        return sesion

    try:
        yield abrir
    finally:
        for sesion in abiertas:
            sesion.rollback()
            sesion.close()
        limpieza = factory()
        try:
            for tabla in reversed(Base.metadata.sorted_tables):
                limpieza.execute(tabla.delete())
            limpieza.commit()
        finally:
            limpieza.close()


@pytest.fixture
def datos_minimos(sesiones: Callable[[], Session]) -> dict[str, int]:
    """One clinic, one dentist, two patients and one free slot, committed."""
    sesion = sesiones()
    clinica = Clinica(nombre="Clínica Test", nit="900.000.001-1", especialidad="Odontología")
    sesion.add(clinica)
    sesion.flush()

    profesional = Profesional(
        clinica_id=clinica.id,
        nombre="Dra. Prueba",
        registro="RM-TEST-1",
        especialidad=Especialidad.ODONTOLOGIA_GENERAL,
    )
    sesion.add(profesional)
    sesion.flush()

    pacientes = [
        Paciente(
            tipo_documento=TipoDocumento.CC,
            documento=f"100000{i}",
            nombre=f"Paciente {i}",
            telefono="+57 3001112233",
            regimen=Regimen.CONTRIBUTIVO,
            afiliacion_activa=True,
        )
        for i in (1, 2)
    ]
    sesion.add_all(pacientes)
    sesion.flush()

    manana = date.today() + timedelta(days=1)
    while not slots_del_dia(manana):
        manana += timedelta(days=1)
    inicio, fin = slots_del_dia(manana)[0]
    slot = AgendaSlot(
        profesional_id=profesional.id,
        fecha=inicio.astimezone(UTC).date(),
        inicio=inicio,
        fin=fin,
    )
    sesion.add(slot)
    sesion.commit()

    return {
        "clinica_id": clinica.id,
        "profesional_id": profesional.id,
        "paciente_a": pacientes[0].id,
        "paciente_b": pacientes[1].id,
        "slot_id": slot.id,
    }


@dataclass(frozen=True, slots=True)
class Escenario:
    """A small, fully controlled clinic.

    Deliberately *not* the Faker seed: assertions here must be exact, and a
    randomised dataset turns every expected value into an approximation.
    """

    clinica_id: int
    general_id: int
    orto_id: int
    #: Contributory regime, active affiliation, consent on file.
    ana_id: int
    ana_documento: str
    #: Subsidised regime, affiliation lapsed → private tariff.
    bruno_id: int
    #: Private patient, no consent recorded → clinical tool must refuse.
    carla_id: int
    #: In arrears well above the alert threshold.
    deudor_id: int
    slots_general: list[int]
    slots_orto: list[int]
    slot_pasado_id: int
    fecha_futura: date


@pytest.fixture
def escenario(sesiones: Callable[[], Session]) -> Escenario:
    sesion = sesiones()
    clinica = Clinica(nombre="Clínica Escenario", nit="900.777.111-2", especialidad="Odontología")
    sesion.add(clinica)
    sesion.flush()

    general = Profesional(
        clinica_id=clinica.id,
        nombre="Dra. General",
        registro="RM-ESC-1",
        especialidad=Especialidad.ODONTOLOGIA_GENERAL,
    )
    orto = Profesional(
        clinica_id=clinica.id,
        nombre="Dr. Ortodoncia",
        registro="RM-ESC-2",
        especialidad=Especialidad.ORTODONCIA,
    )
    sesion.add_all([general, orto])
    sesion.flush()

    def paciente(
        documento: str,
        nombre: str,
        regimen: Regimen,
        *,
        activa: bool = True,
        consentimiento: bool = True,
    ) -> Paciente:
        return Paciente(
            tipo_documento=TipoDocumento.CC,
            documento=documento,
            nombre=nombre,
            telefono="+57 3001112233",
            email=f"{documento}@ejemplo.test",
            regimen=regimen,
            afiliacion_activa=activa,
            nivel_cuota_moderadora=1,
            consentimiento_datos_clinicos=consentimiento,
        )

    ana = paciente("11111111", "Ana Gómez Ruiz", Regimen.CONTRIBUTIVO)
    bruno = paciente("22222222", "Bruno Díaz Peña", Regimen.SUBSIDIADO, activa=False)
    carla = paciente("33333333", "Carla Ríos Mora", Regimen.PARTICULAR, consentimiento=False)
    deudor = paciente("44444444", "Diego Mora Ruiz", Regimen.CONTRIBUTIVO)
    sesion.add_all([ana, bruno, carla, deudor])
    sesion.flush()

    # A debt large enough and old enough to trip the alert threshold.
    sesion.add(
        Cargo(
            paciente_id=deudor.id,
            concepto=ConceptoCargo.PARTICULAR,
            monto=Decimal("180000"),
            descripcion="Tarifa particular vencida",
            estado=EstadoCargo.PENDIENTE,
            vencimiento=date.today() - timedelta(days=75),
        )
    )

    # A future working day with free slots for both professionals.
    futura = date.today() + timedelta(days=3)
    while not slots_del_dia(futura):
        futura += timedelta(days=1)
    rangos = slots_del_dia(futura)

    slots_general: list[AgendaSlot] = []
    slots_orto: list[AgendaSlot] = []
    for inicio, fin in rangos[:6]:
        for profesional, destino in ((general, slots_general), (orto, slots_orto)):
            slot = AgendaSlot(
                profesional_id=profesional.id,
                fecha=a_local(inicio).date(),
                inicio=inicio,
                fin=fin,
            )
            sesion.add(slot)
            destino.append(slot)

    # One slot in the past, to prove the domain refuses to book backwards.
    pasada = date.today() - timedelta(days=5)
    while not slots_del_dia(pasada):
        pasada -= timedelta(days=1)
    inicio_pasado, fin_pasado = slots_del_dia(pasada)[0]
    slot_pasado = AgendaSlot(
        profesional_id=general.id,
        fecha=a_local(inicio_pasado).date(),
        inicio=inicio_pasado,
        fin=fin_pasado,
    )
    sesion.add(slot_pasado)
    sesion.commit()

    return Escenario(
        clinica_id=clinica.id,
        general_id=general.id,
        orto_id=orto.id,
        ana_id=ana.id,
        ana_documento=ana.documento,
        bruno_id=bruno.id,
        carla_id=carla.id,
        deudor_id=deudor.id,
        slots_general=[s.id for s in slots_general],
        slots_orto=[s.id for s in slots_orto],
        slot_pasado_id=slot_pasado.id,
        fecha_futura=futura,
    )


# --------------------------------------------------------------------------- #
# MCP server
# --------------------------------------------------------------------------- #


SUJETO = "recepcion@clinica.test"

#: Key ring for the sealed request state. Test-only, 32 bytes as the codec wants.
CLAVES_ESTADO = ["clave-de-pruebas-para-request-state-32"]

#: What a 2026-07-28 client must send on every call once there is no session to
#: remember the handshake. This is the visible cost of a stateless transport.
SOBRE_MCP: dict[str, Any] = {
    "_meta": {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {"elicitation": {}},
    }
}
CABECERAS_BASE = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2026-07-28",
}


@pytest.fixture
def sesion_backend(sesiones: Callable[[], Session]) -> Session:
    return sesiones()


@pytest.fixture
def ajustes_mcp() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        app_env="test",
        mcp_public_url="http://localhost:8080",
        oauth_issuer="http://localhost:9000",
        oauth_audience="http://localhost:8080",
        request_state_keys=CLAVES_ESTADO,
    )


@pytest.fixture
async def cliente_backend(sesion_backend: Session) -> AsyncIterator[ClienteBackend]:
    """A backend client wired to the FastAPI app in-process."""

    def override() -> Iterator[Session]:
        yield sesion_backend
        sesion_backend.commit()

    backend_app.dependency_overrides[get_session] = override
    transporte = httpx.ASGITransport(app=backend_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transporte, base_url="http://backend") as http:
        yield ClienteBackend("http://backend", cliente=http)
    backend_app.dependency_overrides.clear()


@pytest.fixture
def ctx(cliente_backend: ClienteBackend) -> Contexto:
    return Contexto(cliente=cliente_backend, auditor=Auditor(), exigir_auth=True)


@pytest.fixture
def servidor(ctx: Contexto, ajustes_mcp: Settings) -> MCPServer[Any]:
    """The real server, for assertions about its catalogue."""
    return crear_servidor(ctx, config=ajustes_mcp, con_auth=False)


class ErrorDeHerramienta(Exception):
    """A tool answered `isError`. Carries the text the model would read."""

    def __init__(self, texto: str) -> None:
        super().__init__(texto)
        self.texto = texto


class ClienteMCP:
    """A minimal 2026-07-28 client, enough to drive the server over the wire.

    The contract suite talks to the server this way rather than calling its
    methods, because MRTR only exists on the wire: a tool that needs a human's
    answer returns `input_required`, and the round trip is the thing worth
    testing.
    """

    def __init__(self, http: httpx.AsyncClient, *, puede_confirmar: bool = True) -> None:
        self.http = http
        self.id = 0
        #: A client that does not declare `elicitation` stands in for one on an
        #: older spec, which cannot answer an `input_required`.
        self.puede_confirmar = puede_confirmar

    async def _rpc(self, metodo: str, params: dict[str, Any]) -> Any:
        self.id += 1
        cabeceras = {**CABECERAS_BASE, "mcp-method": metodo}
        if "name" in params:
            cabeceras["mcp-name"] = str(params["name"])
        respuesta = await self.http.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": self.id,
                "method": metodo,
                "params": {**params, **self._sobre()},
            },
            headers=cabeceras,
        )
        cuerpo = respuesta.text
        for linea in cuerpo.splitlines():
            if linea.startswith("data:"):
                cuerpo = linea[5:].strip()
                break
        datos = json.loads(cuerpo)
        if "error" in datos:
            raise ErrorDeHerramienta(json.dumps(datos["error"], ensure_ascii=False))
        return datos["result"]

    def _sobre(self) -> dict[str, Any]:
        capacidades: dict[str, Any] = {"elicitation": {}} if self.puede_confirmar else {}
        return {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": capacidades,
            }
        }

    @staticmethod
    def _desenvolver(resultado: dict[str, Any]) -> Any:
        if resultado.get("isError"):
            textos = [c.get("text", "") for c in resultado.get("content", [])]
            raise ErrorDeHerramienta("\n".join(t for t in textos if t))
        contenido = resultado.get("structuredContent")
        if isinstance(contenido, dict) and set(contenido) == {"result"}:
            return contenido["result"]
        if contenido is not None:
            return contenido
        return [c.get("text") for c in resultado.get("content", [])]

    async def llamar(self, nombre: str, argumentos: dict[str, Any]) -> Any:
        """Call a tool that needs no human answer."""
        return self._desenvolver(
            await self._rpc("tools/call", {"name": nombre, "arguments": argumentos})
        )

    async def preguntar(self, nombre: str, argumentos: dict[str, Any]) -> dict[str, Any]:
        """Call a tool expecting `input_required`, and return what it asks."""
        resultado = await self._rpc("tools/call", {"name": nombre, "arguments": argumentos})
        if resultado.get("resultType") != "input_required":
            self._desenvolver(resultado)  # raises if it was an error
            raise AssertionError(f"{nombre} no pidió confirmación: {resultado}")
        return resultado

    async def responder(
        self,
        nombre: str,
        argumentos: dict[str, Any],
        pregunta: dict[str, Any],
        *,
        confirmado: bool = True,
        accion: str = "accept",
    ) -> Any:
        """Retry the same call carrying the human's answer."""
        clave = next(iter(pregunta["inputRequests"]))
        respuesta: dict[str, Any] = {"action": accion}
        if accion == "accept":
            respuesta["content"] = {"confirmado": confirmado}
        return self._desenvolver(
            await self._rpc(
                "tools/call",
                {
                    "name": nombre,
                    "arguments": argumentos,
                    "inputResponses": {clave: respuesta},
                    "requestState": pregunta["requestState"],
                },
            )
        )

    async def aprobar(self, nombre: str, argumentos: dict[str, Any]) -> Any:
        """The whole round trip: ask, approve, execute."""
        pregunta = await self.preguntar(nombre, argumentos)
        return await self.responder(nombre, argumentos, pregunta)

    def mensaje_de(self, pregunta: dict[str, Any]) -> str:
        clave = next(iter(pregunta["inputRequests"]))
        return str(pregunta["inputRequests"][clave]["params"]["message"])


@asynccontextmanager
async def servidor_http(ctx: Contexto, ajustes: Settings) -> AsyncIterator[ClienteMCP]:
    """A running server over HTTP, for tests that need custom settings."""
    app = construir_app(ctx, config=ajustes, con_auth=False)
    async with LifespanManager(app):
        transporte = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transporte, base_url="http://localhost:8080"
        ) as http:
            yield ClienteMCP(http)


@pytest.fixture
async def mcp_sin_elicitacion(ctx: Contexto, ajustes_mcp: Settings) -> AsyncIterator[ClienteMCP]:
    """A client that cannot ask a person anything, like an older one."""
    app = construir_app(ctx, config=ajustes_mcp, con_auth=False)
    async with LifespanManager(app):
        transporte = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transporte, base_url="http://localhost:8080"
        ) as http:
            yield ClienteMCP(http, puede_confirmar=False)


@pytest.fixture
async def mcp(ctx: Contexto, ajustes_mcp: Settings) -> AsyncIterator[ClienteMCP]:
    """The server over HTTP, with the lifespan running."""
    app = construir_app(ctx, config=ajustes_mcp, con_auth=False)
    async with LifespanManager(app):
        transporte = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transporte, base_url="http://localhost:8080"
        ) as http:
            # No handshake: stateless means every call stands alone, carrying
            # its own protocol version and capabilities in `_meta`.
            yield ClienteMCP(http)


@contextmanager
def como(sujeto: str, scopes: list[str]) -> Iterator[None]:
    """Run the block as a caller holding exactly these scopes."""
    token = AccessToken(
        token="token-de-prueba",
        client_id="cliente-de-prueba",
        scopes=scopes,
        expires_at=None,
        subject=sujeto,
    )
    reset = auth_context_var.set(AuthenticatedUser(token))
    try:
        yield
    finally:
        auth_context_var.reset(reset)


def texto(resultado: CallToolResult) -> str:
    """Flatten a tool result to the text the model would actually read."""
    partes = [getattr(bloque, "text", "") for bloque in resultado.content]
    return "\n".join(p for p in partes if p)


def datos(resultado: CallToolResult) -> Any:
    """The structured payload of a successful in-process call."""
    assert not resultado.is_error, texto(resultado)
    if resultado.structured_content is not None:
        contenido = resultado.structured_content
        if isinstance(contenido, dict) and set(contenido) == {"result"}:
            return contenido["result"]
        return contenido
    return json.loads(texto(resultado))


async def llamar(servidor: MCPServer[Any], nombre: str, argumentos: dict[str, Any]) -> Any:
    """Call a read tool in-process and return its payload."""
    return datos(await servidor.call_tool(nombre, argumentos))


async def error_de(servidor: MCPServer[Any], nombre: str, argumentos: dict[str, Any]) -> str:
    """Call a read tool expecting failure; return the message the model reads."""
    with pytest.raises(ToolError) as capturado:
        await servidor.call_tool(nombre, argumentos)
    return str(capturado.value)
