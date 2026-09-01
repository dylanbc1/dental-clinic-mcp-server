#!/bin/sh
# One image, three processes. The role comes from the environment because a
# platform that runs one command per service (Railway, Fly, Cloud Run) needs a
# way to pick, and per-provider config files are a mechanism this repository
# would then have to track. Compose sets `command:` explicitly and never reaches
# this script.
set -e

case "${APP_ROLE:-mcp}" in
  backend)
    # Migrations before the API accepts anything, and a seed only into an empty
    # database: re-running this on every deploy must not duplicate data.
    alembic upgrade head
    python -m backend.seed --if-empty
    exec uvicorn backend.api:app \
      --host "${BACKEND_HOST:-::}" --port "${BACKEND_PORT:-8000}"
    ;;
  oauth)
    exec python -m mcp_server.oauth.server
    ;;
  mcp)
    exec python -m mcp_server.server
    ;;
  *)
    echo "APP_ROLE must be backend, oauth or mcp; got '${APP_ROLE}'" >&2
    exit 64
    ;;
esac
