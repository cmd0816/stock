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

TARGET_DATE="${SCREEN_DATE:-$(date +%F)}"
ALIGNED_SCREEN_DATE="$("$VENV_PY" - "$CONFIG_PATH" "$DB_PATH" "$TARGET_DATE" <<'PY'
import sys
from pathlib import Path
from weekly_stock.config import load_config
from weekly_stock import db
from weekly_stock.trading_calendar import align_to_last_trading_day

config = load_config(Path(sys.argv[1]))
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
echo "Target date: $TARGET_DATE"
echo "China trading aligned screen date: $ALIGNED_SCREEN_DATE"
echo

ALIGNED_BATCH_ID="${ALIGNED_SCREEN_DATE//-/}"
TODAY_DATE="$(date +%F)"

if [[ "${SKIP_REVIEW:-0}" != "1" ]]; then
  echo "Step 1/6: Review previous selected stocks..."
  if "$VENV_PY" - "$CONFIG_PATH" <<'PY'
from pathlib import Path
import sys

from weekly_stock.config import load_config
from weekly_stock.jobs import weekly_review_job

config_path = Path(sys.argv[1])
config = load_config(config_path)
try:
    review_id = weekly_review_job(config_path, config)
    print(f"weekly_review_job completed: review_id={review_id}")
except RuntimeError as exc:
    if str(exc) == "No selected weekly screen run needs review.":
        print("No previous weekly run pending review; skip Step 1.")
    else:
        raise
PY
  then
    echo "Review completed."
  else
    echo "Review skipped or failed. This is OK if there is no previous run ready to review."
  fi
  echo
else
  echo "Step 1/6: Review skipped by SKIP_REVIEW=1"
  echo
fi

if [[ "${SKIP_XUANGU:-0}" != "1" ]]; then
  echo "Step 2/6: Download/import xuangu results and update 1-year K-line data..."
  XUANGU_ARGS=(--batch-id "${XUANGU_BATCH_ID:-$ALIGNED_BATCH_ID}" --history-end-date "$ALIGNED_SCREEN_DATE")
  "$SCRIPT_DIR/run_xuangu.sh" "${XUANGU_ARGS[@]}" "$@"
  echo
else
  echo "Step 2/6: Xuangu download/import skipped by SKIP_XUANGU=1"
  echo
fi

SCREEN_ARGS=(screen --date "$ALIGNED_SCREEN_DATE" --replace-existing)
SCREEN_ARGS+=(--xuangu-batch-id "${XUANGU_BATCH_ID:-$ALIGNED_BATCH_ID}")

echo "Step 3/6: Generate rule-based Top stocks..."
SCREEN_OUTPUT="$("$VENV_PY" "$SCRIPT_DIR/weekly_stock_main.py" --config "$CONFIG_PATH" "${SCREEN_ARGS[@]}")"
echo "$SCREEN_OUTPUT"
RUN_ID="$(printf '%s\n' "$SCREEN_OUTPUT" | sed -n 's/.*run_id=\([0-9][0-9]*\).*/\1/p' | tail -n 1)"
if [[ -z "$RUN_ID" ]]; then
  RUN_ID="$("$VENV_PY" - "$DB_PATH" "$ALIGNED_SCREEN_DATE" "${XUANGU_BATCH_ID:-$ALIGNED_BATCH_ID}" <<'PY'
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
if [[ -z "$RUN_ID" ]]; then
  echo "Warning: failed to resolve run_id for this weekly screen run."
else
  echo "Resolved run_id: $RUN_ID"
fi
echo

if [[ "${SKIP_TOP_HISTORY:-0}" != "1" ]]; then
  echo "Step 4/6: Download/update 1-year K-line for this run's Top stocks (for review)..."
  TOP_HISTORY_TARGET_DATE="${TOP_HISTORY_TARGET_DATE:-$TODAY_DATE}"
  TOP_HISTORY_MIN_EXISTING_DAYS="${TOP_HISTORY_MIN_EXISTING_DAYS:-200}"
  if [[ -z "${RUN_ID:-}" ]]; then
    echo "Skip Top history download: run_id is empty."
  else
    "$VENV_PY" - "$SCRIPT_DIR" "$DB_PATH" "$RUN_ID" "$TOP_HISTORY_TARGET_DATE" "$TOP_HISTORY_MIN_EXISTING_DAYS" <<'PY'
