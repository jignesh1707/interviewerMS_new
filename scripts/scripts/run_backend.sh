#!/bin/bash
set -e

cd "$(dirname "$0")/../backend"

if [ -d ".venv" ]; then
  source .venv/bin/activate
fi

# Auto-reload is for local development only; never enable it for a deployed service.
RELOAD_FLAG=""
if [ "${RELOAD:-0}" = "1" ]; then
  RELOAD_FLAG="--reload"
fi
exec uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8080}" $RELOAD_FLAG
