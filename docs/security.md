# Security

> 🇪🇸 [Léelo en español](./security.es.md)

## Why this section exists

Public audits of the MCP ecosystem in 2026 found that, of 22,000+ listed
servers, **40% require no authentication**, **79% handle credentials in
plaintext**, and **only 8.5% implement OAuth**. In January 2026 Anthropic's own
reference server carried three CVEs: path traversal, arbitrary file deletion
and RCE. The bar is on the floor; clearing it is a deliberate act.

The concrete failure this design defends against: in July 2025 an AI agent
deleted a production database at SaaStr during a code freeze. It held write
permissions it never needed, and nobody could revoke them granularly.

The important detail is that **the agent's token was valid**. Authentication
would not have stopped it. Scopes alone would not have stopped it either, if the
scope had been granted. What was missing was a human between intent and effect.
That is why layers 2 and 3 exist, and why they are different controls rather
than one.

## The regulatory line

Booking an appointment is **not** a medical act. Recording a *reason for
consultation* is: it is clinical data, and it brings Resolución 2654/2019
(telehealth), informed consent, and RNBD registration with the SIC into scope.

That boundary is not decoration. It is the reason the tool catalogue splits into
`read` / `write` / `clinical` rather than `read` / `write`, and the reason
`registrar_motivo_consulta` requires recorded consent *in addition to* scope and
approval. A regulator's question is not "was the caller authorised?" but "did
the patient consent, and who touched the data?", so the system answers both.

## The five layers

| # | Layer | Where |
|---|---|---|
| 1 | OAuth 2.1 + PKCE, no API keys anywhere | `mcp_server/auth.py`, `mcp_server/oauth/` |
| 2 | Per-tool scopes (`read`/`write`/`clinical`) | `mcp_server/auth.py` |
| 3 | Human-in-the-loop on every mutation | `mcp_server/aprobacion.py` |
| 4 | Structured, actionable errors | `backend/domain/errores.py`, `mcp_server/errores.py` |
| 5 | Audit trail + transport guards | `mcp_server/auditoria.py`, `mcp_server/limites.py`, `backend/models.py` |

---

### Layer 1, OAuth 2.1 + PKCE

The MCP server is a **resource server**. It issues nothing; it verifies tokens
against the authorization server's published JWKS. There is no API key in this
project, no configuration file that could hold one, and no code path that reads
a shared secret.

The discovery handshake is the standard one, and it is what makes the server
usable by a client that has never seen it before:

```
client ──POST /mcp────────────────────────────────▶ 401
       ◀──── WWW-Authenticate: Bearer resource_metadata="…"
       ──GET /.well-known/oauth-protected-resource─▶ { authorization_servers: [ … ] }
       ──GET /.well-known/oauth-authorization-server▶ { authorization_endpoint, token_endpoint, jwks_uri }
       ──GET /authorize?code_challenge=…&method=S256▶ 302 ?code=…
       ──POST /token  code + code_verifier ────────▶ { access_token }
```

The in-repo authorization server **enforces** what OAuth 2.1 requires rather
than merely advertising it, and each of these has a test:

- PKCE is mandatory and `S256`-only; `plain` is refused.
- The implicit and resource-owner-password grants do not exist.
- Authorization codes are single-use and expire in 60 seconds; a replayed code
  burns the original.
- A `redirect_uri` that was not registered gets a `400`, never a redirect:   redirecting to an attacker-supplied URI is an open redirect and a code
  exfiltration path.
- Unknown scopes are refused rather than silently dropped, so a client that asks
  for `admin` learns it does not exist.

Token verification checks four things, and all four matter:

| Check | What it stops |
|---|---|
| Signature (RS256 against JWKS) | Forged tokens |
| `exp` | Replay of an old token |
| `iss` | A correctly-signed token from a different authorization server |
| `aud` | **The confused deputy**: a token minted for another resource server, replayed here |

The verifier pins `RS256`, so the classic `alg: none` substitution never gets a
chance. A failed verification returns nothing rather than a reason:
distinguishing "expired" from "bad signature" for an unauthenticated caller is
free reconnaissance.

