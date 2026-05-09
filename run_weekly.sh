#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="$SCRIPT_DIR/.venv/bin/python"
CONFIG_PATH="$SCRIPT_DIR/config/weekly_strategy.yaml"
DB_PATH="$SCRIPT_DIR/stocks.db"

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

if [[ ! -f "$CONFIG_PATH" ]]; then
  echo "Error: config not found: $CONFIG_PATH"
  exit 1
fi

if [[ ! -f "$DB_PATH" ]]; then
  echo "Warning: DB not found yet: $DB_PATH"
  echo "The xuangu step should create it if the download/import succeeds."
fi

echo "Weekly stock workflow"
echo "Project: $SCRIPT_DIR"
echo

if [[ "${SKIP_REVIEW:-0}" != "1" ]]; then
  echo "Step 1/5: Review previous selected stocks..."
  if "$VENV_PY" "$SCRIPT_DIR/weekly_stock_main.py" --config "$CONFIG_PATH" review; then
    echo "Review completed."
  else
    echo "Review skipped or failed. This is OK if there is no previous run ready to review."
  fi
  echo
else
  echo "Step 1/5: Review skipped by SKIP_REVIEW=1"
  echo
fi

if [[ "${SKIP_XUANGU:-0}" != "1" ]]; then
  echo "Step 2/5: Download/import xuangu results and update 1-year K-line data..."
  "$SCRIPT_DIR/run_xuangu.sh" "$@"
  echo
else
  echo "Step 2/5: Xuangu download/import skipped by SKIP_XUANGU=1"
  echo
fi

SCREEN_DATE="${SCREEN_DATE:-$(date +%F)}"
SCREEN_ARGS=(screen --date "$SCREEN_DATE" --replace-existing)
if [[ -n "${XUANGU_BATCH_ID:-}" ]]; then
  SCREEN_ARGS+=(--xuangu-batch-id "$XUANGU_BATCH_ID")
fi

echo "Step 3/5: Generate rule-based Top stocks..."
"$VENV_PY" "$SCRIPT_DIR/weekly_stock_main.py" --config "$CONFIG_PATH" "${SCREEN_ARGS[@]}"
echo

if [[ "${SKIP_BACKTEST:-0}" != "1" ]]; then
  echo "Step 4/5: Backtest ML models..."
  "$VENV_PY" "$SCRIPT_DIR/weekly_stock_main.py" --config "$CONFIG_PATH" backtest
  echo
else
  echo "Step 4/5: ML backtest skipped by SKIP_BACKTEST=1"
  echo
fi

echo "Step 5/5: Train ML models and predict Top stocks..."
"$VENV_PY" "$SCRIPT_DIR/weekly_stock_main.py" --config "$CONFIG_PATH" predict
echo

echo "Weekly workflow completed."
echo "Open:"
echo "  http://127.0.0.1:8000/screening"
echo "  http://127.0.0.1:8000/top"
