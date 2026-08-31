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

from mcp_server.errores import ErrorHerramienta, error_backend_caido

TIEMPO_LIMITE = httpx.Timeout(10.0, connect=5.0)


class ClienteBackend:
    """Thin async wrapper over the internal API.

    `actor` is forwarded as `X-Actor` on every write so the audit trail records
    *who* asked, not just that the MCP server did.
    """

    def __init__(self, base_url: str, *, cliente: httpx.AsyncClient | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._propio = cliente is None
        self._cliente = cliente or httpx.AsyncClient(base_url=self._base_url, timeout=TIEMPO_LIMITE)

    async def aclose(self) -> None:
        if self._propio:
            await self._cliente.aclose()

    async def _pedir(
        self,
        metodo: str,
        ruta: str,
        *,
        actor: str | None = None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        cabeceras = {"X-Actor": actor} if actor else None
        limpios = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            respuesta = await self._cliente.request(
                metodo, ruta, params=limpios or None, json=json, headers=cabeceras
            )
        except httpx.HTTPError as exc:
            raise error_backend_caido(str(exc)) from exc

        if respuesta.is_success:
            return respuesta.json()

        try:
            payload = respuesta.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict) and payload.get("error"):
            raise ErrorHerramienta.desde_envoltura(payload)
        raise ErrorHerramienta(
            "ERROR_INTERNO",
            f"El backend respondió {respuesta.status_code} sin un error estructurado.",
            sugerencia="Reintenta en unos segundos; si persiste, reporta el incidente.",
            detalles={"status": respuesta.status_code},
        )

    @staticmethod
    def _exigir(datos: Any, tipo: type, ruta: str) -> Any:
        """Assert the response shape before handing it to a tool.

        Not paranoia: the tools declare typed return values, and a backend that
        answers with an unexpected shape should fail here, where the error names
        the endpoint, rather than three frames later inside a formatter.
        """
        if not isinstance(datos, tipo):
            raise ErrorHerramienta(
                "RESPUESTA_INESPERADA",
                f"{ruta} devolvió {type(datos).__name__} en vez de {tipo.__name__}.",
                sugerencia="Es un fallo del backend, no de tu solicitud. Repórtalo.",
            )
        return datos

    # --- reads -------------------------------------------------------------

    async def obtener(self, ruta: str, **params: Any) -> dict[str, Any]:
        """GET returning a JSON object."""
        datos = await self._pedir("GET", ruta, params=params)
        return cast("dict[str, Any]", self._exigir(datos, dict, ruta))

    async def listar(self, ruta: str, **params: Any) -> list[dict[str, Any]]:
        """GET returning a JSON array of objects."""
        datos = await self._pedir("GET", ruta, params=params)
        return cast("list[dict[str, Any]]", self._exigir(datos, list, ruta))

    # --- writes ------------------------------------------------------------

    async def enviar(
        self, ruta: str, *, actor: str, cuerpo: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """POST returning a JSON object."""
        datos = await self._pedir("POST", ruta, actor=actor, json=cuerpo or {})
        return cast("dict[str, Any]", self._exigir(datos, dict, ruta))
