"""Settings tests.

Configuration bugs are silent: a mis-parsed allow-list turns a security control
into a no-op without anything failing. These assertions are cheap insurance.
"""

from __future__ import annotations

import pytest

from backend.config import Settings, get_settings


class TestDefaults:
    def test_starts_with_no_environment_variable_at_all(self) -> None:
        settings_ = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings_.app_env == "development"
        assert settings_.database_url.startswith("postgresql+psycopg://")

    def test_no_real_secret_ships_as_a_default(self) -> None:
        """Defaults must be obviously-local placeholders, never usable creds."""
        key = Settings(_env_file=None).request_state_keys[0]  # type: ignore[call-arg]
        assert "dev-only" in key
        assert "change-me" in key

    def test_get_settings_is_cached(self) -> None:
        assert get_settings() is get_settings()


class TestListsFromTheEnvironment:
    """`a,b,c` must work: forcing JSON into a .env is how allow-lists get typo'd."""

    def test_accepts_a_comma_separated_list(self) -> None:
        settings_ = Settings(  # type: ignore[call-arg]
            _env_file=None, mcp_allowed_origins="http://a.test,http://b.test"
        )
        assert settings_.mcp_allowed_origins == ["http://a.test", "http://b.test"]

    def test_ignores_whitespace_and_empty_entries(self) -> None:
        settings_ = Settings(  # type: ignore[call-arg]
            _env_file=None, mcp_allowed_hosts=" localhost , , 127.0.0.1 ", mcp_port=8080
        )
        assert settings_.mcp_allowed_hosts == [
            "localhost",
            "localhost:8080",
            "127.0.0.1",
            "127.0.0.1:8080",
        ]

    def test_also_accepts_a_python_list(self) -> None:
        settings_ = Settings(  # type: ignore[call-arg]
            _env_file=None, mcp_allowed_hosts=["a", "b"], mcp_port=1234
        )
        assert settings_.mcp_allowed_hosts == ["a", "a:1234", "b", "b:1234"]


class TestHostExpansion:
    """The `Host` header carries a port; a bare allow-list matches nothing."""

    def test_expands_every_host_with_the_configured_port(self) -> None:
        settings_ = Settings(  # type: ignore[call-arg]
            _env_file=None, mcp_allowed_hosts="clinica.example", mcp_port=9443
        )
        assert settings_.mcp_allowed_hosts == ["clinica.example", "clinica.example:9443"]

    def test_it_respects_a_host_that_already_carries_a_port(self) -> None:
        settings_ = Settings(  # type: ignore[call-arg]
            _env_file=None, mcp_allowed_hosts="clinica.example:443", mcp_port=8080
        )
        assert settings_.mcp_allowed_hosts == ["clinica.example:443"]

    def test_it_does_not_duplicate_when_both_forms_are_declared(self) -> None:
        settings_ = Settings(  # type: ignore[call-arg]
            _env_file=None, mcp_allowed_hosts="localhost,localhost:8080", mcp_port=8080
        )
        assert settings_.mcp_allowed_hosts == ["localhost", "localhost:8080"]

    def test_an_empty_list_stays_empty(self) -> None:
        """Empty must keep meaning "nothing allowed", never "everything"."""
        settings_ = Settings(_env_file=None, mcp_allowed_hosts="")  # type: ignore[call-arg]
        assert settings_.mcp_allowed_hosts == []

    def test_an_empty_string_leaves_the_list_empty(self) -> None:
        # An empty allow-list must mean "nothing allowed", never "everything".
        settings_ = Settings(_env_file=None, mcp_allowed_origins="")  # type: ignore[call-arg]
        assert settings_.mcp_allowed_origins == []


class TestEnvironment:
    @pytest.mark.parametrize(
        ("environment", "production"),
        [("development", False), ("test", False), ("production", True)],
    )
    def test_is_production(self, environment: str, production: bool) -> None:
        settings_ = Settings(_env_file=None, app_env=environment)  # type: ignore[call-arg]
        assert settings_.is_production is production

    def test_an_unknown_environment_is_refused(self) -> None:
        with pytest.raises(ValueError, match="app_env"):
            Settings(_env_file=None, app_env="staging")  # type: ignore[call-arg]
