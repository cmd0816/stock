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

"$VENV_PY" "${ARGS[@]}" "$@"

# After a successful run, keep only the latest xuangu batch results in DB.
# Set KEEP_PREVIOUS_XUANGU_LIST=1 to skip cleanup.
if [[ "${KEEP_PREVIOUS_XUANGU_LIST:-0}" == "1" ]]; then
  echo "Skip cleanup: KEEP_PREVIOUS_XUANGU_LIST=1"
  exit 0
fi

if ! "$VENV_PY" - "$SCRIPT_DIR/stocks.db" <<'PY'
import sqlite3
import sys
from pathlib import Path

db_path = Path(sys.argv[1]).expanduser().resolve()
if not db_path.exists():
    print(f"Skip cleanup: DB not found: {db_path}")
    raise SystemExit(0)

with sqlite3.connect(db_path) as conn:
    conn.execute("PRAGMA foreign_keys = ON")
    row = conn.execute(
        """
        SELECT batch_id
        FROM xuangu_batches
        ORDER BY imported_at_utc DESC, batch_id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        print("Skip cleanup: no xuangu batch found.")
        raise SystemExit(0)
    latest_batch_id = str(row[0] or "")
    older_batches = conn.execute(
        """
        SELECT batch_id
        FROM xuangu_batches
        WHERE batch_id <> ?
        ORDER BY imported_at_utc DESC, batch_id DESC
        """,
        (latest_batch_id,),
    ).fetchall()
    if not older_batches:
        print(f"No previous xuangu list to delete. Latest batch: {latest_batch_id}")
        raise SystemExit(0)

    old_ids = [str(r[0]) for r in older_batches]
    placeholders = ",".join("?" for _ in old_ids)
    deleted_results = conn.execute(
        f"DELETE FROM xuangu_results WHERE batch_id IN ({placeholders})",
        old_ids,
    ).rowcount
    deleted_batches = conn.execute(
        f"DELETE FROM xuangu_batches WHERE batch_id IN ({placeholders})",
        old_ids,
    ).rowcount
    conn.commit()
    print(
        f"Deleted previous xuangu lists: batches={deleted_batches}, rows={deleted_results}. "
        f"Kept latest batch={latest_batch_id}"
    )
PY
then
  echo "Warning: failed to cleanup previous xuangu stock lists."
fi
