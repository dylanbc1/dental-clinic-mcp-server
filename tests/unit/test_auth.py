"""Identity and scope helpers, in isolation."""

from __future__ import annotations

import pytest

from mcp_server.auth import (
    ACTOR_ANONIMO,
    IDENTIDAD_ABIERTA,
    Identidad,
    Scope,
    exigir_scope,
    identidad_actual,
    scopes_de,
    validar_scopes_solicitados,
)
from mcp_server.errors import ErrorHerramienta

pytestmark = pytest.mark.security


class TestIdentidad:
    def test_tiene_reconoce_el_scope_presente(self) -> None:
        identidad = Identidad(sujeto="a", scopes=frozenset({"read"}))
        assert identidad.tiene(Scope.READ)
        assert not identidad.tiene(Scope.WRITE)

    def test_la_identidad_abierta_solo_existe_sin_auth(self) -> None:
        """It carries every scope on purpose: with authentication off there is
        no security story, and pretending otherwise would make the gate tests
        lie about what they prove."""
        assert IDENTIDAD_ABIERTA.sujeto == ACTOR_ANONIMO
        assert all(IDENTIDAD_ABIERTA.tiene(s) for s in Scope)

    def test_sin_token_y_sin_exigir_auth_devuelve_la_abierta(self) -> None:
        assert identidad_actual(exigir_auth=False) is IDENTIDAD_ABIERTA

    def test_sin_token_y_exigiendo_auth_falla(self) -> None:
        with pytest.raises(ErrorHerramienta) as exc:
            identidad_actual(exigir_auth=True)
        assert exc.value.codigo == "NO_AUTENTICADO"


class TestExigirScope:
    def test_deja_pasar_al_que_lo_tiene(self) -> None:
        identidad = Identidad(sujeto="a", scopes=frozenset({"write"}))
        assert exigir_scope(identidad, Scope.WRITE, herramienta="x") is identidad

    def test_corta_al_que_no(self) -> None:
        identidad = Identidad(sujeto="a", scopes=frozenset({"read"}))
        with pytest.raises(ErrorHerramienta) as exc:
            exigir_scope(identidad, Scope.CLINICAL, herramienta="registrar_motivo_consulta")
        assert exc.value.codigo == "SCOPE_INSUFICIENTE"
        assert exc.value.detalles["herramienta"] == "registrar_motivo_consulta"


class TestScopesDeClaims:
    def test_lee_el_formato_estandar_separado_por_espacios(self) -> None:
        assert scopes_de({"scope": "read write"}) == frozenset({"read", "write"})

    def test_acepta_tambien_una_lista(self) -> None:
        assert scopes_de({"scope": ["read"]}) == frozenset({"read"})

    def test_sin_scope_no_hay_permisos(self) -> None:
        assert scopes_de({}) == frozenset()


class TestValidarScopesSolicitados:
    def test_acepta_los_conocidos(self) -> None:
        assert validar_scopes_solicitados(["read", "clinical"]) == ["read", "clinical"]

    def test_rechaza_uno_inventado_en_vez_de_ignorarlo(self) -> None:
        """Silently dropping an unknown scope leaves the client believing it has
        a permission it never got."""
        with pytest.raises(ErrorHerramienta) as exc:
            validar_scopes_solicitados(["read", "admin"])
        assert exc.value.detalles["desconocidos"] == ["admin"]
        assert "read" in (exc.value.sugerencia or "")

    def test_una_lista_vacia_es_valida(self) -> None:
        assert validar_scopes_solicitados([]) == []
