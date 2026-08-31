"""Prove the auth layer is pluggable, rather than asserting it.

Starts from a token issued by a **real Keycloak realm** and calls the MCP server
that trusts Keycloak: same image, same code, only `OAUTH_ISSUER` and
`OAUTH_JWKS_URL` repointed. Then it shows the two are not interchangeable by
accident, because each server refuses the other's token. That refusal is the
issuer and audience binding doing its job.

    docker compose --profile keycloak up -d --wait
    uv run python scripts/verificar_keycloak.py
"""

from __future__ import annotations

import sys

import httpx

from scripts.obtener_token import obtener_token
from scripts.smoke import CABECERAS, ClienteMCP

KEYCLOAK = "http://localhost:9100/realms/clinica"
MCP_KEYCLOAK = "http://localhost:8081/mcp"
MCP_PROPIO = "http://localhost:8080/mcp"


def token_de_keycloak(scope: str = "read") -> str:
    respuesta = httpx.post(
        f"{KEYCLOAK}/protocol/openid-connect/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "clinica-service",
            "client_secret": "dev-only-secret-not-a-credential",
            "scope": scope,
        },
        timeout=15,
    )
    respuesta.raise_for_status()
    return str(respuesta.json()["access_token"])


def paso(titulo: str) -> None:
    print(f"\n\033[1m▸ {titulo}\033[0m")


def rechazado(url: str, token: str) -> bool:
    respuesta = httpx.post(
        url,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={**CABECERAS, "Authorization": f"Bearer {token}", "mcp-method": "tools/list"},
        timeout=15,
    )
    return respuesta.status_code == 401


def main() -> int:
    paso("1 · Keycloak emite un token con nuestros scopes")
    kc = token_de_keycloak("read")
    print(f"  token de Keycloak: {kc[:40]}… ({len(kc)} bytes)")

    paso("2 · El mismo código, confiando en Keycloak, lo acepta")
    cliente = ClienteMCP(MCP_KEYCLOAK, kc)
    tools = cliente._rpc("tools/list", {})["tools"]
    print(f"  tools visibles: {len(tools)}")
    cupo = cliente.llamar("consultar_disponibilidad", {"limite": 1})[0]
    print(f"  lectura real: cupo libre {cupo['inicio_local']}")

    paso("3 · Los dos emisores no son intercambiables por accidente")
    if not rechazado(MCP_PROPIO, kc):
        raise SystemExit("FALLO: el server propio aceptó un token de Keycloak")
    print("  token de Keycloak → servidor del AS propio: 401 ✓")

    propio = obtener_token("http://localhost:9000", "read", "recepcion@clinica.local")
    if not rechazado(MCP_KEYCLOAK, propio):
        raise SystemExit("FALLO: el server de Keycloak aceptó un token del AS propio")
    print("  token del AS propio → servidor de Keycloak: 401 ✓")

    print(
        "\n\033[32m✓ La capa de auth es intercambiable: mismo código, otro IdP, "
        "y la validación de emisor/audiencia sigue separándolos.\033[0m"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
