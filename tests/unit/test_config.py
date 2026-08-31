"""Settings tests.

Configuration bugs are silent: a mis-parsed allow-list turns a security control
into a no-op without anything failing. These assertions are cheap insurance.
"""

from __future__ import annotations

import pytest

from backend.config import Settings, get_settings


class TestValoresPorDefecto:
    def test_arranca_sin_ninguna_variable_de_entorno(self) -> None:
        ajustes = Settings(_env_file=None)  # type: ignore[call-arg]
        assert ajustes.app_env == "development"
        assert ajustes.database_url.startswith("postgresql+psycopg://")

    def test_ningun_secreto_real_viene_por_defecto(self) -> None:
        """Defaults must be obviously-local placeholders, never usable creds."""
        clave = Settings(_env_file=None).request_state_keys[0]  # type: ignore[call-arg]
        assert "dev-only" in clave
        assert "change-me" in clave

    def test_get_settings_esta_cacheado(self) -> None:
        assert get_settings() is get_settings()


class TestListasDesdeElEntorno:
    """`a,b,c` must work: forcing JSON into a .env is how allow-lists get typo'd."""

    def test_acepta_una_lista_separada_por_comas(self) -> None:
        ajustes = Settings(  # type: ignore[call-arg]
            _env_file=None, mcp_allowed_origins="http://a.test,http://b.test"
        )
        assert ajustes.mcp_allowed_origins == ["http://a.test", "http://b.test"]

    def test_ignora_espacios_y_entradas_vacias(self) -> None:
        ajustes = Settings(  # type: ignore[call-arg]
            _env_file=None, mcp_allowed_hosts=" localhost , , 127.0.0.1 ", mcp_port=8080
        )
        assert ajustes.mcp_allowed_hosts == [
            "localhost",
            "localhost:8080",
            "127.0.0.1",
            "127.0.0.1:8080",
        ]

    def test_acepta_tambien_una_lista_de_python(self) -> None:
        ajustes = Settings(  # type: ignore[call-arg]
            _env_file=None, mcp_allowed_hosts=["a", "b"], mcp_port=1234
        )
        assert ajustes.mcp_allowed_hosts == ["a", "a:1234", "b", "b:1234"]


class TestExpansionDeHosts:
    """The `Host` header carries a port; a bare allow-list matches nothing."""

    def test_expande_cada_host_con_el_puerto_configurado(self) -> None:
        ajustes = Settings(  # type: ignore[call-arg]
            _env_file=None, mcp_allowed_hosts="clinica.example", mcp_port=9443
        )
        assert ajustes.mcp_allowed_hosts == ["clinica.example", "clinica.example:9443"]

    def test_respeta_un_host_que_ya_trae_puerto(self) -> None:
        ajustes = Settings(  # type: ignore[call-arg]
            _env_file=None, mcp_allowed_hosts="clinica.example:443", mcp_port=8080
        )
        assert ajustes.mcp_allowed_hosts == ["clinica.example:443"]

    def test_no_duplica_si_ambas_formas_estan_declaradas(self) -> None:
        ajustes = Settings(  # type: ignore[call-arg]
            _env_file=None, mcp_allowed_hosts="localhost,localhost:8080", mcp_port=8080
        )
        assert ajustes.mcp_allowed_hosts == ["localhost", "localhost:8080"]

    def test_una_lista_vacia_sigue_vacia(self) -> None:
        """Empty must keep meaning "nothing allowed", never "everything"."""
        ajustes = Settings(_env_file=None, mcp_allowed_hosts="")  # type: ignore[call-arg]
        assert ajustes.mcp_allowed_hosts == []

    def test_una_cadena_vacia_deja_la_lista_vacia(self) -> None:
        # An empty allow-list must mean "nothing allowed", never "everything".
        ajustes = Settings(_env_file=None, mcp_allowed_origins="")  # type: ignore[call-arg]
        assert ajustes.mcp_allowed_origins == []


class TestEntorno:
    @pytest.mark.parametrize(
        ("entorno", "produccion"),
        [("development", False), ("test", False), ("production", True)],
    )
    def test_is_production(self, entorno: str, produccion: bool) -> None:
        ajustes = Settings(_env_file=None, app_env=entorno)  # type: ignore[call-arg]
        assert ajustes.is_production is produccion

    def test_un_entorno_desconocido_se_rechaza(self) -> None:
        with pytest.raises(ValueError, match="app_env"):
            Settings(_env_file=None, app_env="staging")  # type: ignore[call-arg]
