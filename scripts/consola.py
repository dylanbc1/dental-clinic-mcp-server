"""An interactive MCP client, so you can drive the server by hand.

The MCP Inspector cannot exercise the write tools yet: its JavaScript SDK has
not caught up to the 2026-07-28 spec, so it negotiates an older version and has
no way to answer an `input_required`. Read tools work there; this is for the
rest.

It is a real client, not a mock: OAuth 2.1 + PKCE for the token, Streamable
HTTP, and the full MRTR round trip. When a tool asks for a confirmation, you are
the human in the loop.

    uv run python scripts/consola.py
    uv run python scripts/consola.py --scope "read"        # try being refused
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import httpx

from scripts.obtener_token import obtener_token

VERDE, ROJO, AZUL, GRIS, NEGRITA, FIN = (
    "\033[32m",
    "\033[31m",
    "\033[36m",
    "\033[90m",
    "\033[1m",
    "\033[0m",
)

SOBRE = {
    "_meta": {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {"elicitation": {}},
    }
}
CABECERAS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2026-07-28",
}


class Consola:
    def __init__(self, url: str, token: str) -> None:
        self.url = url
        self.http = httpx.Client(
            timeout=30, headers={**CABECERAS, "Authorization": f"Bearer {token}"}
        )
        self.id = 0

    def rpc(self, metodo: str, params: dict[str, Any]) -> dict[str, Any]:
        self.id += 1
        cabeceras = {"mcp-method": metodo}
        if "name" in params:
            cabeceras["mcp-name"] = str(params["name"])
        r = self.http.post(
            self.url,
            json={
                "jsonrpc": "2.0",
                "id": self.id,
                "method": metodo,
                "params": {**params, **SOBRE},
            },
            headers=cabeceras,
        )
        cuerpo = r.text
        for linea in cuerpo.splitlines():
            if linea.startswith("data:"):
                cuerpo = linea[5:].strip()
                break
        try:
            datos = json.loads(cuerpo)
        except json.JSONDecodeError:
            return {"_transporte": f"HTTP {r.status_code}: {cuerpo[:200]}"}
        if "error" in datos:
            return {"_rpc": datos["error"]}
        return dict(datos["result"])

    def llamar(self, nombre: str, argumentos: dict[str, Any]) -> None:
        """Call a tool, answering any confirmation it asks for."""
        resultado = self.rpc("tools/call", {"name": nombre, "arguments": argumentos})

        if "_transporte" in resultado or "_rpc" in resultado:
            print(f"{ROJO}  {resultado.get('_transporte') or resultado['_rpc']}{FIN}")
            return

        if resultado.get("resultType") == "input_required":
            self._confirmar(nombre, argumentos, resultado)
            return

        self._mostrar(resultado)

    def _confirmar(self, nombre: str, argumentos: dict[str, Any], pregunta: dict[str, Any]) -> None:
        clave = next(iter(pregunta["inputRequests"]))
        mensaje = pregunta["inputRequests"][clave]["params"]["message"]

        print(f"\n{AZUL}{NEGRITA}  ── El servidor pide confirmación ──{FIN}")
        for linea in mensaje.splitlines():
            print(f"{AZUL}  │{FIN} {linea}")
        estado = pregunta["requestState"]
        print(f"{GRIS}  │ requestState: {len(estado)} bytes, sellado y opaco{FIN}")
        print(f"{GRIS}  │ nada se ha modificado todavía{FIN}")

        respuesta = input(f"{NEGRITA}  ¿Confirmas? [s/N] {FIN}").strip().lower()
        acepta = respuesta in {"s", "si", "sí", "y", "yes"}

        resultado = self.rpc(
            "tools/call",
            {
                "name": nombre,
                "arguments": argumentos,
                "inputResponses": {clave: {"action": "accept", "content": {"confirmado": acepta}}},
                "requestState": estado,
            },
        )
        print()
        if "_transporte" in resultado or "_rpc" in resultado:
            print(f"{ROJO}  {resultado.get('_transporte') or resultado['_rpc']}{FIN}")
            return
        self._mostrar(resultado)

    @staticmethod
    def _mostrar(resultado: dict[str, Any]) -> None:
        if resultado.get("isError"):
            for bloque in resultado.get("content", []):
                for linea in bloque.get("text", "").splitlines():
                    print(f"{ROJO}  {linea}{FIN}")
            return
        contenido = resultado.get("structuredContent")
        if contenido is None:
            for bloque in resultado.get("content", []):
                print(f"  {bloque.get('text', '')}")
            return
        datos = contenido.get("result", contenido)
        texto = json.dumps(datos, ensure_ascii=False, indent=2)
        for linea in texto.splitlines()[:40]:
            print(f"{VERDE}  {linea}{FIN}")
        if len(texto.splitlines()) > 40:
            print(f"{GRIS}  … ({len(texto.splitlines())} líneas){FIN}")


AYUDA = f"""
{NEGRITA}Comandos{FIN}
  {AZUL}tools{FIN}                        lista las herramientas
  {AZUL}<n>{FIN} o {AZUL}<nombre>{FIN} {GRIS}{{json}}{FIN}       llama una herramienta
  {AZUL}ayuda{FIN}                        esta ayuda
  {AZUL}salir{FIN}

