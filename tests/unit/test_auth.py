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


class TestIdentity:
    def test_has_recognises_the_scope_that_is_present(self) -> None:
        identity = Identity(subject="a", scopes=frozenset({"read"}))
        assert identity.has(Scope.READ)
        assert not identity.has(Scope.WRITE)

    def test_the_open_identity_only_exists_without_auth(self) -> None:
        """It carries every scope on purpose: with authentication off there is
        no security story, and pretending otherwise would make the gate tests
        lie about what they prove."""
        assert OPEN_IDENTITY.subject == ANONYMOUS_ACTOR
        assert all(OPEN_IDENTITY.has(s) for s in Scope)

    def test_without_a_token_and_without_requiring_auth_it_returns_the_open_one(self) -> None:
        assert current_identity(exigir_auth=False) is OPEN_IDENTITY

    def test_without_a_token_and_requiring_auth_it_fails(self) -> None:
        with pytest.raises(StructuredToolError) as exc:
            current_identity(exigir_auth=True)
        assert exc.value.code == "NOT_AUTHENTICATED"


class TestRequireScope:
    def test_lets_through_the_one_that_has_it(self) -> None:
        identity = Identity(subject="a", scopes=frozenset({"write"}))
        assert require_scope(identity, Scope.WRITE, tool_name="x") is identity

    def test_cuts_off_the_one_without_it(self) -> None:
        identity = Identity(subject="a", scopes=frozenset({"read"}))
        with pytest.raises(StructuredToolError) as exc:
            require_scope(identity, Scope.CLINICAL, tool_name="record_visit_reason")
        assert exc.value.code == "INSUFFICIENT_SCOPE"
        assert exc.value.details["herramienta"] == "record_visit_reason"


class TestScopesFromClaims:
    def test_reads_the_standard_space_separated_format(self) -> None:
        assert scopes_from({"scope": "read write"}) == frozenset({"read", "write"})

    def test_also_accepts_a_list(self) -> None:
        assert scopes_from({"scope": ["read"]}) == frozenset({"read"})

    def test_without_a_scope_there_are_no_permissions(self) -> None:
        assert scopes_from({}) == frozenset()


class TestValidateRequestedScopes:
    def test_accepts_the_known_ones(self) -> None:
        assert validate_requested_scopes(["read", "clinical"]) == ["read", "clinical"]

    def test_it_refuses_an_invented_one_instead_of_ignoring_it(self) -> None:
        """Silently dropping an unknown scope leaves the client believing it has
        a permission it never got."""
        with pytest.raises(StructuredToolError) as exc:
            validate_requested_scopes(["read", "admin"])
        assert exc.value.details["desconocidos"] == ["admin"]
        assert "read" in (exc.value.suggestion or "")

    def test_an_empty_list_is_valid(self) -> None:
        assert validate_requested_scopes([]) == []
