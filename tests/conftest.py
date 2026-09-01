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

BASE_DATE_TESTS = date(2026, 8, 31)


def _url_from_container() -> tuple[str, object] | None:
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
        container = PostgresContainer("postgres:16-alpine", driver="psycopg")
        container.start()
    except Exception:  # pragma: no cover - no docker daemon on this machine
        return None
    return container.get_connection_url(), container


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    url = os.getenv("TEST_DATABASE_URL")
    if url:
        yield url
        return

    result = _url_from_container()
    if result is None:
        pytest.skip(
            "No hay PostgreSQL para pruebas de integración. "
            "Define TEST_DATABASE_URL o levanta Docker.",
            allow_module_level=False,
        )
    url, container = result
    try:
        yield url
    finally:
        container.stop()  # type: ignore[attr-defined]


@pytest.fixture(scope="session")
def alembic_config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _is_a_test_database(url: str) -> bool:
    """Guard for the schema drop below.

    The engine fixture wipes `public`. Accept only a database whose name marks
    it as disposable, so a mistyped TEST_DATABASE_URL cannot destroy a dev or,
    far worse, a production database.
    """
    name = urlsplit(url).path.lstrip("/").lower()
    return any(mark in name for mark in ("test", "_ci", "pytest")) or name.startswith("tc-")


@pytest.fixture(scope="session")
def engine(database_url: str, alembic_config: Config) -> Iterator[Engine]:
    """Engine over a schema built by the *migrations*, not by ``create_all``.

    Testing against ``create_all`` would let the migrations rot silently; here a
    broken migration fails the whole suite.
    """
    if not _is_a_test_database(database_url):
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
    opened: list[Session] = []

    def open_one() -> Session:
        session_ = factory()
        opened.append(session_)
        return session_

    try:
        yield open_one
    finally:
        for session_ in opened:
            session_.rollback()
            session_.close()
        cleanup = factory()
        try:
            for table in reversed(Base.metadata.sorted_tables):
                cleanup.execute(table.delete())
            cleanup.commit()
        finally:
            cleanup.close()


