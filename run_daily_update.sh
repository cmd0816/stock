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
  exit 1
fi

if [[ ! -f "$DB_PATH" ]]; then
  echo "Error: DB not found: $DB_PATH"
  echo "Run ./run_weekly.sh or ./run_xuangu.sh first."
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
DAILY_PERSIST_TOP="${DAILY_PERSIST_TOP:-0}"
DAILY_REVIEW_PREVIOUS="${DAILY_REVIEW_PREVIOUS:-0}"

if [[ -z "$BATCH_ID" ]]; then
  echo "Error: no xuangu batch found in $DB_PATH"
  echo "Run ./run_xuangu.sh or import a xuangu XLSX first."
  exit 1
fi

if [[ "$DAILY_PERSIST_TOP" == "1" ]]; then
  echo "Daily update: download K-line -> optional review previous -> persist Top N -> ML predict"
else
  echo "Daily update: download K-line -> preview Top N without writing weekly Top tables"
fi
echo "Project: $SCRIPT_DIR"
echo "Target date: $TARGET_DATE"
echo "China trading aligned date: $ALIGNED_DATE"
echo "Xuangu batch: $BATCH_ID"
echo "Persist Top N: $DAILY_PERSIST_TOP"
echo "Review previous: $DAILY_REVIEW_PREVIOUS"
echo

LOG_DIR="$SCRIPT_DIR/downloads/logs"
mkdir -p "$LOG_DIR"
RUN_LOG="$LOG_DIR/daily_update_${BATCH_ID}_${ALIGNED_DATE}_$(date +%H%M%S).log"
touch "$RUN_LOG"

RUN_STATUS=0

{
  echo
  echo "Step 1/4: Download latest batch daily K-line data (BaoStock first, AKShare fallback)..."
} | tee -a "$RUN_LOG"

HISTORY_ARGS=(
  "$SCRIPT_DIR/download_batch_history.py"
  --db "$DB_PATH"
  --batch-id "$BATCH_ID"
  --end-date "$ALIGNED_DATE"
  --delay "${HISTORY_DELAY:-1.5}"
)

if [[ -n "${HISTORY_LIMIT:-}" ]]; then
  HISTORY_ARGS+=(--limit "$HISTORY_LIMIT")
fi

if [[ -n "${HISTORY_MIN_EXISTING_DAYS:-}" ]]; then
  HISTORY_ARGS+=(--min-existing-days "$HISTORY_MIN_EXISTING_DAYS")
fi

set +e
"$VENV_PY" -u "${HISTORY_ARGS[@]}" 2>&1 | tee -a "$RUN_LOG"
HISTORY_STATUS=${PIPESTATUS[0]}
set -e
if [[ "$HISTORY_STATUS" -ne 0 ]]; then
  RUN_STATUS=$HISTORY_STATUS
fi

PREV_RUN_INFO="$("$VENV_PY" - "$DB_PATH" <<'PY'
import sys
from pathlib import Path
from weekly_stock import db

db_path = Path(sys.argv[1])
with db.connect(db_path) as conn:
    row = conn.execute(
        """
        SELECT r.run_id, COALESCE(r.xuangu_batch_id, '') AS batch_id
        FROM weekly_screen_runs r
        WHERE EXISTS (SELECT 1 FROM weekly_selected_stocks s WHERE s.run_id = r.run_id)
        ORDER BY r.screen_date DESC, r.run_id DESC
        LIMIT 1
        """
    ).fetchone()
if not row:
    print("|")
else:
    print(f"{int(row['run_id'])}|{str(row['batch_id'] or '')}")
PY
)"
PREV_RUN_ID="${PREV_RUN_INFO%%|*}"
PREV_RUN_BATCH="${PREV_RUN_INFO#*|}"

if [[ "$DAILY_REVIEW_PREVIOUS" == "1" && -n "$PREV_RUN_ID" && "$PREV_RUN_BATCH" != "$BATCH_ID" ]]; then
  {
    echo
    echo "Step 2/4: New xuangu batch detected, review previous selected run..."
    echo "Previous run_id=$PREV_RUN_ID batch=$PREV_RUN_BATCH -> current batch=$BATCH_ID"
  } | tee -a "$RUN_LOG"

  REVIEW_EXISTS="$("$VENV_PY" - "$DB_PATH" "$PREV_RUN_ID" "$ALIGNED_DATE" <<'PY'
import sys
from pathlib import Path
from weekly_stock import db

db_path = Path(sys.argv[1])
run_id = int(sys.argv[2])
review_date = sys.argv[3]
with db.connect(db_path) as conn:
    row = conn.execute(
        "SELECT 1 FROM weekly_review_runs WHERE reviewed_run_id = ? AND review_date = ? LIMIT 1",
        (run_id, review_date),
    ).fetchone()
print("1" if row else "")
PY
)"

  if [[ -n "$REVIEW_EXISTS" ]]; then
    echo "Skip review: run_id=$PREV_RUN_ID already reviewed on $ALIGNED_DATE." | tee -a "$RUN_LOG"
  else
    set +e
    "$VENV_PY" -u "$SCRIPT_DIR/weekly_stock_main.py" --config "$CONFIG_PATH" review --run-id "$PREV_RUN_ID" --date "$ALIGNED_DATE" 2>&1 | tee -a "$RUN_LOG"
    REVIEW_STATUS=${PIPESTATUS[0]}
    set -e
    if [[ "$REVIEW_STATUS" -ne 0 ]]; then
      RUN_STATUS=$REVIEW_STATUS
    fi
  fi
