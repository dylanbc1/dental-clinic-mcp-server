"""Call the domain API by hand, with a valid signature.

The API refuses anything the MCP server did not sign, which is the point, and
which would otherwise make every `curl localhost:8000/...` in the documentation
a 401. This is that curl, with the three headers attached.

    uv run python scripts/call_api.py /appointments/1
    uv run python scripts/call_api.py /appointments -X POST -d '{"patient_id": 1, "slot_id": 2}'

It reads the same `INTERNAL_API_KEYS` the services do, so if it works and the
MCP server does not, the difference is not the key.
"""

from __future__ import annotations

import argparse
import json as jsonlib
import sys

import httpx

from backend.config import get_settings
from backend.internal_auth import sign_request


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="e.g. /appointments/1")
    parser.add_argument("-X", "--method", default="GET")
    parser.add_argument("-d", "--data", default=None, help="JSON body")
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--actor", default="cli@clinica.local")
    args = parser.parse_args()

    settings = get_settings()
    body = args.data.encode() if args.data else b""
    url = httpx.URL(args.base.rstrip("/") + args.path)

    headers = sign_request(
        settings.internal_api_keys[0],
        method=args.method,
        path=url.path,
        query=url.query.decode(),
        actor=args.actor,
        body=body,
    )
    if body:
        headers["content-type"] = "application/json"

    response = httpx.request(args.method, url, content=body, headers=headers, timeout=15)
    try:
        print(jsonlib.dumps(response.json(), ensure_ascii=False, indent=2))
    except ValueError:
        print(response.text)
    return 0 if response.is_success else 1


if __name__ == "__main__":
    sys.exit(main())