@pytest.fixture
def minimal_data(sessions: Callable[[], Session]) -> dict[str, int]:
    """One clinic, one dentist, two patients and one free slot, committed."""
    session_ = sessions()
    clinic = Clinic(name="Clínica Test", nit="900.000.001-1", specialty="Odontología")
    session_.add(clinic)
    session_.flush()

    professional = Professional(
        clinic_id=clinic.id,
        name="Dra. Prueba",
        license_number="RM-TEST-1",
        specialty=Specialty.GENERAL_DENTISTRY,
    )
    session_.add(professional)
    session_.flush()

    patients = [
        Patient(
            document_type=DocumentType.CC,
            document_number=f"100000{i}",
            name=f"Paciente {i}",
            phone="+57 3001112233",
            regimen=Regimen.CONTRIBUTIVO,
            affiliation_active=True,
        )
        for i in (1, 2)
    ]
    session_.add_all(patients)
    session_.flush()

    # The clinic's calendar, not the runner's: they differ for five hours a day.
    tomorrow = now_at_clinic().date() + timedelta(days=1)
    while not slots_for_day(tomorrow):
        tomorrow += timedelta(days=1)
    start, end = slots_for_day(tomorrow)[0]
    slot = AgendaSlot(
        professional_id=professional.id,
        day=start.astimezone(UTC).date(),
        start=start,
        end=end,
    )
    session_.add(slot)
    session_.commit()

    return {
        "clinic_id": clinic.id,
        "professional_id": professional.id,
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

    clinic_id: int
    general_id: int
    ortho_id: int
    #: Contributory regime, active affiliation, consent on file.
    ana_id: int
    ana_document: str
    #: Subsidised regime, affiliation lapsed → private tariff.
    bruno_id: int
    #: Private patient, no consent recorded → clinical tool must refuse.
    carla_id: int
    #: In arrears well above the alert threshold.
    debtor_id: int
    slots_general: list[int]
    ortho_slots: list[int]
    past_slot_id: int
    future_date: date


@pytest.fixture
def scenario(sessions: Callable[[], Session]) -> Scenario:
    session_ = sessions()
    clinic = Clinic(name="Clínica Escenario", nit="900.777.111-2", specialty="Odontología")
    session_.add(clinic)
    session_.flush()

    general = Professional(
        clinic_id=clinic.id,
        name="Dra. General",
        license_number="RM-ESC-1",
        specialty=Specialty.GENERAL_DENTISTRY,
    )
    ortho = Professional(
        clinic_id=clinic.id,
        name="Dr. Ortodoncia",
        license_number="RM-ESC-2",
        specialty=Specialty.ORTHODONTICS,
    )
    session_.add_all([general, ortho])
    session_.flush()

    def patient(
        document_number: str,
        name: str,
        regimen: Regimen,
        *,
        active: bool = True,
        consent: bool = True,
    ) -> Patient:
        return Patient(
            document_type=DocumentType.CC,
            document_number=document_number,
            name=name,
            phone="+57 3001112233",
            email=f"{document_number}@ejemplo.test",
            regimen=regimen,
            affiliation_active=active,
            cuota_moderadora_level=1,
            clinical_data_consent=consent,
        )

    ana = patient("11111111", "Ana Gómez Ruiz", Regimen.CONTRIBUTIVO)
    bruno = patient("22222222", "Bruno Díaz Peña", Regimen.SUBSIDIADO, active=False)
    carla = patient("33333333", "Carla Ríos Mora", Regimen.PARTICULAR, consent=False)
    debtor = patient("44444444", "Diego Mora Ruiz", Regimen.CONTRIBUTIVO)
    session_.add_all([ana, bruno, carla, debtor])
    session_.flush()

    # A debt large enough and old enough to trip the alert threshold.
    session_.add(
        Charge(
            patient_id=debtor.id,
            concept=ChargeConcept.PARTICULAR,
            amount=Decimal("180000"),
            description="Tarifa particular vencida",
            status=ChargeState.PENDING,
            due_date=now_at_clinic().date() - timedelta(days=75),
        )
    )

    # A future working day with free slots for both professionals.
    future = now_at_clinic().date() + timedelta(days=3)
    while not slots_for_day(future):
        future += timedelta(days=1)
    ranges = slots_for_day(future)

    slots_general: list[AgendaSlot] = []
    ortho_slots: list[AgendaSlot] = []
    for start, end in ranges[:6]:
        for professional, target in ((general, slots_general), (ortho, ortho_slots)):
            slot = AgendaSlot(
                professional_id=professional.id,
                day=to_clinic_time(start).date(),
                start=start,
                end=end,
            )
            session_.add(slot)
            target.append(slot)

    # One slot in the past, to prove the domain refuses to book backwards.
    past = now_at_clinic().date() - timedelta(days=5)
    while not slots_for_day(past):
        past -= timedelta(days=1)
    past_start, past_end = slots_for_day(past)[0]
    past_slot = AgendaSlot(
        professional_id=general.id,
        day=to_clinic_time(past_start).date(),
        start=past_start,
        end=past_end,
    )
    session_.add(past_slot)
    session_.commit()

    return Scenario(
        clinic_id=clinic.id,
        general_id=general.id,
        ortho_id=ortho.id,
        ana_id=ana.id,
        ana_document=ana.document_number,
        bruno_id=bruno.id,
        carla_id=carla.id,
        debtor_id=debtor.id,
        slots_general=[s.id for s in slots_general],
        ortho_slots=[s.id for s in ortho_slots],
        past_slot_id=past_slot.id,
        future_date=future,
    )


# --------------------------------------------------------------------------- #
# MCP server
# --------------------------------------------------------------------------- #


SUBJECT = "recepcion@clinica.test"

#: Key ring for the sealed request state. Test-only, 32 bytes as the codec wants.
STATE_KEYS = ["clave-de-pruebas-para-request-state-32"]

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
        request_state_keys=STATE_KEYS,
    )


