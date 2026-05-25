#!/usr/bin/env python3
"""Download daily K-line for a xuangu batch: BaoStock first, AKShare fallback."""
from __future__ import annotations

import argparse
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from baostock_to_sqlite import (
    choose_batch_id,
    fetch_kline_rows_baostock,
    infer_market,
    load_batch_stocks,
    save_baostock_kline_rows,
)
from download_top_history_akshare import fetch_kline_with_akshare, save_akshare_kline_rows


def get_existing_kline_stats(db_path: Path) -> dict[str, tuple[int, str]]:
    if not db_path.exists():
        return {}
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT code, COUNT(*) AS day_count, MAX(trade_date) AS latest_trade_date
            FROM eastmoney_stock_daily_klines
            GROUP BY code
            """
        ).fetchall()
    return {str(code): (int(day_count or 0), str(latest_trade_date or "")) for code, day_count, latest_trade_date in rows}


def main() -> None:
    parser = argparse.ArgumentParser(description="Download batch history: BaoStock first, AKShare fallback.")
    parser.add_argument("--db", default="stocks.db", help="SQLite DB path")
    parser.add_argument("--batch-id", default="", help="Xuangu batch id; default: latest batch")
    parser.add_argument("--end-date", default="", help="End date YYYY-MM-DD; default: today")
    parser.add_argument("--days", type=int, default=365, help="Lookback days")
    parser.add_argument("--delay", type=float, default=0.2, help="Seconds between stocks")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of stocks (0 = no limit)")
    parser.add_argument("--min-existing-days", type=int, default=200, help="Skip stocks with >= N K-line rows")
    parser.add_argument("--adjust", choices=["qfq", "hfq", "none"], default="qfq", help="Adjustment mode")
    args = parser.parse_args()

    db_path = Path(args.db).expanduser().resolve()
    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")

    end_date = datetime.strptime(args.end_date, "%Y-%m-%d").date() if args.end_date else datetime.now().date()
    start_date = end_date - timedelta(days=max(1, int(args.days)))
    start_text = start_date.strftime("%Y-%m-%d")
    end_text = end_date.strftime("%Y-%m-%d")

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        batch_id = choose_batch_id(conn, args.batch_id)
        targets = load_batch_stocks(conn, batch_id)

    if not targets:
        raise SystemExit(f"No stocks found in batch {batch_id}")

    existing_stats = get_existing_kline_stats(db_path)
    pending: list[tuple[str, str]] = []
    for code, name in targets:
        day_count, latest = existing_stats.get(code, (0, ""))
        if args.end_date:
            # Daily update mode: when end-date is specified, only skip stocks that
            # are already updated to (or beyond) the target date.
            if latest >= args.end_date:
                continue
        else:
            # Bulk backfill mode: allow skipping by minimum existing history rows.
            if day_count >= args.min_existing_days:
                continue
        pending.append((code, name))

    if args.limit > 0:
        pending = pending[: int(args.limit)]

    if not pending:
        print(f"All {len(targets)} stocks already have sufficient K-line data; skip download.")
        return

    print(
        f"Batch {batch_id}: {len(pending)} stocks pending "
        f"(skipped {len(targets) - len(pending)} already covered)"
    )

    baostock_ok: list[str] = []
    baostock_failed: list[tuple[str, str, str]] = []

    try:
        import baostock as bs

        lg = bs.login()
        if str(getattr(lg, "error_code", "")) != "0":
            raise RuntimeError(f"BaoStock login failed: {lg.error_code}")

        writer_conn = sqlite3.connect(db_path)
        writer_conn.execute("PRAGMA busy_timeout = 5000")
        writer_conn.execute("PRAGMA journal_mode = WAL")
        writer_conn.execute("PRAGMA synchronous = NORMAL")

        adjustflag_map = {"hfq": "1", "qfq": "2", "none": "3"}
        adjustflag = adjustflag_map[args.adjust]

        try:
            for idx, (code, name) in enumerate(pending, start=1):
                try:
                    rows = fetch_kline_rows_baostock(bs, code, start_text, end_text, adjustflag)
                    saved = save_baostock_kline_rows(
                        db_path, infer_market(code), code, name, rows, args.adjust, conn=writer_conn
                    )
                    writer_conn.commit()
                    baostock_ok.append(code)
                    print(f"[{idx}/{len(pending)}] {code} {name}: saved {saved} rows via BaoStock")
                except Exception as exc:
                    baostock_failed.append((code, name, str(exc)))
                    print(f"[{idx}/{len(pending)}] {code} {name} FAILED (BaoStock): {exc}")
                if args.delay > 0 and idx < len(pending):
                    time.sleep(args.delay)
        finally:
            try:
                writer_conn.close()
            except Exception:
                pass
            try:
                bs.logout()
            except Exception:
                pass
    except Exception as exc:
        print(f"BaoStock overall failure: {exc}")
        baostock_failed = [
            (code, name, "BaoStock unavailable or login failed")
            for code, name in pending
            if code not in baostock_ok
        ]

    akshare_ok = 0
    akshare_failed = 0
    akshare_adjust = "" if args.adjust == "none" else args.adjust

    if baostock_failed:
        print(f"\nFalling back to AKShare for {len(baostock_failed)} stocks...")
        for idx, (code, name, _reason) in enumerate(baostock_failed, start=1):
            try:
                end_yyyymmdd = end_text.replace("-", "")
                start_yyyymmdd = start_text.replace("-", "")
                df, source_name = fetch_kline_with_akshare(
                    code, start_yyyymmdd, end_yyyymmdd, akshare_adjust, source="auto"
                )
                records = df.to_dict(orient="records")
                if not records:
                    raise RuntimeError("AKShare returned empty rows")
                row_count = save_akshare_kline_rows(
                    db_path, infer_market(code), code, name, records, source_name
                )
                akshare_ok += 1
                print(
                    f"[{idx}/{len(baostock_failed)}] {code} {name}: "
                    f"saved {row_count} rows via AKShare ({source_name})"
                )
            except Exception as exc:
                akshare_failed += 1
                print(f"[{idx}/{len(baostock_failed)}] {code} {name} FAILED (AKShare): {exc}")
            if args.delay > 0 and idx < len(baostock_failed):
                time.sleep(args.delay)

    total_ok = len(baostock_ok) + akshare_ok
    total_failed = len(baostock_failed) - akshare_ok
    print(
        f"\nDownload summary: total={len(pending)}, "
        f"baostock_ok={len(baostock_ok)}, akshare_ok={akshare_ok}, failed={total_failed}"
    )


if __name__ == "__main__":
    main()
