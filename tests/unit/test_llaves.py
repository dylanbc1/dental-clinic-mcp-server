"""Signing-key handling for the authorization server."""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from mcp_server.oauth import llaves as mod
from mcp_server.oauth.llaves import VARIABLE_LLAVE, cargar_desde_entorno, generar, llaves

pytestmark = pytest.mark.security


class TestGeneracion:
    def test_genera_una_llave_rsa_utilizable(self) -> None:
        par = generar()
        assert isinstance(par.privada, rsa.RSAPrivateKey)
        assert par.efimera is True

    def test_el_pem_es_pkcs8_sin_cifrar(self) -> None:
        pem = generar().pem_privada()
        assert pem.startswith("-----BEGIN PRIVATE KEY-----")

    def test_el_jwks_tiene_exactamente_una_clave_publica(self) -> None:
        jwks = generar().jwks()
        assert len(jwks["keys"]) == 1
        assert "d" not in jwks["keys"][0]


class TestCargaDesdeEntorno:
    def test_sin_variable_no_carga_nada(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(VARIABLE_LLAVE, raising=False)
        assert cargar_desde_entorno() is None

    def test_carga_una_llave_provista(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(VARIABLE_LLAVE, generar().pem_privada())
        par = cargar_desde_entorno()
        assert par is not None
        assert par.efimera is False
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
        monkeypatch.setenv(VARIABLE_LLAVE, pem)
        with pytest.raises(ValueError, match="RSA"):
            cargar_desde_entorno()

    def test_la_llave_del_proceso_esta_cacheada(self) -> None:
        mod.llaves.cache_clear()
        try:
            assert llaves() is llaves()
        finally:
            mod.llaves.cache_clear()

    def test_prefiere_la_del_entorno_sobre_una_generada(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(VARIABLE_LLAVE, generar().pem_privada())
        mod.llaves.cache_clear()
        try:
            assert llaves().efimera is False
        finally:
            mod.llaves.cache_clear()
