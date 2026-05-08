#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="$SCRIPT_DIR/.venv/bin/python"
SCREENING_FILE="$SCRIPT_DIR/screening.txt"

if [[ ! -x "$VENV_PY" ]]; then
  echo "Error: virtualenv python not found: $VENV_PY"
  echo "Run:"
  echo "  cd $SCRIPT_DIR"
  echo "  python3 -m venv .venv"
  echo "  source .venv/bin/activate"
  echo "  pip install -r requirements.txt"
  echo "  playwright install chromium firefox"
  exit 1
fi

if [[ ! -f "$SCREENING_FILE" ]]; then
  echo "Error: screening condition file not found: $SCREENING_FILE"
  echo "Create $SCREENING_FILE first, for example:"
  echo "  总市值<200亿;净利润同比增长率>20%"
  exit 1
fi

if ! "$VENV_PY" -c "import playwright, openpyxl" >/dev/null 2>&1; then
  echo "Error: missing python dependencies in .venv"
  echo "Run:"
  echo "  source $SCRIPT_DIR/.venv/bin/activate"
  echo "  pip install -r $SCRIPT_DIR/requirements.txt"
  echo "  playwright install chromium firefox"
  exit 1
fi

echo "Using condition file: $SCREENING_FILE"
echo "Starting xuangu automation..."

ARGS=(
  "$SCRIPT_DIR/xuangu_to_sqlite.py"
  --url "https://xuangu.eastmoney.com/"
  --condition-file "$SCREENING_FILE"
  --db "$SCRIPT_DIR/stocks.db"
  --download-dir "$SCRIPT_DIR/downloads"
  --browser-engine firefox
  --manual-download
)

if [[ "${WAIT_LOGIN:-0}" == "1" ]]; then
  ARGS+=(--browser-headed --wait-login)
elif [[ "${BROWSER_HEADED:-0}" == "1" ]]; then
  ARGS+=(--browser-headed)
fi

exec "$VENV_PY" "${ARGS[@]}" "$@"
