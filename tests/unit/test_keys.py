"""Signing-key handling for the authorization server."""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from mcp_server.oauth import keys as mod
from mcp_server.oauth.keys import KEY_ENV_VAR, generate, load_from_env, signing_keys

pytestmark = pytest.mark.security


class TestGeneration:
    def test_generates_a_usable_rsa_key(self) -> None:
        pair = generate()
        assert isinstance(pair.private, rsa.RSAPrivateKey)
        assert pair.ephemeral is True

    def test_the_pem_is_unencrypted_pkcs8(self) -> None:
        pem = generate().private_pem()
        assert pem.startswith("-----BEGIN PRIVATE KEY-----")

    def test_the_jwks_holds_exactly_one_public_key(self) -> None:
        jwks = generate().jwks()
        assert len(jwks["keys"]) == 1
        assert "d" not in jwks["keys"][0]


class TestLoadingFromEnv:
    def test_with_no_variable_it_loads_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(KEY_ENV_VAR, raising=False)
        assert load_from_env() is None

    def test_loads_a_supplied_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(KEY_ENV_VAR, generate().private_pem())
        pair = load_from_env()
        assert pair is not None
        assert pair.ephemeral is False
        assert pair.kid == "environment"

    def test_a_key_that_is_not_rsa_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519

        pem = (
            ed25519.Ed25519PrivateKey.generate()
            .private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
            .decode()
        )
        monkeypatch.setenv(KEY_ENV_VAR, pem)
        with pytest.raises(ValueError, match="RSA"):
            load_from_env()

    def test_the_process_key_is_cached(self) -> None:
        mod.signing_keys.cache_clear()
        try:
            assert signing_keys() is signing_keys()
        finally:
            mod.signing_keys.cache_clear()

    def test_it_prefers_the_env_key_over_a_generated_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(KEY_ENV_VAR, generate().private_pem())
        mod.signing_keys.cache_clear()
        try:
            assert signing_keys().ephemeral is False
        finally:
            mod.signing_keys.cache_clear()
