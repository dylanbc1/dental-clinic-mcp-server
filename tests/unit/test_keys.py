"""Signing-key handling for the authorization server."""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from mcp_server.oauth import keys as mod
from mcp_server.oauth.keys import KEY_ENV_VAR, generate, load_from_env, signing_keys

pytestmark = pytest.mark.security


class TestGeneracion:
    def test_genera_una_llave_rsa_utilizable(self) -> None:
        par = generate()
        assert isinstance(par.private, rsa.RSAPrivateKey)
        assert par.ephemeral is True

    def test_el_pem_es_pkcs8_sin_cifrar(self) -> None:
        pem = generate().private_pem()
        assert pem.startswith("-----BEGIN PRIVATE KEY-----")

    def test_el_jwks_tiene_exactamente_una_clave_publica(self) -> None:
        jwks = generate().jwks()
        assert len(jwks["keys"]) == 1
        assert "d" not in jwks["keys"][0]


class TestCargaDesdeEntorno:
    def test_sin_variable_no_carga_nada(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(KEY_ENV_VAR, raising=False)
        assert load_from_env() is None

    def test_carga_una_llave_provista(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(KEY_ENV_VAR, generate().private_pem())
        par = load_from_env()
        assert par is not None
        assert par.ephemeral is False
        assert par.kid == "entorno"

    def test_una_llave_que_no_es_rsa_se_rechaza(self, monkeypatch: pytest.MonkeyPatch) -> None:
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

    def test_la_llave_del_proceso_esta_cacheada(self) -> None:
        mod.signing_keys.cache_clear()
        try:
            assert signing_keys() is signing_keys()
        finally:
            mod.signing_keys.cache_clear()

    def test_prefiere_la_del_entorno_sobre_una_generada(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(KEY_ENV_VAR, generate().private_pem())
        mod.signing_keys.cache_clear()
        try:
            assert signing_keys().ephemeral is False
        finally:
            mod.signing_keys.cache_clear()