import sys
from pathlib import Path

from eastmoney_to_sqlite import fetch_stock_history_1y, save_history_klines
from weekly_stock import db


def infer_market(code: str) -> int:
    text = str(code or "").strip()
    if text.startswith(("6", "9")):
        return 1
    return 0


def quote_url(code: str) -> str:
    text = str(code or "").strip()
    if text.startswith(("8", "4")):
        prefix = "bj"
    elif text.startswith(("6", "9")):
        prefix = "sh"
    else:
        prefix = "sz"
    return f"https://quote.eastmoney.com/concept/{prefix}{text}.html"


script_dir = Path(sys.argv[1])
db_path = Path(sys.argv[2])
run_id = int(sys.argv[3])
target_date = str(sys.argv[4]).strip()
min_existing_days = max(0, int(sys.argv[5]))

with db.connect(db_path) as conn:
    rows = conn.execute(
        """
        SELECT code, name, rank_no
        FROM weekly_selected_stocks
        WHERE run_id = ?
        ORDER BY rank_no
        """,
        (run_id,),
    ).fetchall()

if not rows:
    print(f"No selected Top stocks for run_id={run_id}; skip Top history download.")
    raise SystemExit(0)

updated = 0
skipped = 0
failed = 0
for row in rows:
    code = str(row["code"] or "").strip()
    name = str(row["name"] or "")
    rank_no = int(row["rank_no"] or 0)
    if not code:
        continue

    with db.connect(db_path) as conn:
        existing = conn.execute(
            """
            SELECT COUNT(*) AS cnt, MAX(trade_date) AS latest_trade_date
            FROM eastmoney_stock_daily_klines
            WHERE code = ?
            """,
            (code,),
        ).fetchone()
    existing_days = int(existing["cnt"] or 0)
    latest_trade_date = str(existing["latest_trade_date"] or "")
    if (
        existing_days >= min_existing_days
        and latest_trade_date
        and (not target_date or latest_trade_date >= target_date)
    ):
        print(
            f"Skip Top#{rank_no} {code} {name}: already {existing_days} rows, latest={latest_trade_date} >= {target_date}"
        )
        skipped += 1
        continue

    market = infer_market(code)
    url = quote_url(code)
    try:
        payload = fetch_stock_history_1y(market, code)
        row_count = save_history_klines(db_path, url, market, code, payload)
        api_name = payload.get("data", {}).get("name")
        print(
            f"Updated Top#{rank_no} {code} ({api_name or name or 'UNKNOWN'}): saved {row_count} rows, latest target={target_date}"
        )
        updated += 1
    except Exception as exc:
        print(f"Failed Top#{rank_no} {code} {name}: {exc}")
        failed += 1

print(f"Top history download summary: total={len(rows)}, updated={updated}, skipped={skipped}, failed={failed}")
PY
  fi
  echo
else
  echo "Step 4/6: Top history download skipped by SKIP_TOP_HISTORY=1"
  echo
fi

if [[ "${SKIP_BACKTEST:-0}" != "1" ]]; then
  echo "Step 5/6: Backtest ML models..."
  "$VENV_PY" "$SCRIPT_DIR/weekly_stock_main.py" --config "$CONFIG_PATH" backtest
  echo
else
  echo "Step 5/6: ML backtest skipped by SKIP_BACKTEST=1"
  echo
fi

echo "Step 6/6: Train ML models and predict Top stocks..."
PREDICT_ARGS=(predict)
if [[ -n "${RUN_ID:-}" ]]; then
  PREDICT_ARGS+=(--run-id "$RUN_ID")
fi
"$VENV_PY" "$SCRIPT_DIR/weekly_stock_main.py" --config "$CONFIG_PATH" "${PREDICT_ARGS[@]}"
echo

echo "Weekly workflow completed."
echo "Open:"
echo "  http://127.0.0.1:8000/screening"
echo "  http://127.0.0.1:8000/top"
