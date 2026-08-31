# Driving the server with the MCP Inspector

> 🇪🇸 [Léelo en español](./inspector.es.md)

The Inspector is the fastest way to see that the security layers are real rather
than described. Everything below is a manual walkthrough of what
`scripts/smoke.py` automates.

```bash
make up            # postgres + backend + authorization server + mcp
make inspector     # opens the Inspector, already carrying a valid token
```

`make inspector` runs the full OAuth 2.1 + PKCE flow first and passes the
resulting bearer token to the Inspector. `make inspector-cli` does the same
without a browser.

## Checklist

Each step below is meant to *fail* in an instructive way. If any of them
succeeds where it should not, the corresponding layer is broken.

### 1 · The server refuses an anonymous client (layer 1)

Point the Inspector at `http://localhost:8080/mcp` with no `Authorization`
header. You get `401`, and the `WWW-Authenticate` header names
`/.well-known/oauth-protected-resource`. Open that URL: it tells you which
authorization server to use. That chain is what lets a client that has never
seen this server authenticate on its own.

### 2 · A `read` token cannot write (layer 2)

```bash
TOKEN=$(uv run python scripts/obtener_token.py --scope "read")
npx -y @modelcontextprotocol/inspector --cli http://localhost:8080/mcp \
  --transport http --header "Authorization: Bearer $TOKEN" \
  --method tools/call --tool-name cancelar_cita \
  --tool-arg cita_id=1 --tool-arg motivo="prueba de scope"
```

The refusal names the missing scope, lists the ones you hold, and tells the
model not to retry with the same token.

### 3 · A `write` token cannot touch clinical data (layer 2)

Same call against `registrar_motivo_consulta` with `--scope "read write"`. Also
refused: the scopes do not nest.

### 4 · A write tool changes nothing on its own (layer 3)

With `--scope "read write"`, call `consultar_disponibilidad`, take a `slot_id`,
then call `agendar_cita`.

The Inspector shows you an **elicitation prompt** rather than a result: the
server answered `input_required`, describing what would happen. Nothing has
changed. Call `consultar_disponibilidad` again: the slot is still free.

### 5 · Only your answer executes (layer 3)

Answer the prompt with `confirmado: true`. The Inspector resends the same call
carrying your answer and the sealed `requestState`, and now the appointment
exists.

Answer `false` instead, on a fresh prompt, and you get `OPERACION_NO_APROBADA`
with nothing touched. Decline the prompt outright and the call aborts the same
way.

The interesting part is what the Inspector never shows you: the confirmation is
**not a parameter of the tool**. Look at the schema in the tools list. There is
no field for it, which is precisely why a model cannot approve on your behalf.

### 6 · Errors tell you what to do (layer 4)

Call `agendar_cita` on the slot you just took. The error names the three closest
free slots, with times and professionals. That is the difference between an
agent that recovers on its own turn and one that loops.

### 7 · Everything is on the record (layer 5)

```bash
docker compose logs mcp | grep tool.invocacion | tail -5
```

One JSON line per call, including the refused ones, each with the subject, the
scope required, and whether a human approved. Note that the document number and
the reason for consultation appear as `«redactado»`: the audit log records that
the call happened, not the patient's data.

### 8 · Clinical access needs consent, not just permission

With `--scope "read write clinical"`, propose `registrar_motivo_consulta` on an
appointment belonging to a patient without recorded consent, and confirm it. It
is refused, every gate open except the patient's own authorisation, which is
the one that must still stop it.

## Connecting Claude Desktop or Cursor

Any MCP client on the 2026-07-28 spec supporting Streamable HTTP and OAuth can
connect to
`http://localhost:8080/mcp`; the client performs the discovery and PKCE flow
itself. For a client without OAuth support, run the stack with
`MCP_AUTH_ENABLED=false`, but note that this disables layers 1 and 2 entirely,
which is only reasonable on a machine you control.
