# Every target is runnable from a clean checkout.
.DEFAULT_GOAL := help
SHELL := /bin/bash

TEST_DB ?= postgresql+psycopg://clinic:clinic_dev_only@localhost:5433/clinic_test

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Create the virtualenv and install every dependency
	uv sync --all-groups

up: ## Start the whole stack: postgres + backend + oauth + mcp
	# --build so a code change is never served from a stale image. Layers are
	# cached, so it costs a couple of seconds when nothing changed.
	docker compose up -d --build --wait
	@echo "  backend  http://localhost:8000/docs"
	@echo "  oauth    http://localhost:9000/.well-known/oauth-authorization-server"
	@echo "  mcp      http://localhost:8080/mcp"

down: ## Stop the stack
	docker compose down

reset: ## Drop the database volume and rebuild from scratch
	docker compose down -v && $(MAKE) up

logs: ## Follow the logs of every service
	docker compose logs -f

keycloak: ## Start Keycloak plus a second MCP server that trusts it
	docker compose --profile keycloak up -d --build --wait
	@echo "  keycloak     http://localhost:9100 (admin/admin, realm 'clinic')"
	@echo "  mcp-keycloak http://localhost:8081/mcp"

keycloak-verify: ## Prove the auth layer is pluggable, against the running Keycloak
	uv run python scripts/verify_keycloak.py

migrate: ## Apply database migrations
	uv run alembic upgrade head

seed: ## Load deterministic synthetic data (Faker, fixed seed)
	uv run python -m backend.seed

token: ## Print an access token obtained through the real PKCE flow
	@uv run python scripts/get_token.py --scope "read write clinical"

smoke: ## End-to-end check against the running stack
	uv run python scripts/smoke.py

diagrams: ## Re-render every mermaid block to a committed image
	uv run python scripts/render_diagrams.py

race: ## Ten agents fight for one slot, over the whole stack
	@uv run python scripts/race.py

probe: ## Block E: the five assumptions the manual checks do not cover
	uv run python scripts/probe.py

consola: ## Interactive client: you answer the confirmations yourself
	uv run python scripts/console.py

inspector: ## Open the MCP Inspector (read tools only, see docs/inspector.md)
	@TOKEN=$$(uv run python scripts/get_token.py --scope "read write clinical"); \
		npx -y @modelcontextprotocol/inspector \
			--transport http \
			--server-url http://localhost:8080/mcp \
			--header "Authorization: Bearer $$TOKEN"

inspector-cli: ## List the tools through the Inspector CLI (no browser)
	@TOKEN=$$(uv run python scripts/get_token.py --scope "read write clinical"); \
		npx -y @modelcontextprotocol/inspector --cli http://localhost:8080/mcp \
			--transport http --header "Authorization: Bearer $$TOKEN" --method tools/list

lint: ## Ruff + mypy strict
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy backend mcp_server scripts

fmt: ## Auto-format
	uv run ruff format .
	uv run ruff check --fix .

test: ## Full suite with the coverage gate (spins up its own database)
	uv run pytest --cov --cov-report=term-missing --cov-fail-under=95

test-fast: ## Same suite against the running compose stack (much quicker)
	TEST_DATABASE_URL=$(TEST_DB) uv run pytest --cov --cov-report=term-missing --cov-fail-under=95

test-unit: ## Fast tests only (no database, no docker)
	uv run pytest tests/unit

test-security: ## Only the security-control suite
	TEST_DATABASE_URL=$(TEST_DB) uv run pytest -m security

test-contract: ## Only the MCP protocol-surface suite
	TEST_DATABASE_URL=$(TEST_DB) uv run pytest tests/contract

audit: ## Dependency and static security audit
	uv run bandit -q -r backend mcp_server
	uv run pip-audit

check: lint test audit ## Everything CI runs

.PHONY: help install up down reset logs keycloak keycloak-verify migrate seed token smoke probe race diagrams consola inspector \
	inspector-cli lint fmt test test-fast test-unit test-security test-contract audit check