> **Pluggable by construction, and verified.** `docker compose --profile keycloak
> up` starts a real Keycloak realm carrying the same three scopes *and a second
> MCP server that trusts it*, side by side with the original. Same image, same
> code; only `OAUTH_ISSUER` and `OAUTH_JWKS_URL` differ.
> `scripts/verificar_keycloak.py` gets a Keycloak token, uses it, and then shows
> each server returning `401` for the other's token.
>
> Building it surfaced the lesson worth keeping: **Keycloak does not put a
> resource-server audience in the token unless you configure a mapper.** Because
> this verifier *requires* `aud`, the swap failed loudly until the mapper was
> added, which is the correct outcome. A resource server that accepts an
> audience-less token accepts every token that IdP ever issued, to anyone, and
> that is precisely the confused deputy.

### Layer 2, per-tool scopes

Three scopes. Every tool declares exactly one.

```
read      lookups, no side effects
write     anything that mutates the agenda or the ledger
clinical  the single tool that touches clinical data
```

**The scopes do not nest.** `write` does not imply `read`; `clinical` does not
imply `write`. This is the design decision most likely to look like an oversight
and is deliberate: administrative and clinical are different kinds of authority, not
different amounts of it. An agent that schedules appointments has no business
reading a symptom, and an agent transcribing symptoms has no business cancelling
a visit. Least privilege is the smallest authority that does the job, not a
ladder.

The whole matrix, 13 tools × 3 scopes, 39 combinations, is enumerated in
`tests/security/test_scopes.py`. Not sampled: enumerated. A permission suite
written by example always grows a hole.

Two subtleties the tests pin down:

- **Denial happens before any lookup.** A refusal must not depend on the data,
  and must not leak whether the record exists.
- **The scope is checked on both rounds.** MRTR splits a mutation across two
  calls, and the resolver runs on each of them. A token that held `clinical`
  when the question was asked, and lost it before the answer came back, cannot
  execute. Authority is verified at the moment of effect, not only at the moment
  of intent. A token minted while the caller
  held `clinical` will not execute if that scope has since been revoked.

### A proposal a human cannot act on

Before proposing, a write tool checks what it can: that the slot is still free
and in the future, that its specialty matches, that the patient has no other
appointment at that hour, and that the state transition is legal. All of it via
the same backend validation the booking path runs, so both refuse for exactly
the same reasons.

The alternative is asking someone to approve an operation that will fail on
confirmation, which trains people to approve without reading. The checks are
repeated at execution because the state can change in between, and that second
one is the authoritative one.

Refusals during that validation are audited like everything else. A log that
records only successful proposals cannot tell you an agent spent an hour
proposing something impossible.

### Layer 3, human-in-the-loop over MRTR

Every write and clinical tool pauses for a person. The 2026-07-28 spec expresses
that without a persistent connection, through Multi Round-Trip Requests:

```
client ──tools/call cancelar_cita {cita_id: 412, motivo: "…"}──▶
       ◀── input_required
           inputRequests: "Cancelar la cita 412 de Ana Gómez del 3 sep 09:00.
                           Esto va a pasar: … ¿Confirmas?"
           requestState:  v1.ZZs-yBzkr3f…   (sealed)

           a person reads it and answers

client ──tools/call cancelar_cita {same args, inputResponses, requestState}──▶
       ◀── complete
```

One tool, two calls, no session. The paused operation lives in the client's
hands, sealed, which is why any replica can serve either round.

**The confirmation is not a parameter the model can fill.** It is resolved by
the client, so it never appears in the tool's input schema. A model cannot
approve on the user's behalf, because there is no field for it to write into.

**What protects the second round** is `requestState`, sealed by the SDK with
AES-256-GCM:

| Property | Attack it defeats |
|---|---|
| Encrypted, not merely signed | Reading the operation, the patient id or the caller out of a state the client is holding |
| Bound to the request | Redeeming an approval for one operation against a different one, or with different arguments |
| Bound to the principal | Redeeming someone else's approval |
| Time-limited | An approval granted this morning authorising an action tonight |
| Key ring, `keys[0]` seals and all keys unseal | Rotation without downtime, and without a window where outstanding approvals break |

**The resolver runs on both rounds.** Authorisation, scope, and every domain
check are re-applied when the client retries, so a token that lost a scope in
between cannot execute, and an appointment cancelled by someone else in between
is refused. A confirmation authorises an action; it does not freeze the world it
saw, and it does not make an illegal operation legal.

