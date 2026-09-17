#!/bin/bash
set -e

cd "$(dirname "$0")/.."

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

source .venv/bin/activate

pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
pip install --quiet -r requirements-dev.txt

if [ "${INSTALL_VOICE:-0}" = "1" ]; then
  pip install --quiet -r requirements-voice.txt
fi

if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "Created .env from .env.example - add your API keys before starting."
fi

echo "Backend environment ready."
