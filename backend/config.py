"""Application settings.

Everything configurable lives here and comes from the environment. No secret is
read from a tracked file: `.env.example` documents the shape, `.env` holds local
values and is git-ignored.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- runtime -----------------------------------------------------------
    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"

    # --- database ----------------------------------------------------------
    database_url: str = "postgresql+psycopg://clinica:clinica_dev_only@localhost:5433/clinica"
    db_echo: bool = False
    db_pool_size: int = 5
    db_max_overflow: int = 10

    # --- domain backend ----------------------------------------------------
    # Loopback by default. Binding every interface is a deployment decision, so
    # docker-compose sets 0.0.0.0 explicitly. The code never assumes it.
    backend_host: str = "127.0.0.1"
    backend_port: int = 8000
    #: Where the MCP server reaches the backend. Separate from `backend_host`,
    #: which is a bind address: in Docker the service binds 0.0.0.0 but is
    #: reached at http://backend:8000. Conflating them breaks one or the other.
    backend_base_url: str = "http://127.0.0.1:8000"

    # --- deterministic mock data ------------------------------------------
    seed_value: int = 20260831
    seed_pacientes: int = 60
    seed_dias_agenda: int = 21

    # --- MCP server (M3+) --------------------------------------------------
    mcp_host: str = "127.0.0.1"  # see backend_host
    mcp_port: int = 8080
    mcp_public_url: str = "http://localhost:8080"
    # NoDecode: read these as a plain `a,b,c` string and let `_split_csv` do the
    # rest. Without it pydantic-settings insists on JSON in the env file.
    mcp_allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:8080", "http://127.0.0.1:8080"]
    )
    mcp_allowed_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["localhost", "127.0.0.1"]
    )

    # Protects the database from an agent stuck in a retry loop, a far more
    # common failure than a hostile client.
    #: Stateless transport. Nothing here needs a session: identity comes from
    #: the token and a pending approval travels in the confirmation token, so a
    #: session would only be state to lose. Configurable because a future
    #: feature could need resumability.
    mcp_stateless: bool = True

    #: Master switch for OAuth. Defaults to on so a missing setting fails
    #: closed; turn it off only for local work without an authorization server.
    mcp_auth_enabled: bool = True
    mcp_rate_limite: int = 120
    mcp_rate_ventana_segundos: float = 60.0

    # --- human-in-the-loop (M4) -------------------------------------------
    approval_signing_key: str = "dev-only-approval-key-change-me"
    approval_ttl_seconds: int = 300

    # --- OAuth 2.1 (M5) ----------------------------------------------------
    #: Bind address of the authorization server. Loopback by default, for the
    #: same reason as `backend_host`.
    oauth_host: str = "127.0.0.1"
    #: Public identity of the authorization server. Goes into `iss`, so it must
    #: match what clients discover. Never an internal hostname.
    oauth_issuer: str = "http://localhost:9000"
    oauth_audience: str = "http://localhost:8080"
    oauth_access_token_ttl_seconds: int = 900
    #: Where the resource server fetches JWKS. Defaults to the issuer, and is
    #: overridden in Docker where the issuer only resolves from the host.
    oauth_jwks_url: str = ""

    @property
    def jwks_url(self) -> str:
        return self.oauth_jwks_url or f"{self.oauth_issuer.rstrip('/')}/jwks.json"

    @field_validator("mcp_allowed_origins", "mcp_allowed_hosts", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        """Accept both a JSON list and the more ergonomic `a,b,c` env form."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def _expandir_hosts_con_puerto(self) -> Settings:
        """Add the `host:port` form of every bare host in the allow-list.

        A browser sends `Host: localhost:8080`, not `localhost`, so a list of
        bare names matches nothing and the rebinding guard rejects everything,
        legitimate requests included. Expanding here keeps the config readable
        without a wildcard, which would disable the guard entirely.
        """
        expandidos: list[str] = []
        for host in self.mcp_allowed_hosts:
            expandidos.append(host)
            if ":" not in host:
                expandidos.append(f"{host}:{self.mcp_port}")
        # dict.fromkeys preserves order while removing duplicates.
        object.__setattr__(self, "mcp_allowed_hosts", list(dict.fromkeys(expandidos)))
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