**A refusal never reaches the person.** An unauthorised caller, or an operation
that cannot succeed, is turned away before anyone is asked to approve it. Asking
someone to approve something that will fail trains them to approve without
reading, which quietly disables the whole layer.

Nothing about this needs server-side state, which is why the earlier design's
in-process store of spent approvals is gone along with the limitation it carried.

### Layer 4, structured errors

Every modelled failure carries a stable code, a message, and the part almost
nobody implements: an **actionable** next step.

```json
{
  "error": true,
  "codigo": "SLOT_NO_DISPONIBLE",
  "mensaje": "El cupo del 2026-09-03 09:00 ya no está libre.",
  "sugerencia": "Los cupos libres más cercanos son: 2026-09-03 09:30 (Dra. Ospina), 2026-09-03 11:00 (Dr. Cadena).",
  "detalles": { "slot_id": 88, "alternativas": [{ "slot_id": 91 }, { "slot_id": 96 }] }
}
```

This is not cosmetic. A model that receives `500 Internal Server Error` retries
blindly and burns tokens; a model that receives the payload above recovers on
its own turn. Enforced by tests:

- Every domain error maps to a `4xx`. A modelled failure is the caller's to fix;
  mapping one to `5xx` would claim the server broke.
- No two error classes share a code, the codes are part of the tool contract.
- **One envelope, everywhere.** FastAPI's own 422 body (`{"detail": [...]}`) is a
  second error shape. It is remapped, because two shapes force the caller to
  branch on which one it got.
- An unexpected exception, meaning a genuine bug, is logged in full and answered
  with one opaque structured error. A stack trace never reaches the model.
- Permission failures are worded to stop a retry loop: *"No vuelvas a llamar
  esta herramienta con el token actual: el resultado será el mismo."*

### Layer 5, audit trail and transport guards

**Two separate records**, and conflating them is a common mistake:

- **State changes** live in `cita_historial`, append-only (it has no
  `actualizada_en` column, by design) and written inside the same transaction as
  the change. An audit gap cannot occur. This is the record a regulator asks for.
- **Tool invocations** live in the structured JSON log: who called what, with
  which scope, whether it was approved, whether it succeeded, including the
  calls that were refused. A log that records only successes cannot tell you an
  agent spent an hour failing against a scope it does not have.

Clinical access gets its own event type (`clinico.acceso`), because Res. 2654
asks who touched clinical data and burying that in the generic stream makes it
unanswerable at audit time.

**Nothing sensitive is duplicated into the log.** The reason for consultation,
phone numbers, document numbers, names and confirmation tokens are redacted. An
audit log is not an excuse to copy patient data into a second, less protected
place, and a logged approval token is a replayable approval.

**Transport guards:**

- *Host and Origin validation* (anti DNS-rebinding). Without it, a page the user
  visits in a browser can reach a server bound to `127.0.0.1` and drive it. This
  is the attack that makes "it only listens on localhost" a false comfort. The
  allow-list is explicit, no wildcards, and bare hostnames are automatically
  expanded with the port, because the header a browser sends is
  `localhost:8080`, not `localhost`. (A bare allow-list silently rejects every
  legitimate request, which is how this guard usually ends up disabled.)
- *Rate limiting*, sliding-window, keyed by authenticated subject and falling
  back to client address. It protects the clinic's database from an agent stuck
  in a retry loop, the ordinary failure rather than the adversarial one, so the
  429 carries `Retry-After` and tells the caller to read the last error instead
  of repeating the call. A fixed window would allow twice the intended rate at the
  seam between windows.
- *Streamable HTTP only.* SSE is deprecated for production and is not offered.

## Guarantees that live in the database

Two controls sit in the schema rather than in application code, because an
application check is one a second process walks straight past:

```sql
CREATE UNIQUE INDEX uq_cita_slot_activa ON cita (slot_id)
  WHERE estado IN ('agendada','confirmada','en_espera','atendida');
```

- **Double-booking is impossible.** Two agents both read "slot free" before
  either writes; no `if` can win that race. `tests/integration/test_concurrencia.py`
  runs two live connections against one slot and asserts exactly one survives.
  Optimistic locking on `agenda_slot.version_id` covers concurrent edits to the
  slot itself.
- **Retries are idempotent.** `cita.idempotency_key` is unique, so an agent
  retrying a timed-out booking gets a conflict instead of a second appointment.

## Threat model (STRIDE-lite)

