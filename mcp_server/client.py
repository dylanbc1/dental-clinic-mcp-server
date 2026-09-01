"""HTTP client to the domain backend.

The MCP layer never touches the database. It speaks to the internal REST API
server-to-server, which is what keeps the security controls in one place and
makes the backend replaceable.

Every non-2xx response is translated into :class:`ErrorHerramienta` here, so no
call site has to remember to check a status code.
"""

from __future__ import annotations

from typing import Any, cast

import httpx

from backend.config import get_settings
from backend.domain.errors import ErrorCode
from backend.internal_auth import sign_request
from mcp_server.errors import StructuredToolError, backend_down_error

TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class BackendClient:
    """Thin async wrapper over the internal API.

    `actor` is forwarded as `X-Actor` on every write so the audit trail records
    *who* asked, not just that the MCP server did.
    """

    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.AsyncClient | None = None,
        signing_key: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._owned = client is None
        self._client = client or httpx.AsyncClient(base_url=self._base_url, timeout=TIMEOUT)
        #: Signs every call. Falls back to the ring's first key so a caller that
        #: builds a client by hand still authenticates; the backend refuses
        #: anything unsigned, so getting this wrong fails loudly at the first
        #: request rather than silently downgrading.
        self._signing_key = signing_key or get_settings().internal_api_keys[0]

    async def aclose(self) -> None:
        if self._owned:
            await self._client.aclose()

    async def _send(
        self,
        method: str,
        path: str,
        *,
        actor: str | None = None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        request = self._client.build_request(method, path, params=clean or None, json=json)
        # Sign what will actually be sent: httpx has already serialised the body
        # and assembled the query string, so signing here cannot drift from the
        # bytes on the wire.
        request.headers.update(
            sign_request(
                self._signing_key,
                method=method,
                path=request.url.path,
                query=request.url.query.decode(),
                actor=actor or "",
                body=request.content,
            )
        )
        try:
            response = await self._client.send(request)
        except httpx.HTTPError as exc:
            raise backend_down_error(str(exc)) from exc

        if response.is_success:
            return response.json()

        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict) and payload.get("error"):
            raise StructuredToolError.from_envelope(payload)
        raise StructuredToolError(
            "INTERNAL_ERROR",
            f"The backend answered {response.status_code} with no structured error.",
            suggestion="Retry in a few seconds; if it persists, report the incident.",
            details={"status": response.status_code},
        )

    @staticmethod
    def _require(payload: Any, expected: type, path: str) -> Any:
        """Assert the response shape before handing it to a tool.

        Not paranoia: the tools declare typed return values, and a backend that
        answers with an unexpected shape should fail here, where the error names
        the endpoint, rather than three frames later inside a formatter.
        """
        if not isinstance(payload, expected):
            raise StructuredToolError(
                ErrorCode.UNEXPECTED_RESPONSE,
                f"{path} returned {type(payload).__name__} instead of {expected.__name__}.",
                suggestion="This is a backend fault, not a problem with your request.",
            )
        return payload

    # --- reads -------------------------------------------------------------

    async def get_object(self, path: str, **params: Any) -> dict[str, Any]:
        """GET returning a JSON object."""
        payload = await self._send("GET", path, params=params)
        return cast("dict[str, Any]", self._require(payload, dict, path))

    async def get_list(self, path: str, **params: Any) -> list[dict[str, Any]]:
        """GET returning a JSON array of objects."""
        payload = await self._send("GET", path, params=params)
        return cast("list[dict[str, Any]]", self._require(payload, list, path))

    # --- writes ------------------------------------------------------------

    async def post(
        self, path: str, *, actor: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """POST returning a JSON object."""
        payload = await self._send("POST", path, actor=actor, json=body or {})
        return cast("dict[str, Any]", self._require(payload, dict, path))
