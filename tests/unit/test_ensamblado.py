"""Wiring of the MCP server: what `crear_servidor` and friends actually build."""

from __future__ import annotations

import pytest

from backend.config import Settings
from mcp_server.cliente import ClienteBackend
from mcp_server.contexto import Contexto
from mcp_server.server import (
    SCOPES_SOPORTADOS,
    construir_auth,
    contexto_gestionado,
    crear_contexto,
    crear_servidor,
)

pytestmark = pytest.mark.security


@pytest.fixture
def ajustes() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        app_env="test",
        mcp_public_url="http://localhost:8080",
        oauth_issuer="http://localhost:9000",
        oauth_audience="http://localhost:8080",
    )


class TestContexto:
    def test_crear_contexto_usa_los_ajustes(self, ajustes: Settings) -> None:
        assert crear_contexto(ajustes).cliente._base_url == ajustes.backend_base_url

    def test_la_auth_esta_activa_por_defecto(self) -> None:
        """Fail closed. Deriving this from the environment name would mean a
        typo in APP_ENV silently disables authentication."""
        assert Settings(_env_file=None).mcp_auth_enabled is True  # type: ignore[call-arg]
        assert crear_contexto(Settings(_env_file=None)).exigir_auth is True  # type: ignore[call-arg]

    def test_se_puede_desactivar_explicitamente(self) -> None:
        abierto = Settings(_env_file=None, mcp_auth_enabled=False)  # type: ignore[call-arg]
        assert crear_contexto(abierto).exigir_auth is False

    def test_el_parametro_gana_sobre_la_configuracion(self, ajustes: Settings) -> None:
        assert crear_contexto(ajustes, exigir_auth=False).exigir_auth is False

    def test_el_contexto_gestionado_entrega_uno_utilizable(self, ajustes: Settings) -> None:
        with contexto_gestionado(ajustes=ajustes) as ctx:
            assert isinstance(ctx, Contexto)


class TestAuth:
    def test_construye_los_ajustes_de_resource_server(self, ajustes: Settings) -> None:
        settings, verificador = construir_auth(ajustes)
        assert str(settings.issuer_url).rstrip("/") == ajustes.oauth_issuer
        assert str(settings.resource_server_url).rstrip("/") == ajustes.mcp_public_url
        assert verificador.jwks_uri == f"{ajustes.oauth_issuer}/jwks.json"

    def test_el_jwks_interno_se_puede_separar_del_emisor_publico(self) -> None:
        """In Docker the issuer is only resolvable from the host, so the URL the
        resource server fetches keys from is configured separately."""
        ajustes = Settings(  # type: ignore[call-arg]
            _env_file=None,
            oauth_issuer="http://localhost:9000",
            oauth_jwks_url="http://oauth:9000/jwks.json",
        )
        _, verificador = construir_auth(ajustes)
        assert verificador.issuer == "http://localhost:9000"
        assert verificador.jwks_uri == "http://oauth:9000/jwks.json"

    def test_por_defecto_el_jwks_sale_del_emisor(self, ajustes: Settings) -> None:
        assert ajustes.jwks_url == "http://localhost:9000/jwks.json"

    def test_no_declara_scopes_obligatorios_globales(self, ajustes: Settings) -> None:
        """A blanket requirement would make every tool need every scope, which
        is the opposite of least privilege."""
        settings, _ = construir_auth(ajustes)
        assert settings.required_scopes == []

    def test_el_verificador_ata_la_audiencia(self, ajustes: Settings) -> None:
        _, verificador = construir_auth(ajustes)
        assert verificador.audience == ajustes.oauth_audience


class TestEstadoDeLaPeticion:
    """The sealed state that carries a paused operation between rounds."""

    def test_hay_un_anillo_de_claves_por_defecto(self) -> None:
        ajustes = Settings(_env_file=None)  # type: ignore[call-arg]
        assert ajustes.request_state_keys
        assert all(len(k) >= 32 for k in ajustes.request_state_keys)

    def test_la_clave_por_defecto_se_anuncia_como_de_desarrollo(self) -> None:
        """A default that looks like a real secret is a default someone ships."""
        ajustes = Settings(_env_file=None)  # type: ignore[call-arg]
        assert "dev-only" in ajustes.request_state_keys[0]
        assert "change-me" in ajustes.request_state_keys[0]

    def test_el_anillo_acepta_varias_claves_para_rotar(self) -> None:
        ajustes = Settings(  # type: ignore[call-arg]
            _env_file=None, request_state_keys="a" * 32 + "," + "b" * 32
        )
        assert len(ajustes.request_state_keys) == 2

    def test_la_vigencia_es_corta_por_defecto(self) -> None:
        """An approval granted this morning must not authorise an action tonight."""
        ajustes = Settings(_env_file=None)  # type: ignore[call-arg]
        assert 0 < ajustes.request_state_ttl_seconds <= 900


class TestServidor:
    def test_los_scopes_soportados_son_los_tres(self) -> None:
        assert SCOPES_SOPORTADOS == ["read", "write", "clinical"]

    def test_se_puede_construir_con_auth(self, ajustes: Settings) -> None:
        servidor = crear_servidor(
            Contexto(cliente=ClienteBackend("http://x")), config=ajustes, con_auth=True
        )
        assert servidor.name == "clinica-odontologica"
        assert servidor.version == "0.1.0"

    def test_las_instrucciones_nombran_las_dos_reglas(self, ajustes: Settings) -> None:
        instrucciones = (
            crear_servidor(
                Contexto(cliente=ClienteBackend("http://x")), config=ajustes
            ).instructions
            or ""
        ).lower()
        assert "confirmación" in instrucciones
        assert "retries the same call" in instrucciones
        assert "consent" in instrucciones
