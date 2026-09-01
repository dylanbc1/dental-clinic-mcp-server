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
        ErrorCode.SCOPE_INSUFICIENTE,
        ErrorCode.NO_AUTENTICADO,
        ErrorCode.CONSENTIMIENTO_REQUERIDO,
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
        codigo: str,
        mensaje: str,
        *,
        sugerencia: str | None = None,
        detalles: dict[str, Any] | None = None,
    ) -> None:
        self.codigo = codigo
        self.mensaje = mensaje
        self.sugerencia = sugerencia
        self.detalles = detalles or {}
        super().__init__(self.render())

    def render(self) -> str:
        parts = [f"[{self.codigo}] {self.mensaje}"]
        if self.sugerencia:
            prefijo = (
                "Action required"
                if self.codigo in {str(c) for c in CODES_REQUIRING_ESCALATION}
                else "Suggestion"
            )
            parts.append(f"{prefijo}: {self.sugerencia}")
        if self.detalles:
            parts.append(f"Datos: {json.dumps(self.detalles, ensure_ascii=False, default=str)}")
        return "\n".join(parts)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": True, "codigo": self.codigo, "mensaje": self.mensaje}
        if self.sugerencia:
            payload["sugerencia"] = self.sugerencia
        if self.detalles:
            payload["detalles"] = self.detalles
        return payload

    @classmethod
    def from_envelope(cls, payload: dict[str, Any]) -> StructuredToolError:
        """Rebuild from the backend's JSON envelope."""
        return cls(
            codigo=str(payload.get("codigo", "ERROR_INTERNO")),
            mensaje=str(payload.get("mensaje", "The backend returned an error with no detail.")),
            sugerencia=payload.get("sugerencia"),
            detalles=payload.get("detalles"),
        )

    @classmethod
    def from_domain(cls, error: DomainError) -> StructuredToolError:
        return cls.from_envelope(error.to_dict())


def unauthenticated_error(recurso: str) -> StructuredToolError:
    return StructuredToolError(
        str(ErrorCode.NO_AUTENTICADO),
        "The request carries no valid access token.",
        sugerencia=(
            "Authenticate with the authorization server (OAuth 2.1 + PKCE) described at "
            f"{recurso}/.well-known/oauth-protected-resource and try again."
        ),
    )


def scope_error(tool_name: str, requerido: str, presentes: list[str]) -> StructuredToolError:
    return StructuredToolError(
        str(ErrorCode.SCOPE_INSUFICIENTE),
        f"The tool '{tool_name}' requires the '{requerido}' permission.",
        sugerencia=(
            f"Your token carries {presentes or ['no scopes']}. Request a token that "
            f"includes '{requerido}' before retrying. Do not call this tool again with "
            "the current token: the result will be the same."
        ),
        detalles={
            "herramienta": tool_name,
            "scope_requerido": requerido,
            "scopes_del_token": presentes,
        },
    )


def backend_down_error(detalle: str) -> StructuredToolError:
    """The backend is unreachable. Not the caller's fault, so say so."""
    return StructuredToolError(
        "BACKEND_NO_DISPONIBLE",
        "The clinic's system is not responding.",
        sugerencia=(
            "This is not a problem with your request. Tell the user the system is "
            "temporarily unavailable, and do not retry in a loop."
        ),
        detalles={"detalle": detalle},
    )
