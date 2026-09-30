#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if [[ -f webapp.pid ]] && kill -0 "$(cat webapp.pid)" 2>/dev/null; then
    echo "WEB-APP is already running with PID $(cat webapp.pid)."
    exit 0
fi

nohup "$ROOT/run.sh" > "$ROOT/webapp.log" 2>&1 &
echo $! > "$ROOT/webapp.pid"
echo "WEB-APP started with PID $!. Log: $ROOT/webapp.log"
