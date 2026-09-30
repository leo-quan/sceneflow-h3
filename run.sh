#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if [[ ! -x .venv/bin/python ]]; then
    echo "Missing WEB-APP/.venv. Run setup first." >&2
    exit 1
fi

exec "$ROOT/.venv/bin/uvicorn" app:app --host 0.0.0.0 --port 9018
