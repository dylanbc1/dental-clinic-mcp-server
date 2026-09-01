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

    limit: int
    window: float
    hits: dict[str, deque[float]] = field(default_factory=dict)

    def allow(self, key: str, *, now: float) -> tuple[bool, float]:
        queue = self.hits.setdefault(key, deque())
        lower_bound = now - self.window
        while queue and queue[0] < lower_bound:
            queue.popleft()
        if len(queue) >= self.limit:
            wait = max(0.0, queue[0] + self.window - now)
            return False, wait
        queue.append(now)
        return True, 0.0

    def forget(self, key: str) -> None:
        self.hits.pop(key, None)


class RequestLimiter:
    """ASGI middleware applying the sliding window per caller."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        limit: int = 120,
        window_seconds: float = 60.0,
        clock: object | None = None,
    ) -> None:
        self.app = app
        self.window = SlidingWindow(limit=limit, window=window_seconds)
        self._clock = clock or time.monotonic

    def _key(self, scope: Scope) -> str:
        user = scope.get("user")
        subject = getattr(getattr(user, "access_token", None), "subject", None)
        if subject:
            return f"sub:{subject}"
        client = scope.get("client")
        return f"ip:{client[0] if client else 'unknown'}"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        now = float(self._clock())  # type: ignore[operator]
        allowed, wait = self.window.allow(self._key(scope), now=now)
        if allowed:
            await self.app(scope, receive, send)
            return

        seconds = max(1, int(wait + 0.999))
        body = json.dumps(
            {
                "error": True,
                "code": str(ErrorCode.RATE_LIMIT_EXCEEDED),
                "message": (
                    f"Too many requests: the limit is {self.window.limit} per "
                    f"{int(self.window.window)} seconds."
                ),
                "suggestion": (
                    f"Wait {seconds} seconds before retrying. If you are in a retry "
                    "loop, read the last error instead of repeating the call."
                ),
            },
            ensure_ascii=False,
        ).encode()

        start: Message = {
            "type": "http.response.start",
            "status": 429,
            "headers": [
                (b"content-type", b"application/json"),
                (b"retry-after", str(seconds).encode()),
                (b"x-ratelimit-limit", str(self.window.limit).encode()),
            ],
        }
        await send(start)
        await send({"type": "http.response.body", "body": body})
