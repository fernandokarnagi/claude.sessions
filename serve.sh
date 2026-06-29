#!/usr/bin/env bash
# serve.sh — run the Claude Sessions dashboard inside the local venv.
# Creates the venv and installs deps on first run.
#   ./serve.sh                 # serve on http://127.0.0.1:8765
#   PORT=9000 ./serve.sh       # override port
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$DIR/.venv"
PORT="${PORT:-8765}"

# Load Slack credentials if present (gitignored). Without these the Slack bot
# stays disabled and the dashboard runs web-only.
if [ -f "$DIR/.env.slack" ]; then
    # shellcheck disable=SC1091
    source "$DIR/.env.slack"
fi
export PYTHONUNBUFFERED=1

if [ ! -x "$VENV/bin/python" ]; then
    echo "Creating venv at $VENV ..."
    python3 -m venv "$VENV"
fi

# Install deps if fastapi is missing (cheap check; skips on subsequent runs).
if ! "$VENV/bin/python" -c "import fastapi, uvicorn" 2>/dev/null; then
    echo "Installing dependencies ..."
    "$VENV/bin/pip" install -q -r "$DIR/requirements.txt"
fi

echo "Dashboard → http://127.0.0.1:$PORT"
cd "$DIR"
exec "$VENV/bin/python" -m uvicorn server.app:app --host 127.0.0.1 --port "$PORT" "$@"
