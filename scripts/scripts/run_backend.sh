#!/bin/bash
set -e

cd "$(dirname "$0")/../backend"

if [ -d ".venv" ]; then
  source .venv/bin/activate
fi

exec uvicorn app.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8080}" --reload
