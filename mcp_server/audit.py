"""Security layer 5 (server side): the audit log.

Two separate things are recorded, and conflating them is a common mistake:

* **State changes** live in `cita_historial`, written by the backend inside the
  same transaction as the change. That is the record a regulator would ask for.
* **Tool invocations** live here. Who called what, with which scope, whether it
  was approved, and whether it succeeded, including the calls that were
  refused. A log that records only successes cannot tell you an agent spent an
  hour trying to use a scope it does not have.

Output is structured JSON so it can be shipped to any log pipeline without
parsing prose. Arguments are recorded, but the fields that can carry clinical
data or personal contact details are redacted: an audit log is not an excuse to
copy patient data into a second, less protected place.
"""

from __future__ import annotations

import logging
from typing import Any

import structlog

#: Argument values that never reach the log. `motivo` is the reason for
#: consultation, clinical data under Res. 2654/2019; the rest are identifiers.
#: The fact of the call is auditable, the content is not copied.
REDACTED_FIELDS: frozenset[str] = frozenset(
    {"motivo", "telefono", "email", "documento", "nombre", "token_confirmacion"}
)

REDACTED = "«redacted»"


def configure_logging(nivel: str = "INFO") -> None:
    """JSON logs, one event per line, with an ISO timestamp."""
    logging.basicConfig(format="%(message)s", level=getattr(logging, nivel.upper(), logging.INFO))
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, nivel.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


def redact(arguments: dict[str, Any]) -> dict[str, Any]:
    """Drop the values that must not be duplicated into the log."""
    return {
        key: (REDACTED if key in REDACTED_FIELDS and value is not None else value)
        for key, value in arguments.items()
    }


class Auditor:
    """Records every tool invocation, successful or not."""

    def __init__(self, logger: Any | None = None) -> None:
        self._log = logger or structlog.get_logger("mcp.audit")
        #: Kept in memory so the test-suite can assert on what was recorded
        #: without scraping stdout. Bounded: an audit log is not a data store.
        self.events: list[dict[str, Any]] = []
        self.memory_limit = 500

    def _record(self, evento: str, payload: dict[str, Any]) -> None:
        self.events.append({"event": evento, **payload})
        if len(self.events) > self.memory_limit:
            del self.events[: len(self.events) - self.memory_limit]
        self._log.info(evento, **payload)

    def tool_call(
        self,
        tool_name: str,
        *,
        subject: str,
        scope: str,
        arguments: dict[str, Any],
        result: str,
        error_code: str | None = None,
        approved: bool | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "tool": tool_name,
            "subject": subject,
            "required_scope": scope,
            "arguments": redact(arguments),
            "result": result,
        }
        if error_code is not None:
            payload["error_code"] = error_code
        if approved is not None:
            payload["with_human_approval"] = approved
        self._record("tool.invocation", payload)

    def question_asked(self, action: str, *, subject: str, nonce: str) -> None:
        self._record("approval.proposed", {"action": action, "subject": subject, "nonce": nonce})

    def question_answered(self, action: str, *, subject: str, nonce: str) -> None:
        self._record("approval.confirmed", {"action": action, "subject": subject, "nonce": nonce})

    def clinical_access(self, *, subject: str, cita_id: int, result: str) -> None:
        """Clinical access gets its own event type.

        Res. 2654/2019 asks who touched clinical data; burying that inside the
        generic invocation stream makes it unanswerable at audit time.
        """
        self._record("clinical.access", {"subject": subject, "cita_id": cita_id, "result": result})
