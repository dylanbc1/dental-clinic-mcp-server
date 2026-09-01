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
from mcp_server.auth import Identity, Scope, current_identity, require_scope
from mcp_server.client import BackendClient
from mcp_server.errors import StructuredToolError


@dataclass(slots=True)
class ToolContext:
    client: BackendClient
    auditor: Auditor = field(default_factory=Auditor)
    #: When False the server runs without an authorization server, which is only
    #: appropriate locally. Production sets it True and the tools refuse an
    #: anonymous call.
    exigir_auth: bool = True

    def identity(self) -> Identity:
        return current_identity(exigir_auth=self.exigir_auth)

    def authorize(self, tool_name: str, scope: Scope) -> Identity:
        """Layers 1 and 2 in one call: authenticate, then check least privilege."""
        return require_scope(self.identity(), scope, tool_name=tool_name)

    def authorize_audited(
        self, tool_name: str, scope: Scope, arguments: dict[str, Any]
    ) -> Identity:
        """Authorise, and record the refusal if there is one.

        A denial is exactly the event an audit log exists for: it is how you find
        out an agent spent an hour calling something it has no permission for.
        """
        try:
            return self.authorize(tool_name, scope)
        except StructuredToolError as error:
            self.auditor.tool_call(
                tool_name,
                subject=self.subject_or_anonymous(),
                scope=str(scope),
                arguments=arguments,
                result="error",
                error_code=error.codigo,
            )
            raise

    def subject_or_anonymous(self) -> str:
        """The caller's subject, or a placeholder when there is no token.

        Used only for logging a refusal, where failing again would lose the very
        event we are trying to record.
        """
        try:
            return self.identity().subject
        except StructuredToolError:
            return "sin-identidad"

    @asynccontextmanager
    async def audit_failure(
        self,
        tool_name: str,
        scope: Scope,
        arguments: dict[str, Any],
        identity: Identity,
    ) -> AsyncIterator[None]:
        """Record a validation failure raised inside the block.

        Validation happens before a human is asked anything, so without this a
        refusal at that stage would leave no trace at all.
        """
        try:
            yield
        except StructuredToolError as error:
            self.auditor.tool_call(
                tool_name,
                subject=identity.subject,
                scope=str(scope),
                arguments=arguments,
                result="error",
                error_code=error.codigo,
            )
            raise
