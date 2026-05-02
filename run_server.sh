#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="$SCRIPT_DIR/.venv/bin/python"
DB_PATH="$SCRIPT_DIR/stocks.db"

if [[ -x "$VENV_PY" ]]; then
  PY="$VENV_PY"
else
  PY="python3"
fi

if [[ ! -f "$DB_PATH" ]]; then
  echo "Warning: DB not found at $DB_PATH"
  echo "Server will still start, but pages may have no data."
fi

echo "Starting dashboard at http://127.0.0.1:8000/daily"
exec "$PY" "$SCRIPT_DIR/view_quotes.py" \
  --db "$DB_PATH" \
  --host "127.0.0.1" \
  --port 8000 \
  --limit 400 \
  "$@"
