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
from backend.domain.time import UTC, now_at_clinic, slots_for_day, to_clinic_time
from backend.enums import ChargeConcept, ChargeState, DocumentType, Regimen, Specialty
from backend.models import AgendaSlot, Base, Charge, Clinic, Patient, Professional
from mcp_server.audit import Auditor
from mcp_server.client import BackendClient
from mcp_server.context import ToolContext
from mcp_server.server import build_app, build_server

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

    result = _url_desde_contenedor()
    if result is None:
        pytest.skip(
            "No hay PostgreSQL para pruebas de integración. "
            "Define TEST_DATABASE_URL o levanta Docker.",
            allow_module_level=False,
        )
    url, contenedor = result
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
    session_ = factory()
    try:
        yield session_
    finally:
        session_.close()
        # A test that deliberately triggers an IntegrityError leaves the
        # transaction already unwound; rolling back again is a no-op, not a
        # failure, so it must not surface as a warning-shaped test result.
        if transaction.is_active:
            transaction.rollback()
        connection.close()


@pytest.fixture
def empty_tables(session: Session) -> Session:
    """A session whose tables are empty, for tests that assert on counts."""
    for table in reversed(Base.metadata.sorted_tables):
        session.execute(table.delete())
    session.flush()
    return session


@pytest.fixture
def sessions(engine: Engine) -> Iterator[Callable[[], Session]]:
    """Factory of independent, *committing* sessions.

    Two agents racing for the same slot need two real connections; the
    rollback-wrapped `session` fixture cannot express that. Everything created
    here is wiped on teardown.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    abiertas: list[Session] = []

    def abrir() -> Session:
        session_ = factory()
        abiertas.append(session_)
        return session_

    try:
        yield abrir
    finally:
        for session_ in abiertas:
            session_.rollback()
            session_.close()
        limpieza = factory()
        try:
            for table in reversed(Base.metadata.sorted_tables):
                limpieza.execute(table.delete())
            limpieza.commit()
        finally:
            limpieza.close()


@pytest.fixture
def minimal_data(sessions: Callable[[], Session]) -> dict[str, int]:
    """One clinic, one dentist, two patients and one free slot, committed."""
    session_ = sessions()
    clinica = Clinic(nombre="Clínica Test", nit="900.000.001-1", especialidad="Odontología")
    session_.add(clinica)
    session_.flush()

    profesional = Professional(
        clinica_id=clinica.id,
        nombre="Dra. Prueba",
        registro="RM-TEST-1",
        especialidad=Specialty.GENERAL_DENTISTRY,
    )
    session_.add(profesional)
    session_.flush()

    patients = [
        Patient(
            tipo_documento=DocumentType.CC,
            documento=f"100000{i}",
            nombre=f"Paciente {i}",
            telefono="+57 3001112233",
            regimen=Regimen.CONTRIBUTIVO,
            afiliacion_activa=True,
        )
        for i in (1, 2)
    ]
    session_.add_all(patients)
    session_.flush()

    # The clinic's calendar, not the runner's: they differ for five hours a day.
    manana = now_at_clinic().date() + timedelta(days=1)
    while not slots_for_day(manana):
        manana += timedelta(days=1)
    inicio, fin = slots_for_day(manana)[0]
    slot = AgendaSlot(
        profesional_id=profesional.id,
        fecha=inicio.astimezone(UTC).date(),
        inicio=inicio,
        fin=fin,
    )
    session_.add(slot)
    session_.commit()

    return {
        "clinica_id": clinica.id,
        "profesional_id": profesional.id,
        "paciente_a": patients[0].id,
        "paciente_b": patients[1].id,
        "slot_id": slot.id,
    }


@dataclass(frozen=True, slots=True)
class Scenario:
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
def scenario(sessions: Callable[[], Session]) -> Scenario:
    session_ = sessions()
    clinica = Clinic(nombre="Clínica Escenario", nit="900.777.111-2", especialidad="Odontología")
    session_.add(clinica)
    session_.flush()

    general = Professional(
        clinica_id=clinica.id,
        nombre="Dra. General",
        registro="RM-ESC-1",
        especialidad=Specialty.GENERAL_DENTISTRY,
    )
    orto = Professional(
        clinica_id=clinica.id,
        nombre="Dr. Ortodoncia",
        registro="RM-ESC-2",
        especialidad=Specialty.ORTHODONTICS,
    )
    session_.add_all([general, orto])
    session_.flush()

    def paciente(
        documento: str,
        nombre: str,
        regimen: Regimen,
        *,
        activa: bool = True,
        consentimiento: bool = True,
    ) -> Patient:
        return Patient(
            tipo_documento=DocumentType.CC,
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
    session_.add_all([ana, bruno, carla, deudor])
    session_.flush()

    # A debt large enough and old enough to trip the alert threshold.
    session_.add(
        Charge(
            paciente_id=deudor.id,
            concepto=ChargeConcept.PARTICULAR,
            monto=Decimal("180000"),
            descripcion="Tarifa particular vencida",
            estado=ChargeState.PENDING,
            vencimiento=now_at_clinic().date() - timedelta(days=75),
        )
    )

    # A future working day with free slots for both professionals.
    futura = now_at_clinic().date() + timedelta(days=3)
    while not slots_for_day(futura):
        futura += timedelta(days=1)
    rangos = slots_for_day(futura)

    slots_general: list[AgendaSlot] = []
    slots_orto: list[AgendaSlot] = []
    for inicio, fin in rangos[:6]:
        for profesional, target in ((general, slots_general), (orto, slots_orto)):
            slot = AgendaSlot(
                profesional_id=profesional.id,
                fecha=to_clinic_time(inicio).date(),
                inicio=inicio,
                fin=fin,
            )
            session_.add(slot)
            target.append(slot)

    # One slot in the past, to prove the domain refuses to book backwards.
    pasada = now_at_clinic().date() - timedelta(days=5)
    while not slots_for_day(pasada):
        pasada -= timedelta(days=1)
    inicio_pasado, fin_pasado = slots_for_day(pasada)[0]
    slot_pasado = AgendaSlot(
        profesional_id=general.id,
        fecha=to_clinic_time(inicio_pasado).date(),
        inicio=inicio_pasado,
        fin=fin_pasado,
    )
    session_.add(slot_pasado)
    session_.commit()

    return Scenario(
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


SUBJECT = "recepcion@clinica.test"

#: Key ring for the sealed request state. Test-only, 32 bytes as the codec wants.
CLAVES_ESTADO = ["clave-de-pruebas-para-request-state-32"]

#: What a 2026-07-28 client must send on every call once there is no session to
#: remember the handshake. This is the visible cost of a stateless transport.
MCP_ENVELOPE: dict[str, Any] = {
    "_meta": {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {"elicitation": {}},
    }
}
BASE_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2026-07-28",
}


@pytest.fixture
def backend_session(sessions: Callable[[], Session]) -> Session:
    return sessions()


@pytest.fixture
def mcp_settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        app_env="test",
        mcp_public_url="http://localhost:8080",
        oauth_issuer="http://localhost:9000",
        oauth_audience="http://localhost:8080",
        request_state_keys=CLAVES_ESTADO,
    )


@pytest.fixture
async def backend_client(backend_session: Session) -> AsyncIterator[BackendClient]:
    """A backend client wired to the FastAPI app in-process."""

    def override() -> Iterator[Session]:
        yield backend_session
        backend_session.commit()

    backend_app.dependency_overrides[get_session] = override
    transporte = httpx.ASGITransport(app=backend_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transporte, base_url="http://backend") as http:
        yield BackendClient("http://backend", client=http)
    backend_app.dependency_overrides.clear()


@pytest.fixture
def ctx(backend_client: BackendClient) -> ToolContext:
    return ToolContext(client=backend_client, auditor=Auditor(), exigir_auth=True)


@pytest.fixture
def server_(ctx: ToolContext, mcp_settings: Settings) -> MCPServer[Any]:
    """The real server, for assertions about its catalogue."""
    return build_server(ctx, config=mcp_settings, con_auth=False)


class ToolCallError(Exception):
    """A tool answered `isError`. Carries the text the model would read."""

    def __init__(self, text_of: str) -> None:
        super().__init__(text_of)
        self.text_of = text_of


class MCPTestClient:
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
        cabeceras = {**BASE_HEADERS, "mcp-method": metodo}
        if "name" in params:
            cabeceras["mcp-name"] = str(params["name"])
        response = await self.http.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": self.id,
                "method": metodo,
                "params": {**params, **self._sobre()},
            },
            headers=cabeceras,
        )
        body = response.text
        for line in body.splitlines():
            if line.startswith("data:"):
                body = line[5:].strip()
                break
        payload = json.loads(body)
        if "error" in payload:
            raise ToolCallError(json.dumps(payload["error"], ensure_ascii=False))
        return payload["result"]

    def _sobre(self) -> dict[str, Any]:
        capacidades: dict[str, Any] = {"elicitation": {}} if self.puede_confirmar else {}
        return {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": capacidades,
            }
        }

    @staticmethod
    def _desenvolver(result: dict[str, Any]) -> Any:
        if result.get("isError"):
            textos = [c.get("text", "") for c in result.get("content", [])]
            raise ToolCallError("\n".join(t for t in textos if t))
        contenido = result.get("structuredContent")
        if isinstance(contenido, dict) and set(contenido) == {"result"}:
            return contenido["result"]
        if contenido is not None:
            return contenido
        return [c.get("text") for c in result.get("content", [])]

    async def call_tool(self, nombre: str, arguments: dict[str, Any]) -> Any:
        """Call a tool that needs no human answer."""
        return self._desenvolver(
            await self._rpc("tools/call", {"name": nombre, "arguments": arguments})
        )

    async def ask(self, nombre: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a tool expecting `input_required`, and return what it asks."""
        result = await self._rpc("tools/call", {"name": nombre, "arguments": arguments})
        if result.get("resultType") != "input_required":
            self._desenvolver(result)  # raises if it was an error
            raise AssertionError(f"{nombre} no pidió confirmación: {result}")
        return result

    async def respond(
        self,
        nombre: str,
        arguments: dict[str, Any],
        question: dict[str, Any],
        *,
        confirmado: bool = True,
        action: str = "accept",
    ) -> Any:
        """Retry the same call carrying the human's answer."""
        key = next(iter(question["inputRequests"]))
        response: dict[str, Any] = {"action": action}
        if action == "accept":
            response["content"] = {"confirmado": confirmado}
        return self._desenvolver(
            await self._rpc(
                "tools/call",
                {
                    "name": nombre,
                    "arguments": arguments,
                    "inputResponses": {key: response},
                    "requestState": question["requestState"],
                },
            )
        )

    async def aprobar(self, nombre: str, arguments: dict[str, Any]) -> Any:
        """The whole round trip: ask, approve, execute."""
        question = await self.ask(nombre, arguments)
        return await self.respond(nombre, arguments, question)

    def question_text(self, question: dict[str, Any]) -> str:
        key = next(iter(question["inputRequests"]))
        return str(question["inputRequests"][key]["params"]["message"])


