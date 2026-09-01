"""An interactive MCP client, so you can drive the server by hand.

The MCP Inspector cannot exercise the write tools yet: its JavaScript SDK has
not caught up to the 2026-07-28 spec, so it negotiates an older version and has
no way to answer an `input_required`. Read tools work there; this is for the
rest.

It is a real client, not a mock: OAuth 2.1 + PKCE for the token, Streamable
HTTP, and the full MRTR round trip. When a tool asks for a confirmation, you are
the human in the loop.

    uv run python scripts/console.py
    uv run python scripts/console.py --scope "read"        # try being refused
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import httpx

from scripts.get_token import get_token

GREEN, RED, BLUE, GRIS, BOLD, FIN = (
    "\033[32m",
    "\033[31m",
    "\033[36m",
    "\033[90m",
    "\033[1m",
    "\033[0m",
)

ENVELOPE = {
    "_meta": {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {"elicitation": {}},
    }
}
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2026-07-28",
}


class Console:
    def __init__(self, url: str, token: str) -> None:
        self.url = url
        self.http = httpx.Client(
            timeout=30, headers={**HEADERS, "Authorization": f"Bearer {token}"}
        )
        self.id = 0

    def rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.id += 1
        headers = {"mcp-method": method}
        if "name" in params:
            headers["mcp-name"] = str(params["name"])
        r = self.http.post(
            self.url,
            json={
                "jsonrpc": "2.0",
                "id": self.id,
                "method": method,
                "params": {**params, **ENVELOPE},
            },
            headers=headers,
        )
        body = r.text
        for line in body.splitlines():
            if line.startswith("data:"):
                body = line[5:].strip()
                break
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return {"_transport": f"HTTP {r.status_code}: {body[:200]}"}
        if "error" in payload:
            return {"_rpc": payload["error"]}
        return dict(payload["result"])

    def call_tool(self, name: str, arguments: dict[str, Any]) -> None:
        """Call a tool, answering any confirmation it asks for."""
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})

        if "_transport" in result or "_rpc" in result:
            print(f"{RED}  {result.get('_transport') or result['_rpc']}{FIN}")
            return

        if result.get("resultType") == "input_required":
            self._confirm(name, arguments, result)
            return

        self._show(result)

    def _confirm(self, name: str, arguments: dict[str, Any], question: dict[str, Any]) -> None:
        key = next(iter(question["inputRequests"]))
        message = question["inputRequests"][key]["params"]["message"]

        print(f"\n{BLUE}{BOLD}  ── The server is asking for confirmation ──{FIN}")
        for line in message.splitlines():
            print(f"{BLUE}  │{FIN} {line}")
        status = question["requestState"]
        print(f"{GRIS}  │ requestState: {len(status)} bytes, sealed and opaque{FIN}")
        print(f"{GRIS}  │ nothing has changed yet{FIN}")

        response = input(f"{BOLD}  Confirm? [y/N] {FIN}").strip().lower()
        # Both languages accepted: the person at the keyboard may type either.
        accepts = response in {"y", "yes", "s", "si", "sí"}

        result = self.rpc(
            "tools/call",
            {
                "name": name,
                "arguments": arguments,
                "inputResponses": {key: {"action": "accept", "content": {"confirmed": accepts}}},
                "requestState": status,
            },
        )
        print()
        if "_transport" in result or "_rpc" in result:
            print(f"{RED}  {result.get('_transport') or result['_rpc']}{FIN}")
            return
        self._show(result)

    @staticmethod
    def _show(result: dict[str, Any]) -> None:
        if result.get("isError"):
            for block in result.get("content", []):
                for line in block.get("text", "").splitlines():
                    print(f"{RED}  {line}{FIN}")
            return
        content = result.get("structuredContent")
        if content is None:
            for block in result.get("content", []):
                print(f"  {block.get('text', '')}")
            return
        payload = content.get("result", content)
        text_of = json.dumps(payload, ensure_ascii=False, indent=2)
        for line in text_of.splitlines()[:40]:
            print(f"{GREEN}  {line}{FIN}")
        if len(text_of.splitlines()) > 40:
            print(f"{GRIS}  … ({len(text_of.splitlines())} lines){FIN}")


HELP = f"""
{BOLD}Commands{FIN}
  {BLUE}tools{FIN}                        list the tools
  {BLUE}<n>{FIN} or {BLUE}<name>{FIN} {GRIS}{{json}}{FIN}       call a tool
  {BLUE}help{FIN}                         this help
  {BLUE}quit{FIN}

{BOLD}To get started{FIN}
  {GRIS}search_patients {{"name": "a", "limit": 3}}{FIN}
  {GRIS}check_availability {{"limit": 3}}{FIN}
  {GRIS}book_appointment {{"patient_id": 20, "slot_id": 719}}{FIN}   ← this one will ask you
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mcp", default="http://localhost:8080/mcp")
    parser.add_argument("--issuer", default="http://localhost:9000")
    parser.add_argument("--scope", default="read write clinical")
    parser.add_argument("--subject", default="recepcion@clinica.local")
    args = parser.parse_args()

    print(f"{BOLD}Getting a token over OAuth 2.1 + PKCE…{FIN}")
    token = get_token(args.issuer, args.scope, args.subject)
    print(f"  scopes: {BLUE}{args.scope}{FIN}   subject: {BLUE}{args.subject}{FIN}")

    console = Console(args.mcp, token)
    listing = console.rpc("tools/list", {})
    if "_rpc" in listing or "_transport" in listing:
        print(f"{RED}Could not connect: {listing}{FIN}")
        return 1
    tools = listing["tools"]
    names = [t["name"] for t in tools]
    print(f"  connected · {len(tools)} tools · no session (stateless transport)")
    print(HELP)

    while True:
        try:
            entry = input(f"{BOLD}› {FIN}").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not entry:
            continue
        if entry in {"quit", "exit", "salir"}:
            return 0
        if entry in {"help", "ayuda", "?"}:
            print(HELP)
            continue
        if entry == "tools":
            for i, t in enumerate(tools, 1):
                print(f"  {GRIS}{i:>2}{FIN} {BLUE}{t['name']:<28}{FIN}{t['title']}")
            continue

        parts = entry.split(None, 1)
        name = parts[0]
        if name.isdigit() and 1 <= int(name) <= len(tools):
            name = names[int(name) - 1]
        if name not in names:
            print(f"{RED}  no such tool '{name}'. Type 'tools'.{FIN}")
            continue
        try:
            arguments = json.loads(parts[1]) if len(parts) > 1 else {}
        except json.JSONDecodeError as exc:
            print(f"{RED}  arguments must be JSON: {exc}{FIN}")
            continue

        console.call_tool(name, arguments)
        print()


if __name__ == "__main__":
    sys.exit(main())
