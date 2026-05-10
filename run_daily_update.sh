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

if [[ ! -f "$DB_PATH" ]]; then
  echo "Error: DB not found: $DB_PATH"
  echo "Run ./run_weekly.sh or ./run_xuangu.sh first."
  exit 1
fi

if ! "$VENV_PY" -c "import playwright" >/dev/null 2>&1; then
  echo "Error: missing playwright in .venv"
  echo "Run:"
  echo "  source $SCRIPT_DIR/.venv/bin/activate"
  echo "  pip install -r $SCRIPT_DIR/requirements.txt"
  echo "  playwright install firefox"
  exit 1
fi

TARGET_DATE="${UPDATE_DATE:-$(date +%F)}"
ALIGNED_DATE="$("$VENV_PY" - "$CONFIG_PATH" "$DB_PATH" "$TARGET_DATE" <<'PY'
import sys
from pathlib import Path
from weekly_stock.config import load_config
from weekly_stock import db
from weekly_stock.trading_calendar import align_to_last_trading_day

config_path = Path(sys.argv[1])
config = load_config(config_path) if config_path.exists() else {}
calendar_cfg = config.get("calendar", {})
with db.connect(Path(sys.argv[2])) as conn:
    print(
        align_to_last_trading_day(
            sys.argv[3],
            conn=conn,
            prefer_akshare=bool(calendar_cfg.get("prefer_akshare", True)),
        )
    )
PY
)"

LATEST_BATCH_ID="$("$VENV_PY" - "$DB_PATH" <<'PY'
import sqlite3
import sys
from pathlib import Path

path = Path(sys.argv[1])
with sqlite3.connect(path) as conn:
    row = conn.execute(
        "SELECT batch_id FROM xuangu_batches ORDER BY imported_at_utc DESC LIMIT 1"
    ).fetchone()
print(row[0] if row else "")
PY
)"
BATCH_ID="${XUANGU_BATCH_ID:-$LATEST_BATCH_ID}"

if [[ -z "$BATCH_ID" ]]; then
  echo "Error: no xuangu batch found in $DB_PATH"
  echo "Run ./run_weekly.sh or import a xuangu XLSX first."
  exit 1
fi

echo "Daily K-line update"
echo "Project: $SCRIPT_DIR"
echo "Target date: $TARGET_DATE"
echo "China trading aligned date: $ALIGNED_DATE"
echo "Xuangu batch: $BATCH_ID"
echo

ARGS=(
  "$SCRIPT_DIR/xuangu_to_sqlite.py"
  --history-only
  --url "https://xuangu.eastmoney.com/"
  --db "$DB_PATH"
  --download-dir "$SCRIPT_DIR/downloads"
  --batch-id "$BATCH_ID"
  --history-end-date "$ALIGNED_DATE"
  --history-delay "${HISTORY_DELAY:-1.5}"
  --browser-engine firefox
)

if [[ -n "${HISTORY_LIMIT:-}" ]]; then
  ARGS+=(--history-limit "$HISTORY_LIMIT")
fi

if [[ -n "${HISTORY_MIN_EXISTING_DAYS:-}" ]]; then
  ARGS+=(--history-min-existing-days "$HISTORY_MIN_EXISTING_DAYS")
fi

if [[ "${WAIT_LOGIN:-0}" == "1" ]]; then
  ARGS+=(--browser-headed --wait-login)
elif [[ "${BROWSER_HEADED:-0}" == "1" ]]; then
  ARGS+=(--browser-headed)
fi

LOG_DIR="$SCRIPT_DIR/downloads/logs"
mkdir -p "$LOG_DIR"
RUN_LOG="$LOG_DIR/daily_update_${BATCH_ID}_${ALIGNED_DATE}_$(date +%H%M%S).log"

set +e
"$VENV_PY" "${ARGS[@]}" "$@" 2>&1 | tee "$RUN_LOG"
RUN_STATUS=${PIPESTATUS[0]}
set -e

if [[ -n "${DAILY_EMAIL_TO:-}" ]]; then
  DAILY_UPDATE_DB_PATH="$DB_PATH" \
  DAILY_UPDATE_BATCH_ID="$BATCH_ID" \
  DAILY_UPDATE_TARGET_DATE="$TARGET_DATE" \
  DAILY_UPDATE_ALIGNED_DATE="$ALIGNED_DATE" \
  DAILY_UPDATE_STATUS="$RUN_STATUS" \
  DAILY_UPDATE_LOG_FILE="$RUN_LOG" \
  "$VENV_PY" "$SCRIPT_DIR/daily_update_email.py" \
    || echo "Warning: failed to send daily update email."
fi

exit "$RUN_STATUS"