@asynccontextmanager
async def http_server(ctx: ToolContext, settings_: Settings) -> AsyncIterator[MCPTestClient]:
    """A running server over HTTP, for tests that need custom settings."""
    app = build_app(ctx, config=settings_, con_auth=False)
    async with LifespanManager(app):
        transporte = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transporte, base_url="http://localhost:8080"
        ) as http:
            yield MCPTestClient(http)


@pytest.fixture
async def mcp_without_elicitation(
    ctx: ToolContext, mcp_settings: Settings
) -> AsyncIterator[MCPTestClient]:
    """A client that cannot ask a person anything, like an older one."""
    app = build_app(ctx, config=mcp_settings, con_auth=False)
    async with LifespanManager(app):
        transporte = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transporte, base_url="http://localhost:8080"
        ) as http:
            yield MCPTestClient(http, puede_confirmar=False)


@pytest.fixture
async def mcp(ctx: ToolContext, mcp_settings: Settings) -> AsyncIterator[MCPTestClient]:
    """The server over HTTP, with the lifespan running."""
    app = build_app(ctx, config=mcp_settings, con_auth=False)
    async with LifespanManager(app):
        transporte = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transporte, base_url="http://localhost:8080"
        ) as http:
            # No handshake: stateless means every call stands alone, carrying
            # its own protocol version and capabilities in `_meta`.
            yield MCPTestClient(http)


