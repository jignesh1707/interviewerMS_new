#!/bin/bash
set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "Starting backend on port ${PORT:-8080}..."
cd "$ROOT/backend"
if [ -d ".venv" ]; then
  source .venv/bin/activate
fi
uvicorn app.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8080}" &
BACKEND_PID=$!

cleanup() {
  kill "$BACKEND_PID" 2>/dev/null || true
}
trap cleanup EXIT

echo "Starting frontend on port 5173..."
cd "$ROOT/frontend"
npm run dev -- --host 0.0.0.0 --port 5173
