"""End-to-end smoke test against a running stack.

Walks the path a real client walks: discover the protected resource, obtain a
token through PKCE, initialize the MCP session, list the tools, read a resource,
call a read tool, then propose a write and confirm it. Anything that only works
in the test-suite fails here.

    docker compose up -d --wait
    uv run python scripts/smoke.py
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import httpx

from scripts.obtener_token import obtener_token

CABECERAS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


class SesionMCP:
    """A minimal Streamable HTTP client, enough to prove the server works."""

    def __init__(self, url: str, token: str) -> None:
        self.url = url
        self.cliente = httpx.Client(
            timeout=30, headers={**CABECERAS, "Authorization": f"Bearer {token}"}
        )
        self.id = 0
        self.sesion: str | None = None

    def _rpc(self, metodo: str, params: dict[str, Any] | None = None) -> Any:
        self.id += 1
        # The session header is echoed back if the server issues one. Against a
        # stateless server it never does, and every call stands alone.
        cabeceras = {"Mcp-Session-Id": self.sesion} if self.sesion else {}
        respuesta = self.cliente.post(
            self.url,
            json={"jsonrpc": "2.0", "id": self.id, "method": metodo, "params": params or {}},
            headers=cabeceras,
        )
        respuesta.raise_for_status()
        if "mcp-session-id" in respuesta.headers:
            self.sesion = respuesta.headers["mcp-session-id"]

        cuerpo = respuesta.text
        # Streamable HTTP answers as SSE; pull the single data frame out.
        for linea in cuerpo.splitlines():
            if linea.startswith("data:"):
                cuerpo = linea[5:].strip()
                break
        datos = json.loads(cuerpo)
        if "error" in datos:
            raise SystemExit(f"{metodo} falló: {datos['error']}")
        return datos["result"]

    def notificar(self, metodo: str) -> None:
        self.cliente.post(
            self.url,
            json={"jsonrpc": "2.0", "method": metodo},
            headers={"Mcp-Session-Id": self.sesion} if self.sesion else {},
        )

    def initialize(self) -> Any:
        resultado = self._rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "smoke", "version": "1"},
            },
        )
        self.notificar("notifications/initialized")
        return resultado

    def llamar(self, nombre: str, argumentos: dict[str, Any]) -> Any:
        resultado = self._rpc("tools/call", {"name": nombre, "arguments": argumentos})
        if resultado.get("isError"):
            texto = "\n".join(c.get("text", "") for c in resultado.get("content", []))
            raise SystemExit(f"{nombre} devolvió error:\n{texto}")
        return resultado.get("structuredContent") or resultado.get("content")


def paso(titulo: str) -> None:
    print(f"\n\033[1m▸ {titulo}\033[0m")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mcp", default="http://localhost:8080/mcp")
    parser.add_argument("--issuer", default="http://localhost:9000")
    args = parser.parse_args()

    paso("1 · El servidor exige autenticación")
    anonima = httpx.post(
        args.mcp,
        json={"jsonrpc": "2.0", "id": 0, "method": "initialize"},
        headers=CABECERAS,
        timeout=10,
    )
    if anonima.status_code != 401:
        raise SystemExit(f"esperaba 401 sin token, llegó {anonima.status_code}")
    print(f"  401 · WWW-Authenticate: {anonima.headers.get('www-authenticate', '')[:90]}…")

    paso("2 · Descubrimiento del recurso protegido")
    metadata = httpx.get(
        args.mcp.replace("/mcp", "/.well-known/oauth-protected-resource"), timeout=10
    ).json()
    print(f"  authorization_servers: {metadata['authorization_servers']}")

    paso("3 · OAuth 2.1 + PKCE")
    token = obtener_token(args.issuer, "read write", "recepcion@clinica.local")
    print(f"  access_token: {token[:40]}… ({len(token)} bytes)")

    paso("4 · Sesión MCP (Streamable HTTP, sin estado)")
    sesion = SesionMCP(args.mcp, token)
    info = sesion.initialize()
    print(f"  servidor: {info['serverInfo']['name']} v{info['serverInfo']['version']}")
    print(f"  Mcp-Session-Id: {sesion.sesion or 'ninguno, el transporte no guarda estado'}")

    tools = sesion._rpc("tools/list")["tools"]
    print(f"  tools: {len(tools)} · {', '.join(t['name'] for t in tools[:4])}…")

    paso("5 · Lectura")
    pacientes = sesion.llamar("buscar_paciente", {"nombre": "a", "limite": 1})
    paciente = (pacientes or {}).get("result", pacientes)[0]
    print(f"  paciente: {paciente['nombre']} · régimen {paciente['regimen']}")

    cupos = sesion.llamar("consultar_disponibilidad", {"limite": 1})
    cupo = (cupos or {}).get("result", cupos)[0]
    print(f"  cupo libre: {cupo['inicio_local']} con {cupo['profesional']}")

    paso("6 · Escritura: la propuesta NO ejecuta")
    propuesta = sesion.llamar(
        "agendar_cita", {"paciente_id": paciente["id"], "slot_id": cupo["slot_id"]}
    )
    print(f"  {propuesta['resumen']}")
    for efecto in propuesta["esto_va_a_pasar"]:
        print(f"    · {efecto}")
    for advertencia in propuesta["advertencias"]:
        print(f"    ⚠ {advertencia}")

    paso("7 · Confirmación humana: ahora sí ejecuta")
    hecho = sesion.llamar(
        "confirmar_operacion", {"token_confirmacion": propuesta["token_confirmacion"]}
    )
    cita = hecho["resultado"]["cita"]
    print(f"  cita {cita['id']} · estado {cita['estado']} · {cita['inicio_local']}")

    paso("8 · El token de confirmación es de un solo uso")
    try:
        sesion.llamar(
            "confirmar_operacion", {"token_confirmacion": propuesta["token_confirmacion"]}
        )
    except SystemExit as esperado:
        print(f"  rechazado como debe ser: {str(esperado).splitlines()[1][:80]}")
    else:
        raise SystemExit("FALLO: el token se pudo reusar")

    paso("9 · Un token sin 'clinical' no toca datos clínicos")
    try:
        sesion.llamar("registrar_motivo_consulta", {"cita_id": cita["id"], "motivo": "dolor"})
    except SystemExit as esperado:
        print(f"  {str(esperado).splitlines()[1][:90]}")
    else:
        raise SystemExit("FALLO: se permitió una escritura clínica sin scope")

    print("\n\033[32m✓ Todo el flujo funciona sobre el stack real.\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
