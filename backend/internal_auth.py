"""Authentication for the one hop the MCP server makes into this API.

The domain API used to trust `X-Actor`: whoever called it said who they were,
and the backend wrote that into the audit trail. The header was never an
authorisation decision, so the honest description of the gap is worse than
"a spoofable actor": the API authenticated *nothing*, and anything that could
reach it could write, anonymously, and sign the change with someone else's name.

"It is not reachable from outside" was true of docker-compose and is a property
of a deployment, not of the code. The moment the backend became a service on a
shared private network, the defence moved out of the repository's hands.

**Why a signature and not a shared bearer token.** The MCP server reaches this
API over plain HTTP on an internal network: there is no TLS on that hop. A
static token in a header is then readable by anything that can observe it, and
replayable forever. An HMAC over the request means the key itself never travels,
a captured request cannot be replayed past its window, and the actor is covered
by the signature rather than sitting beside it.

**Why not verify the end user's JWT here instead.** It is the other reasonable
answer, and it was rejected on purpose. It would make the domain API depend on
the authorization server being reachable, duplicate the token-validation logic
that layer 1 already owns, and require a token exchange because the access token's
audience is the MCP server, not this API. The security controls stay in one
place; this module only proves that the caller *is* that place.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Sequence

#: Bumped if the canonical string ever changes shape, so an old client fails
#: loudly instead of silently mismatching.
SCHEME = "v1"

#: Headers carrying the proof. `X-Actor` is unchanged: it is now covered by the
#: signature rather than trusted on its own.
ACTOR_HEADER = "X-Actor"
TIMESTAMP_HEADER = "X-Timestamp"
SIGNATURE_HEADER = "X-Signature"


def canonical_request(
    *, method: str, path: str, query: str, actor: str, body: bytes, timestamp: int
) -> bytes:
    """The exact bytes both sides sign.

    Every field that changes the meaning of the call is in here. The body is
    hashed rather than included so a large payload does not have to be held
    twice, and the timestamp is inside the string so it cannot be edited to
    extend a captured request's life.
    """
    digest = hashlib.sha256(body).hexdigest()
    parts = (SCHEME, method.upper(), path, query, actor, digest, str(timestamp))
    return "\n".join(parts).encode()


def sign(key: str, message: bytes) -> str:
    return f"{SCHEME}=" + hmac.new(key.encode(), message, hashlib.sha256).hexdigest()


def verify(
    keys: Sequence[str],
    message: bytes,
    presented: str,
    *,
    timestamp: int,
    now: float,
    skew_seconds: float,
) -> bool:
    """True when `presented` matches under any key in the ring and is fresh.

    The ring is what makes rotation possible without downtime: ship [old, new],
    then [new, old], then [new]. Comparison is constant-time, and the freshness
    check runs first because an expired signature is not worth comparing.
    """
    if abs(now - timestamp) > skew_seconds:
        return False
    return any(hmac.compare_digest(sign(key, message), presented) for key in keys)


def sign_request(
    key: str,
    *,
    method: str,
    path: str,
    query: str = "",
    actor: str,
    body: bytes = b"",
    timestamp: int | None = None,
) -> dict[str, str]:
    """The three headers a caller must attach. Used by the MCP server's client
    and by `scripts/call_api.py`, so there is exactly one implementation."""
    moment = int(time.time()) if timestamp is None else timestamp
    message = canonical_request(
        method=method, path=path, query=query, actor=actor, body=body, timestamp=moment
    )
    return {
        ACTOR_HEADER: actor,
        TIMESTAMP_HEADER: str(moment),
        SIGNATURE_HEADER: sign(key, message),
    }