elif [[ "$DAILY_REVIEW_PREVIOUS" == "1" ]]; then
  {
    echo
    echo "Step 2/4: No new xuangu batch switch detected, skip previous-run review."
  } | tee -a "$RUN_LOG"
else
  {
    echo
    echo "Step 2/4: Previous-run review disabled for daily update."
    echo "Set DAILY_REVIEW_PREVIOUS=1 to restore the old daily auto-review behavior."
  } | tee -a "$RUN_LOG"
fi

RUN_ID=""
if [[ "$DAILY_PERSIST_TOP" == "1" ]]; then
  {
    echo
    echo "Step 3/4: Screen latest xuangu batch and persist Top N..."
  } | tee -a "$RUN_LOG"

  set +e
  SCREEN_OUTPUT=$("$VENV_PY" -u "$SCRIPT_DIR/weekly_stock_main.py" --config "$CONFIG_PATH" screen --date "$ALIGNED_DATE" --replace-existing --xuangu-batch-id "$BATCH_ID" 2>&1)
  SCREEN_STATUS=$?
  set -e
  echo "$SCREEN_OUTPUT" | tee -a "$RUN_LOG"
  if [[ "$SCREEN_STATUS" -ne 0 ]]; then
    RUN_STATUS=$SCREEN_STATUS
  fi

  RUN_ID="$(printf '%s\n' "$SCREEN_OUTPUT" | sed -n 's/.*run_id=\([0-9][0-9]*\).*/\1/p' | tail -n 1)"
  if [[ -z "$RUN_ID" ]]; then
    RUN_ID="$("$VENV_PY" - "$DB_PATH" "$ALIGNED_DATE" "$BATCH_ID" <<'PY'
import sys
from pathlib import Path
from weekly_stock import db
db_path = Path(sys.argv[1])
screen_date = sys.argv[2]
batch_id = sys.argv[3]
with db.connect(db_path) as conn:
    row = conn.execute(
        """
        SELECT run_id
        FROM weekly_screen_runs
        WHERE screen_date = ?
          AND COALESCE(xuangu_batch_id, '') = COALESCE(?, '')
        ORDER BY run_id DESC
        LIMIT 1
        """,
        (screen_date, batch_id),
    ).fetchone()
print(int(row["run_id"]) if row else "")
PY
    )"
  fi
else
  {
    echo
    echo "Step 3/4: Preview latest xuangu batch Top N (no weekly Top DB write)..."
  } | tee -a "$RUN_LOG"

  set +e
  "$VENV_PY" -u "$SCRIPT_DIR/weekly_stock_main.py" --config "$CONFIG_PATH" preview --date "$ALIGNED_DATE" --xuangu-batch-id "$BATCH_ID" 2>&1 | tee -a "$RUN_LOG"
  PREVIEW_STATUS=${PIPESTATUS[0]}
  set -e
  if [[ "$PREVIEW_STATUS" -ne 0 ]]; then
    RUN_STATUS=$PREVIEW_STATUS
  fi
fi

if [[ "$DAILY_PERSIST_TOP" == "1" && -n "$RUN_ID" ]]; then
  echo "Resolved latest run_id: $RUN_ID" | tee -a "$RUN_LOG"
  {
    echo
    echo "Step 4/4: Train ML model and predict using latest selected stocks..."
  } | tee -a "$RUN_LOG"

  set +e
  "$VENV_PY" -u "$SCRIPT_DIR/weekly_stock_main.py" --config "$CONFIG_PATH" predict --run-id "$RUN_ID" 2>&1 | tee -a "$RUN_LOG"
  PREDICT_STATUS=${PIPESTATUS[0]}
  set -e
  if [[ "$PREDICT_STATUS" -ne 0 ]]; then
    RUN_STATUS=$PREDICT_STATUS
  fi
elif [[ "$DAILY_PERSIST_TOP" == "1" ]]; then
  echo "Warning: failed to resolve run_id after screen; skip ML predict." | tee -a "$RUN_LOG"
  RUN_STATUS=1
else
  {
    echo
    echo "Step 4/4: Skip ML prediction persistence because DAILY_PERSIST_TOP=0."
  } | tee -a "$RUN_LOG"
fi

if [[ -n "${DAILY_EMAIL_TO:-}" ]]; then
  DAILY_UPDATE_DB_PATH="$DB_PATH" \
  DAILY_UPDATE_BATCH_ID="$BATCH_ID" \
  DAILY_UPDATE_TARGET_DATE="$TARGET_DATE" \
  DAILY_UPDATE_ALIGNED_DATE="$ALIGNED_DATE" \
  DAILY_UPDATE_STATUS="$RUN_STATUS" \
  DAILY_UPDATE_LOG_FILE="$RUN_LOG" \
  DAILY_UPDATE_PERSIST_TOP="$DAILY_PERSIST_TOP" \
  "$VENV_PY" "$SCRIPT_DIR/daily_update_email.py" \
    || echo "Warning: failed to send daily update email."
fi

exit "$RUN_STATUS"
