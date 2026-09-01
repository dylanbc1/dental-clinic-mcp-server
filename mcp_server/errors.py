"""Security layer 4 at the MCP boundary.

The backend already answers every failure with a structured envelope. This
module is what turns that envelope into something an LLM can act on in a single
turn, and, just as important, what stops anything else from reaching the model.
A tool that leaks a traceback hands the model a puzzle instead of an
instruction.

The rendering is deliberately text, not JSON: the model reads the tool result as
prose, and `"Suggestion: the closest free slots are 09:00, 09:30"` is acted on
correctly far more often than a nested object it has to traverse.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from backend.domain.errors import DomainError, ErrorCode

#: Errors whose remedy is for the *caller* to obtain something (a permission, an
#: approval, a consent) rather than to correct a parameter. Rendered with a
#: stronger lead-in so the model escalates instead of retrying.
CODES_REQUIRING_ESCALATION: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.INSUFFICIENT_SCOPE,
        ErrorCode.NOT_AUTHENTICATED,
        ErrorCode.CONSENT_REQUIRED,
    }
)


class StructuredToolError(ToolError):
    """A tool failure carrying the structured envelope.

    Subclasses :class:`ToolError` so the SDK returns ``is_error=True`` with the
    message for the model, and logs it at INFO without a traceback. An expected
    failure is not a crash.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        suggestion: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.suggestion = suggestion
        self.details = details or {}
        super().__init__(self.render())

    def render(self) -> str:
        parts = [f"[{self.code}] {self.message}"]
        if self.suggestion:
            prefix = (
                "Action required"
                if self.code in {str(c) for c in CODES_REQUIRING_ESCALATION}
                else "Suggestion"
            )
            parts.append(f"{prefix}: {self.suggestion}")
        if self.details:
            parts.append(f"Details: {json.dumps(self.details, ensure_ascii=False, default=str)}")
        return "\n".join(parts)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": True, "code": self.code, "message": self.message}
        if self.suggestion:
            payload["suggestion"] = self.suggestion
        if self.details:
            payload["details"] = self.details
        return payload

    @classmethod
    def from_envelope(cls, payload: dict[str, Any]) -> StructuredToolError:
        """Rebuild from the backend's JSON envelope."""
        return cls(
            code=str(payload.get("code", "INTERNAL_ERROR")),
            message=str(payload.get("message", "The backend returned an error with no detail.")),
            suggestion=payload.get("suggestion"),
            details=payload.get("details"),
        )

    @classmethod
    def from_domain(cls, error: DomainError) -> StructuredToolError:
        return cls.from_envelope(error.to_dict())


def unauthenticated_error(resource: str) -> StructuredToolError:
    return StructuredToolError(
        str(ErrorCode.NOT_AUTHENTICATED),
        "The request carries no valid access token.",
        suggestion=(
            "Authenticate with the authorization server (OAuth 2.1 + PKCE) described at "
            f"{resource}/.well-known/oauth-protected-resource and try again."
        ),
    )


def scope_error(tool_name: str, required: str, presentes: list[str]) -> StructuredToolError:
    return StructuredToolError(
        str(ErrorCode.INSUFFICIENT_SCOPE),
        f"The tool '{tool_name}' requires the '{required}' permission.",
        suggestion=(
            f"Your token carries {presentes or ['no scopes']}. Request a token that "
            f"includes '{required}' before retrying. Do not call this tool again with "
            "the current token: the result will be the same."
        ),
        details={
            "tool": tool_name,
            "required_scope": required,
            "token_scopes": presentes,
        },
    )


def backend_down_error(detail: str) -> StructuredToolError:
    """The backend is unreachable. Not the caller's fault, so say so."""
    return StructuredToolError(
        "BACKEND_UNAVAILABLE",
        "The clinic's system is not responding.",
        suggestion=(
            "This is not a problem with your request. Tell the user the system is "
            "temporarily unavailable, and do not retry in a loop."
        ),
        details={"detail": detail},
    )
