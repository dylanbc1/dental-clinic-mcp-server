# Manual testing

> 🇪🇸 [Léelo en español](./pruebas-manuales.md)

Thirteen checks so you can evaluate this yourself. Each one says **what to run**
and **what you should see**. If something does not match, that layer is broken.

About 25 minutes total. You need Docker running and `uv` installed.

```bash
git clone https://github.com/dylanbc1/dental-clinic-mcp-server
cd dental-clinic-mcp-server
cp .env.example .env
make up
```

`make up` should finish in about 15 seconds with all four services `healthy`.
The first run takes longer because it builds the images.

---

## Block A · It starts, and it is reproducible (5 min)

### A1 · The quickstart is not a lie

```bash
make down && docker compose down -v      # wipe everything
time make up
```

**Expect:** four healthy containers in under 30 seconds. `make up` migrates,
seeds and starts the API before returning, so if the command finished, the system
answers.

```bash
curl -s localhost:8000/listo
```
→ `{"estado":"listo"}`

### A2 · The data is synthetic and rich enough to be useful

```bash
docker compose exec -T postgres psql -U clinica -d clinica -c "
select regimen, afiliacion_activa, count(*) from paciente group by 1,2 order by 1;
select estado, count(*) from cita group by 1 order by 2 desc;
select count(*) as cupos_libres from agenda_slot where estado='libre';"
```

**Expect:** all four regimes present, some with `afiliacion_activa = f` (with none,
`validar_afiliacion` would have nothing to catch), appointments across all six
states, and over a thousand free slots to book into.

### A3 · The seed is deterministic

```bash
uv run python -m backend.seed --fecha-base 2026-08-31
docker compose exec -T postgres psql -U clinica -d clinica -t -c \
  "select md5(string_agg(documento||nombre, '' order by documento)) from paciente;"

uv run python -m backend.seed --fecha-base 2026-08-31
docker compose exec -T postgres psql -U clinica -d clinica -t -c \
  "select md5(string_agg(documento||nombre, '' order by documento)) from paciente;"
```

**Expect:** the same hash twice. Change `--seed 999` and it must change.

---

## Block B · The five security layers (12 min)

This is where you should be demanding. Every check is designed to **fail**
instructively.

### B1 · Layer 1 · No token, no server

```bash
curl -si -X POST localhost:8080/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize"}' | head -6
```

**Expect:** `HTTP/1.1 401` plus a `WWW-Authenticate: Bearer ...
resource_metadata="..."` header. Without that header a spec-compliant client has
no way to find where to authenticate.

Follow the discovery chain:

```bash
curl -s localhost:8080/.well-known/oauth-protected-resource | python3 -m json.tool
curl -s localhost:9000/.well-known/oauth-authorization-server | python3 -m json.tool
```

**Expect:** the first points at the second. The second declares
`"grant_types_supported": ["authorization_code"]` and
`"code_challenge_methods_supported": ["S256"]`. `implicit`, `password` and
`plain` must **not** appear: OAuth 2.1 removes them.

### B2 · Layer 1 · PKCE is genuinely mandatory

An `/authorize` without PKCE:

```bash
curl -si "localhost:9000/authorize?response_type=code&client_id=clinica-demo\
&redirect_uri=http://localhost:6274/oauth/callback&state=x" | grep -i location
```

**Expect:** the redirect carries `error=invalid_request` saying `code_challenge`
is missing.

And an unregistered `redirect_uri` (a code-exfiltration attempt):

```bash
curl -si "localhost:9000/authorize?response_type=code&client_id=clinica-demo\
&redirect_uri=https://attacker.test/steal&code_challenge=x&code_challenge_method=S256" \
  | head -1
```

**Expect:** `HTTP/1.1 400`, **not** a redirect. Redirecting to an attacker's URI
is an open redirect.

### B3 · Layer 2 · A `read` token cannot write

```bash
TOKEN_READ=$(uv run python scripts/obtener_token.py --scope "read")

npx -y @modelcontextprotocol/inspector --cli http://localhost:8080/mcp \
  --transport http --header "Authorization: Bearer $TOKEN_READ" \
  --method tools/call --tool-name cancelar_cita \
  --tool-arg cita_id=1 --tool-arg motivo="manual check"
```

**Expect:** `SCOPE_INSUFICIENTE`, naming the missing scope (`write`), the ones you
hold (`['read']`), and the line *"No vuelvas a llamar esta herramienta con el
token actual"*. That last detail is what stops an agent looping.

### B4 · Layer 2 · Scopes do not nest