{NEGRITA}Para empezar{FIN}
  {GRIS}buscar_paciente {{"nombre": "a", "limite": 3}}{FIN}
  {GRIS}consultar_disponibilidad {{"limite": 3}}{FIN}
  {GRIS}agendar_cita {{"paciente_id": 20, "slot_id": 719}}{FIN}   ← este te va a preguntar
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mcp", default="http://localhost:8080/mcp")
    parser.add_argument("--issuer", default="http://localhost:9000")
    parser.add_argument("--scope", default="read write clinical")
    parser.add_argument("--sujeto", default="recepcion@clinica.local")
    args = parser.parse_args()

    print(f"{NEGRITA}Obteniendo un token por OAuth 2.1 + PKCE…{FIN}")
    token = obtener_token(args.issuer, args.scope, args.sujeto)
    print(f"  scopes: {AZUL}{args.scope}{FIN}   sujeto: {AZUL}{args.sujeto}{FIN}")

    consola = Consola(args.mcp, token)
    listado = consola.rpc("tools/list", {})
    if "_rpc" in listado or "_transporte" in listado:
        print(f"{ROJO}No pude conectar: {listado}{FIN}")
        return 1
    tools = listado["tools"]
    nombres = [t["name"] for t in tools]
    print(f"  conectado · {len(tools)} herramientas · sin sesión (transporte sin estado)")
    print(AYUDA)

    while True:
        try:
            entrada = input(f"{NEGRITA}› {FIN}").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not entrada:
            continue
        if entrada in {"salir", "exit", "quit"}:
            return 0
        if entrada in {"ayuda", "help", "?"}:
            print(AYUDA)
            continue
        if entrada == "tools":
            for i, t in enumerate(tools, 1):
                print(f"  {GRIS}{i:>2}{FIN} {AZUL}{t['name']:<28}{FIN}{t['title']}")
            continue

        partes = entrada.split(None, 1)
        nombre = partes[0]
        if nombre.isdigit() and 1 <= int(nombre) <= len(tools):
            nombre = nombres[int(nombre) - 1]
        if nombre not in nombres:
            print(f"{ROJO}  no existe la herramienta '{nombre}'. Escribe 'tools'.{FIN}")
            continue
        try:
            argumentos = json.loads(partes[1]) if len(partes) > 1 else {}
        except json.JSONDecodeError as exc:
            print(f"{ROJO}  los argumentos deben ser JSON: {exc}{FIN}")
            continue

        consola.llamar(nombre, argumentos)
        print()


if __name__ == "__main__":
    sys.exit(main())