@contextmanager
def as_caller(subject: str, scopes: list[str]) -> Iterator[None]:
    """Run the block as a caller holding exactly these scopes."""
    token = AccessToken(
        token="token-de-prueba",
        client_id="cliente-de-prueba",
        scopes=scopes,
        expires_at=None,
        subject=subject,
    )
    reset = auth_context_var.set(AuthenticatedUser(token))
    try:
        yield
    finally:
        auth_context_var.reset(reset)


def text_of(result: CallToolResult) -> str:
    """Flatten a tool result to the text the model would actually read."""
    parts = [getattr(bloque, "text", "") for bloque in result.content]
    return "\n".join(p for p in parts if p)


def payload(result: CallToolResult) -> Any:
    """The structured payload of a successful in-process call."""
    assert not result.is_error, text_of(result)
    if result.structured_content is not None:
        contenido = result.structured_content
        if isinstance(contenido, dict) and set(contenido) == {"result"}:
            return contenido["result"]
        return contenido
    return json.loads(text_of(result))


async def call_tool(server_: MCPServer[Any], nombre: str, arguments: dict[str, Any]) -> Any:
    """Call a read tool in-process and return its payload."""
    return payload(await server_.call_tool(nombre, arguments))


async def error_from(server_: MCPServer[Any], nombre: str, arguments: dict[str, Any]) -> str:
    """Call a read tool expecting failure; return the message the model reads."""
    with pytest.raises(ToolError) as capturado:
        await server_.call_tool(nombre, arguments)
    return str(capturado.value)
