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

from scripts.get_token import obtener_token

CABECERAS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2026-07-28",
}

#: With no session to remember the handshake, every call carries its own
#: protocol version and capabilities. That is the visible cost of statelessness.
SOBRE = {
    "_meta": {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {"elicitation": {}},
    }
}


class ClienteMCP:
    """A minimal 2026-07-28 client, enough to prove the server works."""

    def __init__(self, url: str, token: str) -> None:
        self.url = url
        self.http = httpx.Client(
            timeout=30, headers={**CABECERAS, "Authorization": f"Bearer {token}"}
        )
        self.id = 0

    def _rpc(self, metodo: str, params: dict[str, Any]) -> Any:
        self.id += 1
        cabeceras = {"mcp-method": metodo}
        if "name" in params:
            cabeceras["mcp-name"] = str(params["name"])
        respuesta = self.http.post(
            self.url,
            json={
                "jsonrpc": "2.0",
                "id": self.id,
                "method": metodo,
                "params": {**params, **SOBRE},
            },
            headers=cabeceras,
        )
        if respuesta.status_code >= 400:
            # A malformed or tampered requestState is refused at the protocol
            # layer, before any tool runs, so it arrives as a plain HTTP error.
            raise SystemExit(f"{metodo} refused ({respuesta.status_code}): {respuesta.text[:160]}")
        cuerpo = respuesta.text
        # Streamable HTTP answers as SSE; pull the single data frame out.
        for linea in cuerpo.splitlines():
            if linea.startswith("data:"):
                cuerpo = linea[5:].strip()
                break
        datos = json.loads(cuerpo)
        if "error" in datos:
            raise SystemExit(f"{metodo} failed: {datos['error']}")
        return datos["result"]

    @staticmethod
    def _payload(resultado: dict[str, Any]) -> Any:
        if resultado.get("isError"):
            texto = "\n".join(c.get("text", "") for c in resultado.get("content", []))
            raise SystemExit(f"the tool returned an error:\n{texto}")
        contenido = resultado.get("structuredContent") or {}
        return contenido.get("result", contenido)

    def llamar(self, nombre: str, argumentos: dict[str, Any]) -> Any:
        return self._payload(self._rpc("tools/call", {"name": nombre, "arguments": argumentos}))

    def preguntar(self, nombre: str, argumentos: dict[str, Any]) -> dict[str, Any]:
        resultado: dict[str, Any] = self._rpc(
            "tools/call", {"name": nombre, "arguments": argumentos}
        )
        if resultado.get("resultType") != "input_required":
            self._payload(resultado)
            raise SystemExit(f"{nombre} did not ask for confirmation")
        return resultado

    def responder(
        self, nombre: str, argumentos: dict[str, Any], pregunta: dict[str, Any], *, si: bool
    ) -> Any:
        clave = next(iter(pregunta["inputRequests"]))
        return self._payload(
            self._rpc(
                "tools/call",
                {
                    "name": nombre,
                    "arguments": argumentos,
                    "inputResponses": {clave: {"action": "accept", "content": {"confirmado": si}}},
                    "requestState": pregunta["requestState"],
                },
            )
        )

    @staticmethod
    def mensaje(pregunta: dict[str, Any]) -> str:
        clave = next(iter(pregunta["inputRequests"]))
        return str(pregunta["inputRequests"][clave]["params"]["message"])


def paso(titulo: str) -> None:
    print(f"\n\033[1m▸ {titulo}\033[0m")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mcp", default="http://localhost:8080/mcp")
    parser.add_argument("--issuer", default="http://localhost:9000")
    args = parser.parse_args()

    paso("1 · The server requires authentication")
    anonima = httpx.post(
        args.mcp,
        json={"jsonrpc": "2.0", "id": 0, "method": "tools/list"},
        headers=CABECERAS,
        timeout=10,
    )
    if anonima.status_code != 401:
        raise SystemExit(f"expected 401 with no token, got {anonima.status_code}")
    print(f"  401 · WWW-Authenticate: {anonima.headers.get('www-authenticate', '')[:80]}…")

    paso("2 · Protected-resource discovery")
    metadata = httpx.get(
        args.mcp.replace("/mcp", "/.well-known/oauth-protected-resource"), timeout=10
    ).json()
    print(f"  authorization_servers: {metadata['authorization_servers']}")
    print(f"  scopes_supported:      {metadata['scopes_supported']}")

    paso("3 · OAuth 2.1 + PKCE")
    token = obtener_token(args.issuer, "read write", "recepcion@clinica.local")
    print(f"  access_token: {token[:40]}… ({len(token)} bytes)")

    paso("4 · Streamable HTTP, stateless")
    cliente = ClienteMCP(args.mcp, token)
    tools = cliente._rpc("tools/list", {})["tools"]
    print("  no initialize call, no session to carry around")
    print(f"  tools: {len(tools)} · {', '.join(t['name'] for t in tools[:4])}…")

    paso("5 · Reading")
    paciente = cliente.llamar("buscar_paciente", {"nombre": "a", "limite": 1})[0]
    print(f"  patient: {paciente['nombre']} · régimen {paciente['regimen']}")

    # Pick a slot at an hour the patient is not already booked for. A patient
    # cannot be in two chairs at once, and the domain says so, so the client
    # picks properly instead of discovering it in an error.
    ocupadas = {
        c["inicio_local"]
        for c in cliente.llamar("listar_citas_paciente", {"paciente_id": paciente["id"]})
    }
    libres = cliente.llamar("consultar_disponibilidad", {"limite": 25})
    cupo = next((s for s in libres if s["inicio_local"] not in ocupadas), None)
    if cupo is None:
        raise SystemExit("no free slot at an hour the patient has available")
    print(f"  free slot: {cupo['inicio_local']} with {cupo['profesional']}")

    paso("6 · Write, round 1: the server asks and does NOT execute")
    argumentos = {"paciente_id": paciente["id"], "slot_id": cupo["slot_id"]}
    pregunta = cliente.preguntar("agendar_cita", argumentos)
    for linea in cliente.mensaje(pregunta).splitlines():
        print(f"    {linea}")
    print(f"  requestState: {len(pregunta['requestState'])} bytes, sealed")

    paso("7 · Round 2: the person approves, and now it executes")
    hecho = cliente.responder("agendar_cita", argumentos, pregunta, si=True)
    cita = hecho["cita"]
    print(f"  appointment {cita['id']} · state {cita['estado']} · {cita['inicio_local']}")

    paso("8 · The sealed state cannot be reused or tampered with")
    alterado = {**pregunta, "requestState": pregunta["requestState"][:-4] + "AAAA"}
    try:
        cliente.responder("agendar_cita", argumentos, alterado, si=True)
    except SystemExit:
        print("  tampered state: refused ✓")
    else:
        raise SystemExit("FAILURE: a tampered requestState was accepted")

    paso("9 · A token without 'clinical' cannot touch clinical data")
    try:
        cliente.preguntar("registrar_motivo_consulta", {"cita_id": cita["id"], "motivo": "dolor"})
    except SystemExit as esperado:
        print(f"  {str(esperado).splitlines()[1][:88]}")
    else:
        raise SystemExit("FAILURE: a clinical write was allowed without the scope")

    print("\n\033[32m✓ The whole flow works against the real stack.\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
