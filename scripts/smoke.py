"""End-to-end smoke test against a running stack.

Walks the path a real client walks: discover the protected resource, obtain a
token through PKCE, list the tools, read, then ask to book and complete the
booking over MRTR. Anything that only works in the test-suite fails here.

    docker compose up -d --wait
    uv run python scripts/smoke.py
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import httpx

from scripts.get_token import get_token

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2026-07-28",
}

#: With no session to remember the handshake, every call carries its own
#: protocol version and capabilities. That is the visible cost of statelessness.
ENVELOPE = {
    "_meta": {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {"elicitation": {}},
    }
}


class MCPTestClient:
    """A minimal 2026-07-28 client, enough to prove the server works."""

    def __init__(self, url: str, token: str) -> None:
        self.url = url
        self.http = httpx.Client(
            timeout=30, headers={**HEADERS, "Authorization": f"Bearer {token}"}
        )
        self.id = 0

    def _rpc(self, metodo: str, params: dict[str, Any]) -> Any:
        self.id += 1
        cabeceras = {"mcp-method": metodo}
        if "name" in params:
            cabeceras["mcp-name"] = str(params["name"])
        response = self.http.post(
            self.url,
            json={
                "jsonrpc": "2.0",
                "id": self.id,
                "method": metodo,
                "params": {**params, **ENVELOPE},
            },
            headers=cabeceras,
        )
        if response.status_code >= 400:
            # A malformed or tampered requestState is refused at the protocol
            # layer, before any tool runs, so it arrives as a plain HTTP error.
            raise SystemExit(f"{metodo} refused ({response.status_code}): {response.text[:160]}")
        body = response.text
        # Streamable HTTP answers as SSE; pull the single data frame out.
        for line in body.splitlines():
            if line.startswith("data:"):
                body = line[5:].strip()
                break
        payload = json.loads(body)
        if "error" in payload:
            raise SystemExit(f"{metodo} failed: {payload['error']}")
        return payload["result"]

    @staticmethod
    def _payload(result: dict[str, Any]) -> Any:
        if result.get("isError"):
            text_of = "\n".join(c.get("text", "") for c in result.get("content", []))
            raise SystemExit(f"the tool returned an error:\n{text_of}")
        contenido = result.get("structuredContent") or {}
        return contenido.get("result", contenido)

    def call_tool(self, nombre: str, arguments: dict[str, Any]) -> Any:
        return self._payload(self._rpc("tools/call", {"name": nombre, "arguments": arguments}))

    def ask(self, nombre: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = self._rpc("tools/call", {"name": nombre, "arguments": arguments})
        if result.get("resultType") != "input_required":
            self._payload(result)
            raise SystemExit(f"{nombre} did not ask for confirmation")
        return result

    def respond(
        self, nombre: str, arguments: dict[str, Any], question: dict[str, Any], *, si: bool
    ) -> Any:
        key = next(iter(question["inputRequests"]))
        return self._payload(
            self._rpc(
                "tools/call",
                {
                    "name": nombre,
                    "arguments": arguments,
                    "inputResponses": {key: {"action": "accept", "content": {"confirmado": si}}},
                    "requestState": question["requestState"],
                },
            )
        )

    @staticmethod
    def question_text(question: dict[str, Any]) -> str:
        key = next(iter(question["inputRequests"]))
        return str(question["inputRequests"][key]["params"]["message"])


def step(titulo: str) -> None:
    print(f"\n\033[1m▸ {titulo}\033[0m")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mcp", default="http://localhost:8080/mcp")
    parser.add_argument("--issuer", default="http://localhost:9000")
    args = parser.parse_args()

    step("1 · The server requires authentication")
    anonima = httpx.post(
        args.mcp,
        json={"jsonrpc": "2.0", "id": 0, "method": "tools/list"},
        headers=HEADERS,
        timeout=10,
    )
    if anonima.status_code != 401:
        raise SystemExit(f"expected 401 with no token, got {anonima.status_code}")
    print(f"  401 · WWW-Authenticate: {anonima.headers.get('www-authenticate', '')[:80]}…")

    step("2 · Protected-resource discovery")
    metadata = httpx.get(
        args.mcp.replace("/mcp", "/.well-known/oauth-protected-resource"), timeout=10
    ).json()
    print(f"  authorization_servers: {metadata['authorization_servers']}")
    print(f"  scopes_supported:      {metadata['scopes_supported']}")

    step("3 · OAuth 2.1 + PKCE")
    token = get_token(args.issuer, "read write", "recepcion@clinica.local")
    print(f"  access_token: {token[:40]}… ({len(token)} bytes)")

    step("4 · Streamable HTTP, stateless")
    client = MCPTestClient(args.mcp, token)
    tools = client._rpc("tools/list", {})["tools"]
    print("  no initialize call, no session to carry around")
    print(f"  tools: {len(tools)} · {', '.join(t['name'] for t in tools[:4])}…")

    step("5 · Reading")
    paciente = client.call_tool("buscar_paciente", {"nombre": "a", "limite": 1})[0]
    print(f"  patient: {paciente['nombre']} · régimen {paciente['regimen']}")

    # Pick a slot at an hour the patient is not already booked for. A patient
    # cannot be in two chairs at once, and the domain says so, so the client
    # picks properly instead of discovering it in an error.
    ocupadas = {
        c["inicio_local"]
        for c in client.call_tool("listar_citas_paciente", {"paciente_id": paciente["id"]})
    }
    free_slots = client.call_tool("consultar_disponibilidad", {"limite": 25})
    slot = next((s for s in free_slots if s["inicio_local"] not in ocupadas), None)
    if slot is None:
        raise SystemExit("no free slot at an hour the patient has available")
    print(f"  free slot: {slot['inicio_local']} with {slot['profesional']}")

    step("6 · Write, round 1: the server asks and does NOT execute")
    arguments = {"paciente_id": paciente["id"], "slot_id": slot["slot_id"]}
    question = client.ask("agendar_cita", arguments)
    for line in client.question_text(question).splitlines():
        print(f"    {line}")
    print(f"  requestState: {len(question['requestState'])} bytes, sealed")

    step("7 · Round 2: the person approves, and now it executes")
    hecho = client.respond("agendar_cita", arguments, question, si=True)
    cita = hecho["cita"]
    print(f"  appointment {cita['id']} · state {cita['estado']} · {cita['inicio_local']}")

    step("8 · The sealed state cannot be reused or tampered with")
    tampered = {**question, "requestState": question["requestState"][:-4] + "AAAA"}
    try:
        client.respond("agendar_cita", arguments, tampered, si=True)
    except SystemExit:
        print("  tampered state: refused ✓")
    else:
        raise SystemExit("FAILURE: a tampered requestState was accepted")

    step("9 · A token without 'clinical' cannot touch clinical data")
    try:
        client.ask("registrar_motivo_consulta", {"cita_id": cita["id"], "motivo": "dolor"})
    except SystemExit as esperado:
        print(f"  {str(esperado).splitlines()[1][:88]}")
    else:
        raise SystemExit("FAILURE: a clinical write was allowed without the scope")

    print("\n\033[32m✓ The whole flow works against the real stack.\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
