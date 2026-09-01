"""Block E of the manual walkthrough, automated.

Five probes at what the other thirteen checks take for granted, run against the
stack that is actually up. Three of them found real bugs the test suite could
not: concurrency answered with `500`s, an idempotent retry that was not, and a
replayed confirmation offering one freed slot to two patients. They live here so
those three stay fixed against a live system and not only in a fixture.

    make up && make probe

Exits non-zero if any probe fails, so it can gate a release. E1 needs a short
token lifetime to watch expiry happen and says so rather than pretending.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

MCP = "http://localhost:8080/mcp"
ISSUER = "http://localhost:9000"
REDIRECT = "http://localhost:6274/oauth/callback"
CLIENT = "clinic-demo"
ENVELOPE = {
    "_meta": {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {"elicitation": {}},
    }
}
HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2026-07-28",
    "Origin": "http://localhost:8080",
}
#: Failures a race may legitimately produce. Anything else is untyped, which is
#: the thing E4 exists to catch.
TYPED = ("SLOT_UNAVAILABLE", "PATIENT_ALREADY_BOOKED", "CONCURRENCY_CONFLICT")

failures: list[str] = []


def verdict(passed: bool, note: str = "") -> None:
    line = f"  VEREDICTO: {'PASA' if passed else 'FALLA'}" + (f"  ·  {note}" if note else "")
    print(line)
    if not passed:
        failures.append(line)


def token(scope: str = "read write clinical", subject: str = "recepcion@clinica.local") -> str:
    """A token through the real PKCE flow, not a shortcut."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    with httpx.Client(follow_redirects=False, timeout=20) as client:
        authorized = client.get(
            f"{ISSUER}/authorize",
            params={
                "response_type": "code",
                "client_id": CLIENT,
                "redirect_uri": REDIRECT,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "scope": scope,
                "state": secrets.token_urlsafe(8),
                "login_hint": subject,
            },
        )
        code = parse_qs(urlparse(authorized.headers["location"]).query)["code"][0]
        granted = client.post(
            f"{ISSUER}/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT,
                "client_id": CLIENT,
                "code_verifier": verifier,
            },
        )
        return str(granted.json()["access_token"])


def rpc(access: str, method: str, params: dict[str, Any]) -> Any:
    """One JSON-RPC call, with the headers the transport guards require."""
    guard = {"mcp-method": method}
    if "name" in params:
        guard["mcp-name"] = str(params["name"])
    with httpx.Client(timeout=60) as client:
        response = client.post(
            MCP,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": {**params, **ENVELOPE}},
            headers={**HEADERS, "Authorization": f"Bearer {access}", **guard},
        )
        if response.status_code >= 400:
            return {"__http__": response.status_code}
        body = response.text
        for line in body.splitlines():
            if line.startswith("data:"):
                body = line[5:].strip()
                break
        payload = json.loads(body)
        return payload.get("result", payload)


