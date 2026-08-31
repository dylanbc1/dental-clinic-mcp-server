"""How a failure is rendered for the model.

The message a tool returns *is* the recovery instruction. These tests pin the
rendering so a refactor cannot quietly turn an actionable error back into
"something went wrong".
"""

from __future__ import annotations

import pytest

from backend.domain.errores import CodigoError, SlotNoDisponible
from mcp_server.errores import (
    ErrorHerramienta,
    error_backend_caido,
    error_no_autenticado,
    error_scope,
)

pytestmark = pytest.mark.security


class TestRenderizado:
    def test_lo_minimo_es_codigo_y_mensaje(self) -> None:
        error = ErrorHerramienta("X", "algo pasó")
        assert error.render() == "[X] algo pasó"

    def test_la_sugerencia_se_etiqueta_como_tal(self) -> None:
        error = ErrorHerramienta("X", "algo pasó", sugerencia="haz esto otro")
        assert "Sugerencia: haz esto otro" in error.render()

    def test_los_errores_de_permiso_piden_escalar_no_reintentar(self) -> None:
        """ "Sugerencia" invites a retry. A missing permission never resolves by
        retrying, so those get a stronger lead-in."""
        error = ErrorHerramienta(
            str(CodigoError.SCOPE_INSUFICIENTE), "falta permiso", sugerencia="pide write"
        )
        assert "Acción requerida: pide write" in error.render()

    def test_los_detalles_viajan_como_json_legible(self) -> None:
        error = ErrorHerramienta("X", "y", detalles={"slot_id": 4, "libres": [1, 2]})
        assert '"slot_id": 4' in error.render()

    def test_los_detalles_no_reventan_con_tipos_raros(self) -> None:
        from datetime import date

        error = ErrorHerramienta("X", "y", detalles={"cuando": date(2026, 9, 1)})
        assert "2026-09-01" in error.render()

    def test_el_mensaje_de_la_excepcion_es_el_render(self) -> None:
        error = ErrorHerramienta("X", "y", sugerencia="z")
        assert str(error) == error.render()

    def test_to_dict_omite_lo_vacio(self) -> None:
        assert ErrorHerramienta("X", "y").to_dict() == {
            "error": True,
            "codigo": "X",
            "mensaje": "y",
        }


class TestConversiones:
    def test_reconstruye_desde_la_envoltura_del_backend(self) -> None:
        error = ErrorHerramienta.desde_envoltura(
            {
                "error": True,
                "codigo": "SLOT_NO_DISPONIBLE",
                "mensaje": "ocupado",
                "sugerencia": "prueba 09:00",
                "detalles": {"slot_id": 3},
            }
        )
        assert error.codigo == "SLOT_NO_DISPONIBLE"
        assert error.detalles == {"slot_id": 3}

    def test_una_envoltura_incompleta_no_revienta(self) -> None:
        error = ErrorHerramienta.desde_envoltura({})
        assert error.codigo == "ERROR_INTERNO"
        assert error.mensaje

    def test_convierte_un_error_de_dominio(self) -> None:
        dominio = SlotNoDisponible("ocupado", sugerencia="prueba otro", detalles={"a": 1})
        error = ErrorHerramienta.desde_dominio(dominio)
        assert error.codigo == "SLOT_NO_DISPONIBLE"
        assert error.sugerencia == "prueba otro"


class TestErroresPrefabricados:
    def test_no_autenticado_apunta_al_descubrimiento(self) -> None:
        error = error_no_autenticado("http://localhost:8080")
        assert error.codigo == "NO_AUTENTICADO"
        assert "oauth-protected-resource" in (error.sugerencia or "")

    def test_el_de_scope_nombra_lo_que_falta_y_lo_que_hay(self) -> None:
        error = error_scope("cancelar_cita", "write", ["read"])
        assert error.detalles["scope_requerido"] == "write"
        assert error.detalles["scopes_del_token"] == ["read"]
        assert "no vuelvas a llamar" in (error.sugerencia or "").lower()

    def test_el_de_scope_sin_scopes_lo_dice_en_castellano(self) -> None:
        assert "ningún scope" in (error_scope("x", "read", []).sugerencia or "")

    def test_el_de_backend_caido_deslinda_al_llamador(self) -> None:
        """The model must not conclude its own arguments were wrong."""
        error = error_backend_caido("connection refused")
        assert "No es un problema de tu solicitud" in (error.sugerencia or "")
        assert "no reintentes en bucle" in (error.sugerencia or "")
