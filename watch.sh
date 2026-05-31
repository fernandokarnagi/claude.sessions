#!/usr/bin/env bash
# watch.sh — run watch_session.py inside the local venv.
# Creates the venv on first run. All args are passed through.
#   ./watch.sh            # follow most recent session
#   ./watch.sh --list     # list recent sessions
#   ./watch.sh --all      # replay history then follow
#   ./watch.sh --session 9fb37a4a
set -euo pipefail

# Resolve this script's directory so it works from anywhere.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$DIR/.venv"

# Create venv if missing (script uses stdlib only, so no pip install needed).
if [ ! -x "$VENV/bin/python" ]; then
    echo "Creating venv at $VENV ..."
    python3 -m venv "$VENV"
fi

exec "$VENV/bin/python" "$DIR/watch_session.py" "$@"
