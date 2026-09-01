"""Prove the auth layer is pluggable, rather than asserting it.

Starts from a token issued by a **real Keycloak realm** and calls the MCP server
that trusts Keycloak: same image, same code, only `OAUTH_ISSUER` and
`OAUTH_JWKS_URL` repointed. Then it shows the two are not interchangeable by
accident, because each server refuses the other's token. That refusal is the
issuer and audience binding doing its job.

    docker compose --profile keycloak up -d --wait
    uv run python scripts/verify_keycloak.py
"""

from __future__ import annotations

import sys

import httpx

from scripts.get_token import get_token
from scripts.smoke import HEADERS, MCPTestClient

KEYCLOAK = "http://localhost:9100/realms/clinic"
MCP_KEYCLOAK = "http://localhost:8081/mcp"
MCP_PROPIO = "http://localhost:8080/mcp"


def keycloak_token(scope: str = "read") -> str:
    response = httpx.post(
        f"{KEYCLOAK}/protocol/openid-connect/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "clinica-service",
            "client_secret": "dev-only-secret-not-a-credential",
            "scope": scope,
        },
        timeout=15,
    )
    response.raise_for_status()
    return str(response.json()["access_token"])


def step(titulo: str) -> None:
    print(f"\n\033[1m▸ {titulo}\033[0m")


def rejected(url: str, token: str) -> bool:
    response = httpx.post(
        url,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={**HEADERS, "Authorization": f"Bearer {token}", "mcp-method": "tools/list"},
        timeout=15,
    )
    return response.status_code == 401


def main() -> int:
    step("1 · Keycloak issues a token carrying our scopes")
    kc = keycloak_token("read")
    print(f"  Keycloak token: {kc[:40]}… ({len(kc)} bytes)")

    step("2 · The same code, trusting Keycloak, accepts it")
    client = MCPTestClient(MCP_KEYCLOAK, kc)
    tools = client._rpc("tools/list", {})["tools"]
    print(f"  tools visible: {len(tools)}")
    slot = client.call_tool("check_availability", {"limit": 1})[0]
    print(f"  a real read: free slot {slot['start_local']}")

    step("3 · The two issuers are not interchangeable by accident")
    if not rejected(MCP_PROPIO, kc):
        raise SystemExit("FAILURE: the in-repo server accepted a Keycloak token")
    print("  Keycloak token → in-repo AS server: 401 ✓")

    propio = get_token("http://localhost:9000", "read", "recepcion@clinica.local")
    if not rejected(MCP_KEYCLOAK, propio):
        raise SystemExit("FAILURE: the Keycloak server accepted an in-repo AS token")
    print("  in-repo AS token → Keycloak server: 401 ✓")

    print(
        "\n\033[32m✓ The auth layer is swappable: same code, a different IdP, and "
        "issuer/audience validation still keeps them apart.\033[0m"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
