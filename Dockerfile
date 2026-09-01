# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Dependency layer first, so source edits do not invalidate the install cache.
# There is deliberately no `--mount=type=cache` here. It saved a few seconds on
# a cold build and cost portability: BuildKit defaults a cache mount's id to its
# target path, but some hosted builders (Railway among them) require the id
# spelled out *and* namespaced with their own per-service key, which would tie
# this file to one deployment. Layer ordering is where the real caching is: a
# source edit leaves this layer untouched, so a rebuild is ~12s against ~25s
# cold, and only a change to uv.lock pays the full price.
COPY pyproject.toml uv.lock* README.md ./
RUN uv sync --frozen --no-install-project --no-dev 2>/dev/null || uv sync --no-install-project --no-dev

COPY alembic.ini docker-entrypoint.sh ./
COPY backend/ ./backend/
COPY mcp_server/ ./mcp_server/

RUN uv sync --no-dev

# Never run as root: the container has no business writing outside /app.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000 8080

# One image, three processes: the domain API, the authorization server and the
# MCP server. Compose names the one it wants per service and never reaches this;
# anywhere else, APP_ROLE picks, defaulting to the MCP server, which is the
# product.
CMD ["/app/docker-entrypoint.sh"]