def sql(query: str) -> str:
    """Read straight from the database, to check what the API claimed."""
    completed = subprocess.run(  # noqa: S603
        [
            "/usr/bin/env",
            "docker",
            "compose",
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "clinic",
            "-d",
            "clinic",
            "-At",
            "-c",
            query,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip()


def message_of(result: dict[str, Any]) -> str:
    return "\n".join(block.get("text", "") for block in result.get("content", []))


def code_of(result: dict[str, Any]) -> str:
    if "__http__" in result:
        return f"HTTP {result['__http__']}"
    if not result.get("isError"):
        return "OK"
    text = message_of(result)
    # The code is the FIRST bracketed token: "[SLOT_UNAVAILABLE] ...". Taking
    # the last one picked up the JSON array of alternatives inside the details.
    return text.split("[", 1)[-1].split("]", 1)[0] if "[" in text else "ERROR"


def appointment_of(result: dict[str, Any]) -> Any:
    content = result.get("structuredContent", {})
    return content.get("result", content).get("appointment", {}).get("id")


def answer_for(paused: dict[str, Any]) -> dict[str, Any]:
    key = next(iter(paused["inputRequests"]))
    return {key: {"action": "accept", "content": {"confirmed": True}}}


def bookable_pair(access: str, skip: set[int]) -> tuple[int, int, dict[str, Any]]:
    """A patient and slot that genuinely pause for confirmation.

    Not every pair does: an overlapping appointment or a taken slot is refused
    during validation, before anyone is asked. Taking the first of each and
    hoping is how a probe ends up measuring something else.
    """
    people = rpc(
        access, "tools/call", {"name": "search_patients", "arguments": {"name": "a", "limit": 25}}
    )
    slots = rpc(access, "tools/call", {"name": "check_availability", "arguments": {"limit": 25}})
    if "structuredContent" not in slots:
        raise SystemExit(f"check_availability failed: {json.dumps(slots)[:200]}")
    for slot in slots["structuredContent"]["result"]:
        slot_id = int(slot.get("slot_id") or slot["id"])
        if slot_id in skip:
            continue
        for person in people["structuredContent"]["result"]:
            patient_id = int(person.get("patient_id") or person["id"])
            paused = rpc(
                access,
                "tools/call",
                {
                    "name": "book_appointment",
                    "arguments": {"patient_id": patient_id, "slot_id": slot_id},
                },
            )
            if paused.get("resultType") == "input_required":
                return patient_id, slot_id, paused
    raise SystemExit("no bookable pair left; run `make reset`")


def probe_expired_token() -> None:
    print("\n" + "=" * 72)
    print("E1 · TOKEN VENCIDO (no un scope equivocado: exp)")
    print("=" * 72)
    lifetime = _oauth_token_lifetime()
    if lifetime > 30:
        print(f"  OMITIDA: el token vive {lifetime}s. Para verla, arranca el AS con")
        print("           OAUTH_ACCESS_TOKEN_TTL_SECONDS=3 docker compose up -d oauth --wait")
        return
    print("  esperado: 401 con WWW-Authenticate; ni 500 ni un resultado")
    access = token()
    time.sleep(lifetime + 3)
    with httpx.Client(timeout=30) as client:
        response = client.post(
            MCP,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": ENVELOPE},
            headers={**HEADERS, "Authorization": f"Bearer {access}", "mcp-method": "tools/list"},
        )
    challenge = response.headers.get("www-authenticate", "")
    print(f"  obtenido: {response.status_code}   WWW-Authenticate: {challenge[:70]}")
    verdict(response.status_code == 401 and "Bearer" in challenge)


def _oauth_token_lifetime() -> int:
    """Read the lifetime off a real token rather than off configuration."""
    access = token(scope="read")
    claims = json.loads(base64.urlsafe_b64decode(access.split(".")[1] + "=="))
    return int(claims["exp"] - claims["iat"])


def probe_replayed_confirmation(access: str) -> int:
    print("\n" + "=" * 72)
    print("E2 · CONFIRMACION REPRODUCIDA TRAS EXITO")
    print("=" * 72)
    patient_id, slot_id, paused = bookable_pair(access, set())
    args = {"patient_id": patient_id, "slot_id": slot_id}
    answer = answer_for(paused)
    before = int(sql("select count(*) from appointment"))
    first = rpc(
        access,
        "tools/call",
        {
            **{"name": "book_appointment", "arguments": args},
            "requestState": paused["requestState"],
            "inputResponses": answer,
        },
    )
    rpc(
        access,
        "tools/call",
        {
            **{"name": "book_appointment", "arguments": args},
            "requestState": paused["requestState"],
            "inputResponses": answer,
        },
    )
    after = int(sql("select count(*) from appointment"))
    print("  esperado: rechazo o idempotencia; nunca un segundo efecto")
    print(f"  obtenido: cita {appointment_of(first)}; citas {before} -> {after}")
    verdict(after - before == 1, "el indice unico es el que lo frena, no el estado sellado")
    return slot_id


def probe_idempotent_retry(access: str, skip: set[int]) -> None:
    print("\n" + "=" * 72)
    print("E3 · IDEMPOTENCIA BAJO REINTENTO REAL")
    print("=" * 72)
    patient_id, slot_id, _ = bookable_pair(access, skip)
    key = f"probe-{secrets.token_hex(6)}"
    args = {"patient_id": patient_id, "slot_id": slot_id, "idempotency_key": key}

    def book() -> dict[str, Any]:
        paused: dict[str, Any] = rpc(
            access, "tools/call", {"name": "book_appointment", "arguments": args}
        )
        if paused.get("resultType") != "input_required":
            return paused
        executed: dict[str, Any] = rpc(
            access,
            "tools/call",
            {
                "name": "book_appointment",
                "arguments": args,
                "requestState": paused["requestState"],
                "inputResponses": answer_for(paused),
            },
        )
        return executed

    first, again = book(), book()
    rows = sql(f"select count(*) from appointment where idempotency_key='{key}'")  # noqa: S608
    print("  esperado: el mismo appointment_id dos veces; en base, una fila")
    print(f"  obtenido: {appointment_of(first)} y {appointment_of(again)}; filas={rows}")
    verdict(
        appointment_of(first) is not None
        and appointment_of(first) == appointment_of(again)
        and rows == "1"
    )


def probe_concurrent_bookings(access: str) -> None:
    print("\n" + "=" * 72)
    print("E4 · 10 RESERVAS CONCURRENTES SOBRE EL MISMO CUPO, POR HTTP")
    print("=" * 72)
    _, slot_id, _ = bookable_pair(access, set())
    people = rpc(
        access, "tools/call", {"name": "search_patients", "arguments": {"name": "a", "limit": 25}}
    )
    patients = [int(p.get("patient_id") or p["id"]) for p in people["structuredContent"]["result"]][
        :10
    ]
    while len(patients) < 10:
        patients.append(patients[0])

    def full_flow(patient_id: int) -> str:
        args = {"patient_id": patient_id, "slot_id": slot_id}
        paused = rpc(access, "tools/call", {"name": "book_appointment", "arguments": args})
        if paused.get("resultType") != "input_required":
            return code_of(paused)
        return code_of(
            rpc(
                access,
                "tools/call",
                {
                    "name": "book_appointment",
                    "arguments": args,
                    "requestState": paused["requestState"],
                    "inputResponses": answer_for(paused),
                },
            )
        )

    with ThreadPoolExecutor(max_workers=10) as pool:
        outcomes = list(pool.map(full_flow, patients))
    live = int(
        sql(
            # Interpolating an integer we chose, into a read-only probe: not a
            # query shape any caller can reach.
            f"select count(*) from appointment where slot_id={slot_id} "  # noqa: S608
            "and status in ('scheduled','confirmed','waiting','attended')"
        )
    )
    untyped = [o for o in outcomes if o != "OK" and not any(t in o for t in TYPED)]
    print("  esperado: un solo exito, el resto tipado y accionable, ningun 500")
    print(f"  obtenido: {dict(Counter(outcomes))}")
    print(f"  citas vivas en el cupo {slot_id}: {live}   respuestas sin tipar: {untyped}")
    verdict(outcomes.count("OK") == 1 and live == 1 and not untyped)


def probe_tenant_isolation(access: str) -> None:
    print("\n" + "=" * 72)
    print("E5 · AISLAMIENTO ENTRE TENANTS (autorizacion horizontal)")
    print("=" * 72)
    del access
    clinics = sql("select count(*) from clinic")
    scoped = sql(
        "select string_agg(table_name,',') from information_schema.columns "
        "where table_schema='public' and column_name='clinic_id'"
    )
    outsider = token(subject="intruso@otra-clinica.local")
    read = rpc(
        outsider, "tools/call", {"name": "search_patients", "arguments": {"name": "a", "limit": 1}}
    )
    found = read.get("structuredContent", {}).get("result", [])
    print(f"  clinicas: {clinics}   tablas con clinic_id: {scoped or '(ninguna)'}")
    print("  esperado: o rechazo, o un limite documentado")
    print(f"  obtenido: un sujeto de otra clinica leyo {len(found)} paciente(s)")
    print("  VEREDICTO: NO HAY TENANT. Alcance, no defecto, y esta documentado")
    print("             como limite conocido en docs/pruebas-manuales.md, E5.")


def main() -> int:
    probe_expired_token()
    access = token()
    used = probe_replayed_confirmation(access)
    probe_idempotent_retry(access, {used})
    probe_concurrent_bookings(access)
    probe_tenant_isolation(access)
    print("\n" + "=" * 72)
    print(f"{'TODAS PASAN' if not failures else str(len(failures)) + ' FALLAN'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
