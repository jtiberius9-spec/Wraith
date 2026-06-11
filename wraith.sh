#!/usr/bin/env bash
# Wraith launcher. Creates a venv on first run, then forwards all args.
set -euo pipefail
cd "$(dirname "$0")"

VENV=".venv"
if [ ! -d "$VENV" ]; then
  echo "🖤 first run — setting up venv…"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q -r requirements.txt
fi

exec "$VENV/bin/python" -m wraith "$@"