```bash
TOKEN_RW=$(uv run python scripts/obtener_token.py --scope "read write")

npx -y @modelcontextprotocol/inspector --cli http://localhost:8080/mcp \
  --transport http --header "Authorization: Bearer $TOKEN_RW" \
  --method tools/call --tool-name registrar_motivo_consulta \
  --tool-arg cita_id=1 --tool-arg motivo="tooth pain"
```

**Expect:** refused. Holding `write` grants no clinical access. This is the design
decision that most looks like an oversight and is not: scheduling and diagnosing
are different kinds of authority, not different amounts of the same one.

### B5 · Layer 3 · A write does not write

```bash
MCP="npx -y @modelcontextprotocol/inspector --cli http://localhost:8080/mcp --transport http"

# A real patient and a real slot. The ids depend on database state, so do not
# invent them: ask for them.
$MCP --header "Authorization: Bearer $TOKEN_RW" \
  --method tools/call --tool-name buscar_paciente --tool-arg nombre=a --tool-arg limite=1

$MCP --header "Authorization: Bearer $TOKEN_RW" \
  --method tools/call --tool-name consultar_disponibilidad --tool-arg limite=1

# Book with the ids you saw
$MCP --header "Authorization: Bearer $TOKEN_RW" \
  --method tools/call --tool-name agendar_cita \
  --tool-arg paciente_id=PACIENTE_ID --tool-arg slot_id=SLOT_ID
```

**Expect:** `"requiere_confirmacion": true`, an `esto_va_a_pasar` list, and a
`token_confirmacion`. **Nothing was booked.** Ask for availability again: the slot
is still free.

Look at the `resumen`: it names the time and the professional, not the
`slot_id`. Read aloud to a receptionist, "slot 412" means nothing.

Confirm it in the database:

```bash
docker compose exec -T postgres psql -U clinica -d clinica -t -c \
  "select estado from agenda_slot where id = SLOT_ID;"
```
→ `libre`

### B6 · Layer 3 · The confirmation token survives tampering

```bash
# 1. Actually execute
npx -y @modelcontextprotocol/inspector --cli http://localhost:8080/mcp \
  --transport http --header "Authorization: Bearer $TOKEN_RW" \
  --method tools/call --tool-name confirmar_operacion \
  --tool-arg token_confirmacion="THE_TOKEN"
```
→ `"ejecutada": true`. The appointment now exists.

```bash
# 2. Replay it
```
→ `APROBACION_YA_USADA`

```bash
# 3. Change one character of the token and retry
```
→ `APROBACION_INVALIDA`

```bash
# 4. Wait 5 minutes with a fresh unconfirmed proposal, then confirm it
```
→ `APROBACION_EXPIRADA`

**Why the order matters:** try step 3 with a token that is *also* expired. It must
say `APROBACION_INVALIDA`, not `APROBACION_EXPIRADA`. Telling an attacker their
forged token "expired" confirms the payload parsed.

### B7 · Layer 4 · Errors tell you what to do

Try booking the slot you just took:

```bash
npx -y @modelcontextprotocol/inspector --cli http://localhost:8080/mcp \
  --transport http --header "Authorization: Bearer $TOKEN_RW" \
  --method tools/call --tool-name agendar_cita \
  --tool-arg paciente_id=2 --tool-arg slot_id=SAME_SLOT
```

**Expect:** `SLOT_NO_DISPONIBLE` listing the three closest free slots with times
and professionals. Compare with what 92% of the ecosystem returns: `500`.

Note *when* it fails: at proposal time, not on confirmation. Asking a person to
approve an operation that cannot succeed is worse than an error. The same holds
if the patient already has an appointment at that hour, or if the slot's
specialty is not the one you asked for. The check runs again at execution,
because the state can change in between.

And check a real bug leaks nothing:

```bash
curl -s localhost:8000/citas/999999 | python3 -m json.tool
```

**Expect:** JSON with `codigo`, `mensaje` and `sugerencia`. No `Traceback`, no
SQL, no internal class names.

### B8 · Layer 5 · The audit records without copying

```bash
docker compose logs mcp | grep tool.invocacion | tail -5
```

**Expect:** one JSON line per call, **refusals included**, with `sujeto`,
`scope_requerido`, `resultado` and `con_aprobacion_humana`. Note that `documento`
and `motivo` show as `«redactado»`: the log records that the call happened, not
the patient's data.

Check it on purpose:

```bash
docker compose logs mcp | grep -c "manual check"   # the motivo you sent in B3
docker compose logs mcp | grep -c "redactado"
```

