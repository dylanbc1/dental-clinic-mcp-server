"""Security layer 4 at the MCP boundary.

The backend already answers every failure with a structured envelope. This
module is what turns that envelope into something an LLM can act on in a single
turn, and, just as important, what stops anything else from reaching the model.
A tool that leaks a traceback hands the model a puzzle instead of an
instruction.

The rendering is deliberately text, not JSON: the model reads the tool result as
prose, and `"Sugerencia: los cupos libres más cercanos son 09:00, 09:30"` is
acted on correctly far more often than a nested object it has to traverse.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from backend.domain.errores import CodigoError, ErrorDominio

#: Errors whose remedy is for the *caller* to obtain something (a permission, an
#: approval, a consent) rather than to correct a parameter. Rendered with a
#: stronger lead-in so the model escalates instead of retrying.
CODIGOS_QUE_EXIGEN_ESCALAR: frozenset[CodigoError] = frozenset(
    {
        CodigoError.SCOPE_INSUFICIENTE,
        CodigoError.NO_AUTENTICADO,
        CodigoError.CONSENTIMIENTO_REQUERIDO,
    }
)


class ErrorHerramienta(ToolError):
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
        partes = [f"[{self.codigo}] {self.mensaje}"]
        if self.sugerencia:
            prefijo = (
                "Acción requerida"
                if self.codigo in {str(c) for c in CODIGOS_QUE_EXIGEN_ESCALAR}
                else "Sugerencia"
            )
            partes.append(f"{prefijo}: {self.sugerencia}")
        if self.detalles:
            partes.append(f"Datos: {json.dumps(self.detalles, ensure_ascii=False, default=str)}")
        return "\n".join(partes)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": True, "codigo": self.codigo, "mensaje": self.mensaje}
        if self.sugerencia:
            payload["sugerencia"] = self.sugerencia
        if self.detalles:
            payload["detalles"] = self.detalles
        return payload

    @classmethod
    def desde_envoltura(cls, payload: dict[str, Any]) -> ErrorHerramienta:
        """Rebuild from the backend's JSON envelope."""
        return cls(
            codigo=str(payload.get("codigo", "ERROR_INTERNO")),
            mensaje=str(payload.get("mensaje", "El backend devolvió un error sin detalle.")),
            sugerencia=payload.get("sugerencia"),
            detalles=payload.get("detalles"),
        )

    @classmethod
    def desde_dominio(cls, error: ErrorDominio) -> ErrorHerramienta:
        return cls.desde_envoltura(error.to_dict())


def error_no_autenticado(recurso: str) -> ErrorHerramienta:
    return ErrorHerramienta(
        str(CodigoError.NO_AUTENTICADO),
        "La petición no trae un token de acceso válido.",
        sugerencia=(
            "Autentícate con el Authorization Server (OAuth 2.1 + PKCE) descrito en "
            f"{recurso}/.well-known/oauth-protected-resource y reintenta."
        ),
    )


def error_scope(herramienta: str, requerido: str, presentes: list[str]) -> ErrorHerramienta:
    return ErrorHerramienta(
        str(CodigoError.SCOPE_INSUFICIENTE),
        f"La herramienta '{herramienta}' exige el permiso '{requerido}'.",
        sugerencia=(
            f"Tu token trae {presentes or ['ningún scope']}. Solicita un token que incluya "
            f"'{requerido}' antes de reintentar. No vuelvas a llamar esta herramienta con "
            "el token actual: el resultado será el mismo."
        ),
        detalles={
            "herramienta": herramienta,
            "scope_requerido": requerido,
            "scopes_del_token": presentes,
        },
    )


def error_backend_caido(detalle: str) -> ErrorHerramienta:
    """The backend is unreachable. Not the caller's fault, so say so."""
    return ErrorHerramienta(
        "BACKEND_NO_DISPONIBLE",
        "El sistema de la clínica no está respondiendo.",
        sugerencia=(
            "No es un problema de tu solicitud. Informa al usuario que el sistema está "
            "temporalmente fuera de servicio y no reintentes en bucle."
        ),
        detalles={"detalle": detalle},
    )
