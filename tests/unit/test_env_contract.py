"""Every documented setting must actually reach the process that reads it.

Three times now a variable has been declared and inert, and each one failed
silently because pydantic falls back to a default rather than complaining:

* `APPROVAL_SIGNING_KEY`, set by compose long after `Settings` renamed the field
  to `REQUEST_STATE_KEYS`. It configured nothing, and the key ring sealing
  paused operations stayed on its published development default.
* `REQUEST_STATE_KEYS` itself, which replaced it and which compose then never
  passed, so there was no way to override that default at all.
* `OAUTH_ACCESS_TOKEN_TTL_SECONDS`, documented in `.env.example` and never
  passed through compose, so shortening a token's life did nothing to a
  container and expiry could not be observed without editing files by hand.

Writing a fourth test for a fourth instance would be missing the point. These
assertions describe the contract between the three places a variable lives, and
fail on any variable that breaks it:

1. `.env.example` documents it.
2. Some code reads it: a `Settings` field, an `os.getenv`, or the entrypoint.
3. `docker-compose.yml` hands it to the services that need it.

An exemption is allowed and must carry a reason in `EXEMPT`, so "this one is
different" is a sentence someone wrote on purpose rather than a silence.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest
import yaml

from backend.config import Settings

ROOT = pathlib.Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.yml"
ENV_EXAMPLE = ROOT / ".env.example"
ENTRYPOINT = ROOT / "docker-entrypoint.sh"

#: Services built from this repository's image. The database and Keycloak run
#: upstream images and take their own configuration, so their variables say
#: nothing about our contract.
OUR_SERVICES = frozenset({"backend", "oauth", "mcp", "mcp-keycloak"})

#: Documented in `.env.example`, deliberately not handed to a container. Each
#: entry is a claim someone has to defend at review time.
EXEMPT: dict[str, str] = {
    "POSTGRES_USER": "consumed by the postgres image and by compose, which builds "
    "DATABASE_URL from it; no code of ours reads it",
    "POSTGRES_PASSWORD": "same as POSTGRES_USER: the database image's own configuration",
    "POSTGRES_DB": "same as POSTGRES_USER: the database image's own configuration",
    "POSTGRES_HOST": "host-side only: compose reaches the database by service name",
    "POSTGRES_PORT": "the published port in the compose mapping, not a value inside",
    "BACKEND_PORT": "the published port; the container always listens on 8000",
    "DATABASE_URL": "compose composes its own from POSTGRES_*; this one is for "
    "alembic and pytest running on the host",
    "APP_ROLE": "read by docker-entrypoint.sh, which compose bypasses by naming "
    "a command per service; it is for a platform that runs one command",
    "OAUTH_PRIVATE_KEY_PEM": "passed to the oauth service, and only that one: no "
    "other process has any business holding a signing key",
}


def env_example_keys() -> list[str]:
    pattern = re.compile(r"^([A-Z][A-Z0-9_]*)=")
    return [
        match.group(1)
        for line in ENV_EXAMPLE.read_text().splitlines()
        if (match := pattern.match(line))
    ]


def compose_environments() -> dict[str, set[str]]:
    """Every variable compose hands to each service, anchors resolved."""
    document = yaml.safe_load(COMPOSE.read_text())
    return {
        name: set((service.get("environment") or {}).keys())
        for name, service in document["services"].items()
    }


def direct_env_reads() -> set[str]:
    """Variables Python reads straight from the environment.

    `os.getenv(KEY_ENV_VAR)` names a module constant rather than a literal, so
    module-level string assignments are resolved before looking.
    """
    found: set[str] = set()
    for path in [*(ROOT / "backend").rglob("*.py"), *(ROOT / "mcp_server").rglob("*.py")]:
        if "__pycache__" in str(path):
            continue
        tree = ast.parse(path.read_text())
        constants = {
            target.id: node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
            for target in node.targets
            if isinstance(target, ast.Name) and isinstance(node.value.value, str)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            call = ast.unparse(node.func)
            if call not in {"os.getenv", "os.environ.get"}:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.add(first.value)
            elif isinstance(first, ast.Name) and first.id in constants:
                found.add(constants[first.id])
    return found


def entrypoint_reads() -> set[str]:
    return set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", ENTRYPOINT.read_text()))


SETTINGS_FIELDS = {name.upper() for name in Settings.model_fields}
CONSUMERS = SETTINGS_FIELDS | direct_env_reads() | entrypoint_reads()


class TestEveryDocumentedVariableIsRead:
    def test_something_in_the_code_reads_it(self) -> None:
        """A key nobody reads is documentation for a feature that does not
        exist, which is worse than no documentation."""
        orphans = [k for k in env_example_keys() if k not in CONSUMERS and k not in EXEMPT]
        assert not orphans, (
            "declared in .env.example and read by nothing: "
            + ", ".join(orphans)
            + ". Either wire it up, delete it, or add it to EXEMPT with a reason."
        )

    def test_no_key_is_declared_twice(self) -> None:
        keys = env_example_keys()
        duplicates = {k for k in keys if keys.count(k) > 1}
        assert not duplicates, f"declared more than once: {sorted(duplicates)}"


class TestEveryDocumentedVariableReachesAContainer:
    def test_compose_hands_it_to_at_least_one_service(self) -> None:
        """The `OAUTH_ACCESS_TOKEN_TTL_SECONDS` shape of bug.

        A setting `Settings` knows about, documented for the reader, and never
        passed to any container: editing `.env` changed nothing that runs, and
        nothing said so.
        """
        passed = set().union(*compose_environments().values())
        inert = [
            k
            for k in env_example_keys()
            if k in SETTINGS_FIELDS and k not in passed and k not in EXEMPT
        ]
        assert not inert, (
            "documented but never reaches a container: "
            + ", ".join(inert)
            + ". Add it to the right service in docker-compose.yml, or to EXEMPT "
            "with the reason it does not apply."
        )


class TestComposeSetsNothingNobodyReads:
    def test_every_variable_compose_passes_is_read(self) -> None:
        """The `APPROVAL_SIGNING_KEY` shape of bug.

        Compose kept setting a name the code had renamed away from. It looked
        configured, it configured nothing, and the value it was supposed to
        override stayed at a default published in this repository.
        """
        offenders: list[str] = []
        for service, variables in compose_environments().items():
            if service not in OUR_SERVICES:
                continue
            offenders += [f"{service}:{v}" for v in sorted(variables) if v not in CONSUMERS]
        assert not offenders, (
            "compose sets variables no code reads: "
            + ", ".join(offenders)
            + ". A renamed setting leaves exactly this trace."
        )

    def test_every_variable_compose_passes_is_documented(self) -> None:
        """Whatever a deployment must set has to be findable in one place."""
        documented = set(env_example_keys())
        missing: list[str] = []
        for service, variables in compose_environments().items():
            if service not in OUR_SERVICES:
                continue
            missing += [v for v in sorted(variables) if v not in documented]
        assert not missing, "passed by compose and absent from .env.example: " + ", ".join(
            sorted(set(missing))
        )


class TestTheExemptionsAreHonest:
    @pytest.mark.parametrize("key", sorted(EXEMPT))
    def test_an_exemption_names_a_key_that_exists(self, key: str) -> None:
        """An exemption for a key nobody declares is a stale excuse, and it
        would quietly cover a future variable of the same name."""
        assert key in env_example_keys(), f"{key} is exempt but no longer declared"

    @pytest.mark.parametrize("key", sorted(EXEMPT))
    def test_an_exemption_gives_a_reason(self, key: str) -> None:
        assert len(EXEMPT[key]) > 20, f"{key} needs a reason, not a placeholder"