The first must be `0` and the second above `0`.

And the appointment history in the database:

```bash
docker compose exec -T postgres psql -U clinica -d clinica -c \
  "select estado_anterior, estado_nuevo, usuario, momento from cita_historial
   order by id desc limit 5;"
```

**Expect:** `usuario` is the token's subject (`recepcion@clinica.local`), not
`system` or `mcp-server`. An audit trail with the same user in every row is not
an audit trail.

### B9 · Layer 5 · DNS rebinding

```bash
curl -si -X POST localhost:8080/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Origin: http://malicious-site.test' \
  -H "Authorization: Bearer $TOKEN_RW" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize"}' | head -1
```

**Expect:** refused. Without this guard a page you visit in your browser can reach
a server bound to `127.0.0.1` and drive it. This is the attack that makes "it only
listens on localhost" a false comfort.

---

## Block C · The domain is real (5 min)

### C1 · Debt warns, it does not block

The seed includes balances carried over from before the agenda window, because
charges fall due 30 days after the visit and the agenda only reaches two weeks
back. Without them every patient would read `al_dia` and this rule would have
nothing to demonstrate itself on.

Find a patient in arrears and book them an appointment:

```bash
docker compose exec -T postgres psql -U clinica -d clinica -t -c \
  "select paciente_id, sum(monto)::int from cargo
   where estado='pendiente' and vencimiento < current_date
   group by 1 order by 2 desc limit 1;"
```

Book with that `paciente_id`. **Expect:** the proposal carries a warning
`"...en mora... No impide agendar"` and a valid `token_confirmacion`. The
appointment goes through. Clinics do not refuse care over an unpaid copayment.

### C2 · A lapsed affiliation changes the tariff, not the access

```bash
docker compose exec -T postgres psql -U clinica -d clinica -t -c \
  "select id from paciente where afiliacion_activa=false and regimen<>'particular' limit 1;"
```

Call `validar_afiliacion` with that id. **Expect:** `regimen_efectivo:
"particular"`, `bloquea_agendamiento: false`, and a suggestion about reactivating
with the EPS.

### C3 · The state machine allows no shortcuts

On an appointment in `agendada`, propose and confirm `registrar_asistencia` with
`estado=atendida`, skipping `confirmada` and `en_espera`.

**Expect:** `TRANSICION_INVALIDA` at **proposal** time, listing which transitions
would be valid. Ask `consultar_cita` first: the `transiciones_validas` field says
exactly what can happen next, which is the same information the model uses to
pick its next tool.

The check also runs at execution, and that is where it really counts: if someone
cancels the appointment between your proposal and your confirmation, the domain
refuses anyway. **Human approval does not make an illegal operation legal.**

### C4 · Double-booking is impossible

```bash
uv run pytest tests/integration/test_concurrencia.py -v 2>&1 | tail -20
```

**Expect:** 15 green tests. The key one is
`test_dos_agentes_sobre_el_mismo_cupo_solo_uno_gana`: two real Postgres
connections racing, one wins and the other gets a clean conflict. An
application-level check loses that race every time.

---

## Block D · The automated paths (3 min)

### D1 · The full suite

```bash
make test-fast
```

**Expect:** over 800 green tests, coverage ≥95% (currently 99%).

### D2 · The whole client path

```bash
make smoke
```

**Expect:** nine green steps, from the 401 to the clinical write refused on scope.

### D3 · Swapping the authorization server

```bash
make keycloak          # about a minute the first time
make keycloak-verify
```

**Expect:** a Keycloak-issued token works against the server on :8081, and **each
server returns 401 for the other's token**. That refusal is the audience binding
working.

---

## How to judge what you saw

If you want to be hard on the project, these are the questions I would ask:

| Question | Where to look |
|---|---|
| Is the security real or just comments? | B1 through B9. Every one fails verifiably. |
| Is the domain authentic or invented? | C1 and C2: debt that does not block, and affiliation that only changes the tariff, are sector rules rather than a programmer's. |
| Do the tests prove anything? | `tests/security/test_scopes.py` enumerates all 39 combinations, it does not sample. `test_concurrencia.py` uses two real connections. |
| Does it know its own limits? | `docs/security.md`, "Known limits": in-memory AS, in-process rate limiter, unsigned `X-Actor`. Stated, not hidden. |
| Does it work outside the author's machine? | `make up` from scratch, and the CI `e2e` job does exactly the same. |

To clean up:

```bash
make down && docker compose down -v
```
