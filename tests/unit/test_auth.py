"""Identity and scope helpers, in isolation."""

from __future__ import annotations

import pytest

from mcp_server.auth import (
    ANONYMOUS_ACTOR,
    OPEN_IDENTITY,
    Identity,
    Scope,
    current_identity,
    require_scope,
    scopes_from,
    validate_requested_scopes,
)
from mcp_server.errors import StructuredToolError

pytestmark = pytest.mark.security


class TestIdentidad:
    def test_tiene_reconoce_el_scope_presente(self) -> None:
        identity = Identity(subject="a", scopes=frozenset({"read"}))
        assert identity.has(Scope.READ)
        assert not identity.has(Scope.WRITE)

    def test_la_identidad_abierta_solo_existe_sin_auth(self) -> None:
        """It carries every scope on purpose: with authentication off there is
        no security story, and pretending otherwise would make the gate tests
        lie about what they prove."""
        assert OPEN_IDENTITY.subject == ANONYMOUS_ACTOR
        assert all(OPEN_IDENTITY.has(s) for s in Scope)

    def test_sin_token_y_sin_exigir_auth_devuelve_la_abierta(self) -> None:
        assert current_identity(exigir_auth=False) is OPEN_IDENTITY

    def test_sin_token_y_exigiendo_auth_falla(self) -> None:
        with pytest.raises(StructuredToolError) as exc:
            current_identity(exigir_auth=True)
        assert exc.value.code == "NOT_AUTHENTICATED"


class TestExigirScope:
    def test_deja_pasar_al_que_lo_tiene(self) -> None:
        identity = Identity(subject="a", scopes=frozenset({"write"}))
        assert require_scope(identity, Scope.WRITE, tool_name="x") is identity

    def test_corta_al_que_no(self) -> None:
        identity = Identity(subject="a", scopes=frozenset({"read"}))
        with pytest.raises(StructuredToolError) as exc:
            require_scope(identity, Scope.CLINICAL, tool_name="record_visit_reason")
        assert exc.value.code == "INSUFFICIENT_SCOPE"
        assert exc.value.details["herramienta"] == "record_visit_reason"


class TestScopesDeClaims:
    def test_lee_el_formato_estandar_separado_por_espacios(self) -> None:
        assert scopes_from({"scope": "read write"}) == frozenset({"read", "write"})

    def test_acepta_tambien_una_lista(self) -> None:
        assert scopes_from({"scope": ["read"]}) == frozenset({"read"})

    def test_sin_scope_no_hay_permisos(self) -> None:
        assert scopes_from({}) == frozenset()


class TestValidarScopesSolicitados:
    def test_acepta_los_conocidos(self) -> None:
        assert validate_requested_scopes(["read", "clinical"]) == ["read", "clinical"]

    def test_rechaza_uno_inventado_en_vez_de_ignorarlo(self) -> None:
        """Silently dropping an unknown scope leaves the client believing it has
        a permission it never got."""
        with pytest.raises(StructuredToolError) as exc:
            validate_requested_scopes(["read", "admin"])
        assert exc.value.details["desconocidos"] == ["admin"]
        assert "read" in (exc.value.suggestion or "")

    def test_una_lista_vacia_es_valida(self) -> None:
        assert validate_requested_scopes([]) == []
