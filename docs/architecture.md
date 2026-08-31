# Architecture

> 🇪🇸 [Léelo en español](./architecture.es.md)

## The three layers

```mermaid
flowchart LR
    subgraph client["MCP client (not built here)"]
        C["Claude Desktop · Cursor · MCP Inspector"]
    end

    subgraph mcp["MCP server · FastMCP · Streamable HTTP"]
        direction TB
        A1["1· OAuth 2.1 + PKCE<br/>identity"]
        A2["2· Scope check<br/>read / write / clinical"]
        A3["3· Human-in-the-loop<br/>signed proposal"]
        T["tools · resources · prompts"]
        A4["4· Structured errors"]
        A5["5· Audit + transport guards"]
        A1 --> A2 --> A3 --> T --> A4
        T --> A5
    end

    subgraph backend["Domain backend · FastAPI"]
        API["Internal REST API"]
        DOM["Domain logic<br/>state machine · cartera · afiliación · lista de espera"]
        DB[("PostgreSQL 16")]
        API --> DOM --> DB
    end

    C -- "Streamable HTTP" --> A1
    T -- "HTTP, server-to-server" --> API

    style mcp fill:#f6f2ff,stroke:#7c5cff
    style backend fill:#f0f7ff,stroke:#3b82f6
```

The LLM never reaches PostgreSQL. Every request crosses the five controls
before a single row is touched.

### Why the backend and the MCP server are separate

| Reason | What it buys |
|---|---|
| Realism | In production an MCP server almost never *is* the system, it wraps one that already exists. Modelling that separation is the honest shape. |
| Security | The security controls have exactly one place to live. There is no path from the model to the database that bypasses them. |
| Reuse | The same backend can feed the web demo (v1.1) or a voice module without rewriting a line of domain logic. |

## Request flow

1. The client calls a tool over Streamable HTTP (SSE is deprecated for production).
2. **Layer 1** validates the OAuth 2.1 access token. Missing or invalid ⇒ `401`
   with a `WWW-Authenticate` header pointing at the protected-resource metadata.
3. **Layer 2** checks the token's scopes against the scope the tool declares.
   A `read` token calling `agendar_cita` is refused here.
4. **Layer 3**, for any `write` or `clinical` tool, returns a *proposal* instead
   of acting: a signed, single-use, TTL-bound token describing exactly what would
   happen. Only `confirmar_operacion` with that token mutates anything, and it
   re-checks the scope of the action named *inside* the token, so authority is
   verified at the moment of effect rather than the moment of intent.
5. The tool calls the backend REST API.
6. **Layer 4** converts every failure into `{codigo, mensaje, sugerencia, detalles}`.
7. **Layer 5** writes the audit row, in the same transaction as the change.

## Domain model

```mermaid
erDiagram
    CLINICA ||--o{ PROFESIONAL : emplea
    PROFESIONAL ||--o{ AGENDA_SLOT : ofrece
    AGENDA_SLOT ||--o| CITA : "ocupada por"
    PACIENTE ||--o{ CITA : agenda
    CITA ||--o{ CITA_HISTORIAL : audita
    CITA ||--o{ CARGO : genera
    PACIENTE ||--o{ CARGO : adeuda
    PACIENTE ||--o{ LISTA_ESPERA : espera
```

### The appointment state machine

```mermaid
stateDiagram-v2
    [*] --> agendada
    agendada --> confirmada
    agendada --> cancelada : exige motivo
    agendada --> reprogramada
    agendada --> no_asistio
    confirmada --> en_espera
    confirmada --> cancelada : exige motivo
    confirmada --> reprogramada
    confirmada --> no_asistio
    en_espera --> atendida
    en_espera --> cancelada : exige motivo
    atendida --> [*]
    cancelada --> [*]
    reprogramada --> [*]
    no_asistio --> [*]
```

Three rules ride on this diagram, all of them enforced in
`backend/domain/estados.py` and exhaustively tested:

- `cancelada` **requires a reason**. A cancellation without one destroys the
  clinic's ability to audit its own no-show rate.
- `cancelada`, `reprogramada` and `no_asistio` **free the slot**; only
  `cancelada` triggers the waiting list, because a reschedule moves the same
  patient and a no-show happens once the slot has already elapsed.
- `atendida` and `no_asistio` **produce a charge** in accounts receivable.

## Decisions worth arguing about

### Store UTC, present America/Bogota

Every persisted timestamp is timezone-aware UTC; `backend/domain/tiempo.py` is
the only place that converts. Naive datetimes are rejected rather than assumed: guessing a timezone is how a schedule silently drifts five hours.

### Double-booking is prevented by the database, not by an `if`

Two agents both read "slot free" before either writes. An application-level
check cannot win that race. A partial unique index over `cita.slot_id`,
restricted to the states that actually hold a slot, means the second booking
fails with a clean conflict. Optimistic locking on `agenda_slot.version_id`
covers concurrent edits to the slot itself.

```sql
CREATE UNIQUE INDEX uq_cita_slot_activa ON cita (slot_id)
  WHERE estado IN ('agendada','confirmada','en_espera','atendida');
```

### Idempotency keys on booking

An agent that retries a timed-out call must get the same appointment back, not a
second one. `cita.idempotency_key` is unique; the retry hits the constraint
instead of creating a duplicate.

### Migrations, not `create_all`

The test-suite builds its schema with `alembic upgrade head`, so a model change
without a migration fails CI. `tests/integration/test_migraciones.py` also
asserts the migrated schema still matches the models and that the migration is
reversible.

## Repository layout

```
backend/            domain source of truth, knows nothing about MCP
  domain/           pure logic: estados, cartera, afiliacion, lista_espera, tiempo, errores
  models.py         SQLAlchemy 2.x schema
  seed.py           deterministic synthetic data (Faker, fixed seed)
  api.py            internal REST API
  migrations/       alembic
mcp_server/
  tools/            read.py · write.py · clinical.py · confirmacion.py
  contexto.py       everything the tools need, injected rather than global
  auth.py           token verification and scopes            (layers 1-2)
  aprobacion.py     signed human-approval tokens             (layer 3)
  errores.py        structured failures for the model        (layer 4)
  auditoria.py      audit log · limites.py  rate limiting    (layer 5)
  cliente.py        HTTP client to the backend
  recursos.py       resources and the receptionist prompt
  oauth/            the in-repo authorization server
tests/
  unit/             pure domain, no database, no docker
  integration/      real PostgreSQL: schema, concurrency, seed, migrations
  contract/         MCP protocol surface, every tool end to end
  security/         scope matrix, approvals, OAuth, transport guards
scripts/            obtener_token.py (PKCE flow) · smoke.py (end-to-end)
docs/
```
