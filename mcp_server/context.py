"""Everything the tools need, assembled in one place.

Passing this around instead of reaching for module-level globals is what lets
the test-suite run the real server against a real backend while choosing the
exact identity a call arrives with.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from mcp_server.audit import Auditor
from mcp_server.auth import Identidad, Scope, exigir_scope, identidad_actual
from mcp_server.client import ClienteBackend
from mcp_server.errors import ErrorHerramienta


@dataclass(slots=True)
class Contexto:
    cliente: ClienteBackend
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

    def autorizar_auditando(
        self, herramienta: str, scope: Scope, argumentos: dict[str, Any]
    ) -> Identidad:
        """Authorise, and record the refusal if there is one.

        A denial is exactly the event an audit log exists for: it is how you find
        out an agent spent an hour calling something it has no permission for.
        """
        try:
            return self.autorizar(herramienta, scope)
        except ErrorHerramienta as error:
            self.auditor.invocacion(
                herramienta,
                sujeto=self.identidad_o_anonima(),
                scope=str(scope),
                argumentos=argumentos,
                resultado="error",
                codigo_error=error.codigo,
            )
            raise

    def identidad_o_anonima(self) -> str:
        """The caller's subject, or a placeholder when there is no token.

        Used only for logging a refusal, where failing again would lose the very
        event we are trying to record.
        """
        try:
            return self.identidad().sujeto
        except ErrorHerramienta:
            return "sin-identidad"

    @asynccontextmanager
    async def auditar_fallo(
        self,
        herramienta: str,
        scope: Scope,
        argumentos: dict[str, Any],
        identidad: Identidad,
    ) -> AsyncIterator[None]:
        """Record a validation failure raised inside the block.

        Validation happens before a human is asked anything, so without this a
        refusal at that stage would leave no trace at all.
        """
        try:
            yield
        except ErrorHerramienta as error:
            self.auditor.invocacion(
                herramienta,
                sujeto=identidad.sujeto,
                scope=str(scope),
                argumentos=argumentos,
                resultado="error",
                codigo_error=error.codigo,
            )
            raise
