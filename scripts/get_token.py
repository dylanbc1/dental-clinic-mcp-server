"""Obtain an access token by walking the real OAuth 2.1 + PKCE flow.

Useful for the MCP Inspector, for curl, and for convincing yourself the flow is
not a stub. It does exactly what a client does: generate a verifier, derive the
S256 challenge, hit /authorize, exchange the code. No shortcuts.

    uv run python scripts/get_token.py --scope "read write"
    uv run python scripts/get_token.py --scope "read write clinical" \
        --sujeto odontologa@clinica.local

(The file is deliberately not named `token.py`: on the script path it would
shadow the standard library's `token` module and break every import of
`tokenize`.)
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import secrets
import sys
from urllib.parse import parse_qs, urlparse

import httpx

REDIRECT = "http://localhost:6274/oauth/callback"
CLIENTE = "clinica-demo"


def pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    return verifier, challenge


def get_token(issuer: str, scope: str, subject: str) -> str:
    issuer = issuer.rstrip("/")
    verifier, challenge = pkce()

    with httpx.Client(follow_redirects=False, timeout=10) as client:
        metadata = client.get(f"{issuer}/.well-known/oauth-authorization-server").json()

        autorizacion = client.get(
            metadata["authorization_endpoint"],
            params={
                "response_type": "code",
                "client_id": CLIENTE,
                "redirect_uri": REDIRECT,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "scope": scope,
                "state": secrets.token_urlsafe(8),
                "login_hint": subject,
            },
        )
        if autorizacion.status_code != 302:
            raise SystemExit(f"/authorize failed: {autorizacion.status_code} {autorizacion.text}")

        query = parse_qs(urlparse(autorizacion.headers["location"]).query)
        if "error" in query:
            raise SystemExit(f"/authorize refused the request: {query}")

        token = client.post(
            metadata["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": query["code"][0],
                "code_verifier": verifier,
                "client_id": CLIENTE,
            },
        )
        if token.status_code != 200:
            raise SystemExit(f"/token failed: {token.status_code} {token.text}")
        return str(token.json()["access_token"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issuer", default="http://localhost:9000")
    parser.add_argument("--scope", default="read write")
    parser.add_argument("--subject", default="recepcion@clinica.local")
    args = parser.parse_args()
    print(get_token(args.issuer, args.scope, args.subject))
    return 0


if __name__ == "__main__":
    sys.exit(main())