| Threat | Vector | Control | Residual risk |
|---|---|---|---|
| **S**poofing | Forged or replayed access token | RS256 + JWKS, `iss`, `aud`, `exp`; `alg` pinned | AS key compromise. Mitigate by rotating `OAUTH_PRIVATE_KEY_PEM`; the ephemeral dev key invalidates on restart by design |
| **S**poofing | Redeeming another user's approval | `requestState` sealed and bound to the authenticated principal | Key-ring compromise ⇒ rotate `REQUEST_STATE_KEYS`; states live 5 minutes |
| **T**ampering | Editing the arguments of an approved operation | AES-256-GCM over the whole state, bound to the request | None known |
| **T**ampering | Double-booking through a race | Partial unique index + optimistic locking | None at the database level |
| **R**epudiation | "I never cancelled that appointment" | `cita_historial` append-only, actor from the token, same transaction | The backend trusts the `X-Actor` header, acceptable because it is not reachable from outside the compose network; a public deployment must put mTLS or a signed header there |
| **I**nformation disclosure | Clinical data reaching an unauthorised caller | `clinical` scope + recorded consent + never returned by read tools | A caller legitimately holding `clinical` sees the data, that is the point |
| **I**nformation disclosure | Patient data leaking through logs | Redaction of clinical and identifying fields | Log pipeline itself must be protected |
| **I**nformation disclosure | Stack traces or SQL fragments in errors | Single opaque envelope for unexpected failures | None known |
| **D**enial of service | Agent stuck in a retry loop | Sliding-window rate limit; errors worded to stop loops | In-process counter: behind multiple replicas the effective limit multiplies (see below) |
| **D**enial of service | Burning a legitimate approval | Nothing is spent server-side; a failed redemption leaves the state usable by its owner | None known |
| **E**levation of privilege | Over-broad token (the SaaStr shape) | Non-nesting per-tool scopes; scope re-checked at execution | An operator can still grant `clinical` to something that does not need it, a policy problem, not a code one |
| **E**levation of privilege | Browser-driven access to a local server | Host/Origin validation, explicit allow-list | None known |
| **E**levation of privilege | Agent mutating data unilaterally | MRTR: the confirmation is resolved by the client and is not a field the model can fill | A human who approves without reading. Mitigated by writing the question to be read aloud, and by never asking about an operation that would fail |

## Known limits, stated plainly

These are deliberate boundaries of a portfolio project, not oversights:

1. **The authorization server stores state in memory** and auto-approves the
   consent step. It demonstrates that the protocol is understood; it is not
   where your production identities should live. `--profile keycloak` is the
   answer for a real deployment.
2. **The rate limiter is in-process.** Correct for a single replica; behind more
   than one the effective limit multiplies, and it belongs in Redis. The
   interface is narrow enough to swap without touching a tool. Pending approvals
   no longer have this problem: they ride sealed in the client's `requestState`,
   so there is nothing for replicas to share.
3. **The backend trusts `X-Actor`.** It is not reachable from outside the compose
   network, and the MCP server is the only client. A public deployment needs
   mTLS or a signed header on that hop.
4. **The ephemeral signing key is intentional in development.** Every restart
   invalidates every outstanding token, which is exactly what should happen to a
   key nobody chose to persist.

## Handling of secrets

- No credential exists in this repository. `.env.example` documents shape only,
  with values that are obviously local placeholders.
- `alembic.ini` ships an empty `sqlalchemy.url`; the real value is injected from
  `Settings` at runtime, so there is one place to configure it and no tracked
  file that can leak it.
- `REQUEST_STATE_KEYS` ships an obviously-local placeholder that names itself
  `dev-only` and `change-me`. A default that looks like a real secret is a
  default someone ships.
- `mcp_auth_enabled` defaults to **on**. Deriving it from the environment name
  would mean a typo in `APP_ENV` silently disabling authentication.
- Bind addresses default to loopback in code; `0.0.0.0` is a deployment decision
  made explicitly in `docker-compose.yml`.
- The container runs as an unprivileged user (uid 10001).
- CI fails the build if a secret-shaped literal or a PEM private key appears
  anywhere in the source.

## Data

All data is synthetic, generated by Faker with a fixed seed. There is no real
patient information in this project and no code path that could introduce any.
`tests/integration/test_seed.py::TestSinPiiReal` asserts it.