@pytest.fixture
async def backend_client(backend_session: Session) -> AsyncIterator[BackendClient]:
    """A backend client wired to the FastAPI app in-process."""

    def override() -> Iterator[Session]:
        yield backend_session
        backend_session.commit()

    backend_app.dependency_overrides[get_session] = override
    transport = httpx.ASGITransport(app=backend_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://backend") as http:
        yield BackendClient("http://backend", client=http)
    backend_app.dependency_overrides.clear()


@pytest.fixture
def ctx(backend_client: BackendClient) -> ToolContext:
    return ToolContext(client=backend_client, auditor=Auditor(), require_auth=True)


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

    def __init__(self, http: httpx.AsyncClient, *, can_confirm: bool = True) -> None:
        self.http = http
        self.id = 0
        #: A client that does not declare `elicitation` stands in for one on an
        #: older spec, which cannot answer an `input_required`.
        self.can_confirm = can_confirm

    async def _rpc(self, method: str, params: dict[str, Any]) -> Any:
        self.id += 1
        headers = {**BASE_HEADERS, "mcp-method": method}
        if "name" in params:
            headers["mcp-name"] = str(params["name"])
        response = await self.http.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": self.id,
                "method": method,
                "params": {**params, **self._envelope()},
            },
            headers=headers,
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

    def _envelope(self) -> dict[str, Any]:
        capabilities: dict[str, Any] = {"elicitation": {}} if self.can_confirm else {}
        return {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": capabilities,
            }
        }

    @staticmethod
    def _unwrap(result: dict[str, Any]) -> Any:
        if result.get("isError"):
            texts = [c.get("text", "") for c in result.get("content", [])]
            raise ToolCallError("\n".join(t for t in texts if t))
        content = result.get("structuredContent")
        if isinstance(content, dict) and set(content) == {"result"}:
            return content["result"]
        if content is not None:
            return content
        return [c.get("text") for c in result.get("content", [])]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Call a tool that needs no human answer."""
        return self._unwrap(await self._rpc("tools/call", {"name": name, "arguments": arguments}))

    async def ask(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a tool expecting `input_required`, and return what it asks."""
        result = await self._rpc("tools/call", {"name": name, "arguments": arguments})
        if result.get("resultType") != "input_required":
            self._unwrap(result)  # raises if it was an error
            raise AssertionError(f"{name} no pidió confirmación: {result}")
        return result

    async def respond(
        self,
        name: str,
        arguments: dict[str, Any],
        question: dict[str, Any],
        *,
        confirmed: bool = True,
        action: str = "accept",
    ) -> Any:
        """Retry the same call carrying the human's answer."""
        key = next(iter(question["inputRequests"]))
        response: dict[str, Any] = {"action": action}
        if action == "accept":
            response["content"] = {"confirmed": confirmed}
        return self._unwrap(
            await self._rpc(
                "tools/call",
                {
                    "name": name,
                    "arguments": arguments,
                    "inputResponses": {key: response},
                    "requestState": question["requestState"],
                },
            )
        )

    async def approve(self, name: str, arguments: dict[str, Any]) -> Any:
        """The whole round trip: ask, approve, execute."""
        question = await self.ask(name, arguments)
        return await self.respond(name, arguments, question)

    def question_text(self, question: dict[str, Any]) -> str:
        key = next(iter(question["inputRequests"]))
        return str(question["inputRequests"][key]["params"]["message"])


@asynccontextmanager
async def http_server(ctx: ToolContext, settings_: Settings) -> AsyncIterator[MCPTestClient]:
    """A running server over HTTP, for tests that need custom settings."""
    app = build_app(ctx, config=settings_, con_auth=False)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8080") as http:
            yield MCPTestClient(http)


@pytest.fixture
async def mcp_without_elicitation(
    ctx: ToolContext, mcp_settings: Settings
) -> AsyncIterator[MCPTestClient]:
    """A client that cannot ask a person anything, like an older one."""
    app = build_app(ctx, config=mcp_settings, con_auth=False)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8080") as http:
            yield MCPTestClient(http, can_confirm=False)


@pytest.fixture
async def mcp(ctx: ToolContext, mcp_settings: Settings) -> AsyncIterator[MCPTestClient]:
    """The server over HTTP, with the lifespan running."""
    app = build_app(ctx, config=mcp_settings, con_auth=False)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8080") as http:
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
    parts = [getattr(block, "text", "") for block in result.content]
    return "\n".join(p for p in parts if p)


def payload(result: CallToolResult) -> Any:
    """The structured payload of a successful in-process call."""
    assert not result.is_error, text_of(result)
    if result.structured_content is not None:
        content = result.structured_content
        if isinstance(content, dict) and set(content) == {"result"}:
            return content["result"]
        return content
    return json.loads(text_of(result))


async def call_tool(server_: MCPServer[Any], name: str, arguments: dict[str, Any]) -> Any:
    """Call a read tool in-process and return its payload."""
    return payload(await server_.call_tool(name, arguments))


async def error_from(server_: MCPServer[Any], name: str, arguments: dict[str, Any]) -> str:
    """Call a read tool expecting failure; return the message the model reads."""
    with pytest.raises(ToolError) as captured:
        await server_.call_tool(name, arguments)
    return str(captured.value)
