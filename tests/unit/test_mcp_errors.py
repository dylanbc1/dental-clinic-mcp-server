"""How a failure is rendered for the model.

The message a tool returns *is* the recovery instruction. These tests pin the
rendering so a refactor cannot quietly turn an actionable error back into
"something went wrong".
"""

from __future__ import annotations

import pytest

from backend.domain.errors import ErrorCode, SlotUnavailable
from mcp_server.errors import (
    StructuredToolError,
    backend_down_error,
    scope_error,
    unauthenticated_error,
)

pytestmark = pytest.mark.security


class TestRenderizado:
    def test_lo_minimo_es_codigo_y_mensaje(self) -> None:
        error = StructuredToolError("X", "algo pasó")
        assert error.render() == "[X] algo pasó"

    def test_la_sugerencia_se_etiqueta_como_tal(self) -> None:
        error = StructuredToolError("X", "algo pasó", suggestion="haz esto otro")
        assert "Suggestion: haz esto otro" in error.render()

    def test_los_errores_de_permiso_piden_escalar_no_reintentar(self) -> None:
        """ "Sugerencia" invites a retry. A missing permission never resolves by
        retrying, so those get a stronger lead-in."""
        error = StructuredToolError(
            str(ErrorCode.INSUFFICIENT_SCOPE), "falta permiso", suggestion="pide write"
        )
        assert "Action required: pide write" in error.render()

    def test_los_detalles_viajan_como_json_legible(self) -> None:
        error = StructuredToolError("X", "y", details={"slot_id": 4, "libres": [1, 2]})
        assert '"slot_id": 4' in error.render()

    def test_los_detalles_no_reventan_con_tipos_raros(self) -> None:
        from datetime import date

        error = StructuredToolError("X", "y", details={"cuando": date(2026, 9, 1)})
        assert "2026-09-01" in error.render()

    def test_el_mensaje_de_la_excepcion_es_el_render(self) -> None:
        error = StructuredToolError("X", "y", suggestion="z")
        assert str(error) == error.render()

    def test_to_dict_omite_lo_vacio(self) -> None:
        assert StructuredToolError("X", "y").to_dict() == {
            "error": True,
            "code": "X",
            "message": "y",
        }


class TestConversiones:
    def test_reconstruye_desde_la_envoltura_del_backend(self) -> None:
        error = StructuredToolError.from_envelope(
            {
                "error": True,
                "code": "SLOT_UNAVAILABLE",
                "message": "ocupado",
                "suggestion": "prueba 09:00",
                "details": {"slot_id": 3},
            }
        )
        assert error.code == "SLOT_UNAVAILABLE"
        assert error.details == {"slot_id": 3}

    def test_una_envoltura_incompleta_no_revienta(self) -> None:
        error = StructuredToolError.from_envelope({})
        assert error.code == "INTERNAL_ERROR"
        assert error.message

    def test_convierte_un_error_de_dominio(self) -> None:
        as_entries = SlotUnavailable("ocupado", suggestion="prueba otro", details={"a": 1})
        error = StructuredToolError.from_domain(as_entries)
        assert error.code == "SLOT_UNAVAILABLE"
        assert error.suggestion == "prueba otro"


class TestErroresPrefabricados:
    def test_no_autenticado_apunta_al_descubrimiento(self) -> None:
        error = unauthenticated_error("http://localhost:8080")
        assert error.code == "NOT_AUTHENTICATED"
        assert "oauth-protected-resource" in (error.suggestion or "")

    def test_el_de_scope_nombra_lo_que_falta_y_lo_que_hay(self) -> None:
        error = scope_error("cancel_appointment", "write", ["read"])
        assert error.details["scope_requerido"] == "write"
        assert error.details["scopes_del_token"] == ["read"]
        assert "do not call this tool again" in (error.suggestion or "").lower()

    def test_el_de_scope_sin_scopes_lo_dice_explicitamente(self) -> None:
        assert "no scopes" in (scope_error("x", "read", []).suggestion or "")

    def test_el_de_backend_caido_deslinda_al_llamador(self) -> None:
        """The model must not conclude its own arguments were wrong."""
        error = backend_down_error("connection refused")
        assert "not a problem with your request" in (error.suggestion or "")
        assert "do not retry in a loop" in (error.suggestion or "")
