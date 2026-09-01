"""Security layer 5 (transport): rate limiting.

An agent in a retry loop is the ordinary case, not the adversarial one: a model
that misreads an error can call the same tool hundreds of times a minute. Left
alone that is a self-inflicted denial of service against the clinic's own
database, so the limit is here to protect the *backend*, not to punish the
caller, hence the `Retry-After` header and a body that says how long to wait.

The bucket is keyed by authenticated subject when there is one, and by client
address otherwise: rate-limiting purely by IP would let one misbehaving agent
starve every other user behind the same NAT.

State is in-process, which is honest for a single replica and documented in
`docs/security.md`. Behind more than one, this belongs in Redis.
"""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from backend.domain.errors import ErrorCode


@dataclass(slots=True)
class SlidingWindow:
    """Sliding-window counter.

    A fixed window lets a caller fire the whole budget at the end of one window
    and again at the start of the next: twice the intended rate at the seam.
    """

    limite: int
    ventana: float
    marcas: dict[str, deque[float]] = field(default_factory=dict)

    def allow(self, key: str, *, now: float) -> tuple[bool, float]:
        cola = self.marcas.setdefault(key, deque())
        limite_inferior = now - self.ventana
        while cola and cola[0] < limite_inferior:
            cola.popleft()
        if len(cola) >= self.limite:
            espera = max(0.0, cola[0] + self.ventana - now)
            return False, espera
        cola.append(now)
        return True, 0.0

    def forget(self, key: str) -> None:
        self.marcas.pop(key, None)


class RequestLimiter:
    """ASGI middleware applying the sliding window per caller."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        limite: int = 120,
        ventana_segundos: float = 60.0,
        reloj: object | None = None,
    ) -> None:
        self.app = app
        self.ventana = SlidingWindow(limite=limite, ventana=ventana_segundos)
        self._reloj = reloj or time.monotonic

    def _key(self, scope: Scope) -> str:
        usuario = scope.get("user")
        subject = getattr(getattr(usuario, "access_token", None), "subject", None)
        if subject:
            return f"sub:{subject}"
        client = scope.get("client")
        return f"ip:{client[0] if client else 'desconocido'}"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        now = float(self._reloj())  # type: ignore[operator]
        permitido, espera = self.ventana.allow(self._key(scope), now=now)
        if permitido:
            await self.app(scope, receive, send)
            return

        segundos = max(1, int(espera + 0.999))
        body = json.dumps(
            {
                "error": True,
                "codigo": str(ErrorCode.RATE_LIMIT_EXCEDIDO),
                "mensaje": (
                    f"Demasiadas peticiones: el límite es {self.ventana.limite} por "
                    f"{int(self.ventana.ventana)} segundos."
                ),
                "sugerencia": (
                    f"Espera {segundos} segundos antes de reintentar. Si estás en un bucle "
                    "de reintentos, revisa el último error en vez de repetir la llamada."
                ),
            },
            ensure_ascii=False,
        ).encode()

        inicio: Message = {
            "type": "http.response.start",
            "status": 429,
            "headers": [
                (b"content-type", b"application/json"),
                (b"retry-after", str(segundos).encode()),
                (b"x-ratelimit-limit", str(self.ventana.limite).encode()),
            ],
        }
        await send(inicio)
        await send({"type": "http.response.body", "body": body})
