#!/usr/bin/env bash
# Entry point viam-server launches for this local module.
# Creates a virtualenv next to this script on first run, then starts the module.
set -euo pipefail
cd "$(dirname "$0")"

VENV=".venv"
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
fi
# Cheap no-op once everything is installed.
"$VENV/bin/pip" install --quiet -r requirements.txt

exec "$VENV/bin/python" -m src.main "$@"
