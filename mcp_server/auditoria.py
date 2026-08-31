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
CAMPOS_REDACTADOS: frozenset[str] = frozenset(
    {"motivo", "telefono", "email", "documento", "nombre", "token_confirmacion"}
)

REDACTADO = "«redactado»"


def configurar_logging(nivel: str = "INFO") -> None:
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


def redactar(argumentos: dict[str, Any]) -> dict[str, Any]:
    """Drop the values that must not be duplicated into the log."""
    return {
        clave: (REDACTADO if clave in CAMPOS_REDACTADOS and valor is not None else valor)
        for clave, valor in argumentos.items()
    }


class Auditor:
    """Records every tool invocation, successful or not."""

    def __init__(self, logger: Any | None = None) -> None:
        self._log = logger or structlog.get_logger("mcp.auditoria")
        #: Kept in memory so the test-suite can assert on what was recorded
        #: without scraping stdout. Bounded: an audit log is not a data store.
        self.eventos: list[dict[str, Any]] = []
        self.limite_memoria = 500

    def _registrar(self, evento: str, datos: dict[str, Any]) -> None:
        self.eventos.append({"evento": evento, **datos})
        if len(self.eventos) > self.limite_memoria:
            del self.eventos[: len(self.eventos) - self.limite_memoria]
        self._log.info(evento, **datos)

    def invocacion(
        self,
        herramienta: str,
        *,
        sujeto: str,
        scope: str,
        argumentos: dict[str, Any],
        resultado: str,
        codigo_error: str | None = None,
        aprobada: bool | None = None,
    ) -> None:
        datos: dict[str, Any] = {
            "herramienta": herramienta,
            "sujeto": sujeto,
            "scope_requerido": scope,
            "argumentos": redactar(argumentos),
            "resultado": resultado,
        }
        if codigo_error is not None:
            datos["codigo_error"] = codigo_error
        if aprobada is not None:
            datos["con_aprobacion_humana"] = aprobada
        self._registrar("tool.invocacion", datos)

    def propuesta_emitida(self, accion: str, *, sujeto: str, nonce: str) -> None:
        self._registrar(
            "aprobacion.propuesta", {"accion": accion, "sujeto": sujeto, "nonce": nonce}
        )

    def propuesta_confirmada(self, accion: str, *, sujeto: str, nonce: str) -> None:
        self._registrar(
            "aprobacion.confirmada", {"accion": accion, "sujeto": sujeto, "nonce": nonce}
        )

    def acceso_clinico(self, *, sujeto: str, cita_id: int, resultado: str) -> None:
        """Clinical access gets its own event type.

        Res. 2654/2019 asks who touched clinical data; burying that inside the
        generic invocation stream makes it unanswerable at audit time.
        """
        self._registrar(
            "clinico.acceso", {"sujeto": sujeto, "cita_id": cita_id, "resultado": resultado}
        )
