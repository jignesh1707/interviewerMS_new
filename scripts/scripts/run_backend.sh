#!/bin/bash
set -e

cd "$(dirname "$0")/../backend"

if [ -d ".venv" ]; then
  source .venv/bin/activate
fi

exec uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8080}" --reload
