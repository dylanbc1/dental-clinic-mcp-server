"""Everything the tools need, assembled in one place.

Passing this around instead of reaching for module-level globals is what lets
the contract suite run the real server against a fake backend, and the security
suite run it with authentication forced on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mcp_server.aprobacion import GestorDeAprobaciones, formatear_propuesta
from mcp_server.auditoria import Auditor
from mcp_server.auth import Identidad, Scope, exigir_scope, identidad_actual
from mcp_server.cliente import ClienteBackend


@dataclass(slots=True)
class Contexto:
    cliente: ClienteBackend
    aprobaciones: GestorDeAprobaciones
    auditor: Auditor = field(default_factory=Auditor)
    #: When False the server runs without an authorization server, which is only
    #: appropriate locally. Production sets it True and the tools refuse an
    #: anonymous call.
    exigir_auth: bool = True

    def identidad(self) -> Identidad:
        return identidad_actual(exigir_auth=self.exigir_auth)

    def autorizar(self, herramienta: str, scope: Scope) -> Identidad:
        """Layers 1 and 2 in one call: authenticate, then check least privilege."""
        return exigir_scope(self.identidad(), scope, herramienta=herramienta)

    def proponer(
        self,
        accion: str,
        argumentos: dict[str, Any],
        *,
        resumen: str,
        efectos: list[str],
        sujeto: str,
        advertencias: list[str] | None = None,
    ) -> dict[str, Any]:
        """Layer 3: return a proposal instead of acting."""
        propuesta, token = self.aprobaciones.proponer(
            accion,
            argumentos,
            resumen=resumen,
            efectos=efectos,
            sujeto=sujeto,
            advertencias=advertencias,
        )
        self.auditor.propuesta_emitida(accion, sujeto=sujeto, nonce=propuesta.nonce)
        return formatear_propuesta(propuesta, token, self.aprobaciones.ttl)
