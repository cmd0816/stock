RUN_ID=40 TOP_N=50 .venv/bin/python - <<'PY'
import os
from weekly_stock import db
from eastmoney_to_sqlite import fetch_stock_history_1y, save_history_klines

run_id = int(os.environ["RUN_ID"])
top_n = int(os.environ["TOP_N"])
db_path = "stocks.db"

def infer_market(code: str) -> int:
    return 1 if code.startswith(("6","9")) else 0

def quote_url(code: str) -> str:
    if code.startswith(("8","4")): p = "bj"
    elif code.startswith(("6","9")): p = "sh"
    else: p = "sz"
    return f"https://quote.eastmoney.com/concept/{p}{code}.html"

with db.connect(db_path) as conn:
    rows = conn.execute(
        "SELECT code,name,rank_no FROM weekly_selected_stocks WHERE run_id=? ORDER BY rank_no LIMIT ?",
        (run_id, top_n)
    ).fetchall()

for r in rows:
    code = str(r["code"])
    name = str(r["name"] or "")
    rank = int(r["rank_no"] or 0)
    try:
        payload = fetch_stock_history_1y(infer_market(code), code)
        saved = save_history_klines(db_path, quote_url(code), infer_market(code), code, payload)
        print(f"OK Top#{rank} {code} {name}: saved {saved}")
    except Exception as e:
        print(f"FAIL Top#{rank} {code} {name}: {e}")
PY
