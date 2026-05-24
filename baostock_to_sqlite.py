#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from eastmoney_to_sqlite import init_db


def to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text or text == "-":
            return None
        if text.endswith("%"):
            text = text[:-1].strip()
        try:
            return float(text)
        except Exception:
            return None
    try:
        return float(value)
    except Exception:
        return None


def infer_market(code: str) -> int:
    text = str(code or "").strip()
    if text.startswith(("6", "9")):
        return 1
    return 0


def to_baostock_code(code: str) -> str:
    text = str(code or "").strip()
    if not text:
        raise ValueError("empty stock code")
    if text.startswith(("6", "9")):
        return f"sh.{text}"
    if text.startswith(("8", "4")):
        return f"bj.{text}"
    return f"sz.{text}"


def choose_batch_id(conn: sqlite3.Connection, batch_id: str) -> str:
    if batch_id:
        return batch_id
    row = conn.execute(
        "SELECT batch_id FROM xuangu_batches ORDER BY imported_at_utc DESC LIMIT 1"
    ).fetchone()
    if not row:
        raise RuntimeError("No xuangu batch found. Please pass --batch-id or import xuangu data first.")
    return str(row[0])


def load_batch_stocks(conn: sqlite3.Connection, batch_id: str) -> list[tuple[str, str]]:
    rows = conn.execute(
        """
        SELECT stock_code, COALESCE(MAX(stock_name), '') AS stock_name
        FROM xuangu_results
        WHERE batch_id = ?
          AND stock_code IS NOT NULL
          AND stock_code <> ''
        GROUP BY stock_code
        ORDER BY stock_code
        """,
        (batch_id,),
    ).fetchall()
    return [(str(row[0]), str(row[1] or "")) for row in rows]


def filter_codes_with_null_turnover(
    conn: sqlite3.Connection, codes: list[str], start_date: str, end_date: str
) -> set[str]:
    if not codes:
        return set()
    placeholders = ",".join(["?"] * len(codes))
    rows = conn.execute(
        f"""
        SELECT DISTINCT code
        FROM eastmoney_stock_daily_klines
        WHERE code IN ({placeholders})
          AND trade_date BETWEEN ? AND ?
          AND turnover_rate IS NULL
        """,
        [*codes, start_date, end_date],
    ).fetchall()
    return {str(row[0]) for row in rows}


def fetch_kline_rows_baostock(
    bs: Any, code: str, start_date: str, end_date: str, adjustflag: str
) -> list[dict[str, str]]:
    fields = "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg"
    rs = bs.query_history_k_data_plus(
        to_baostock_code(code),
        fields,
        start_date=start_date,
        end_date=end_date,
        frequency="d",
        adjustflag=adjustflag,
    )
    if str(rs.error_code) != "0":
        raise RuntimeError(f"query failed: error_code={rs.error_code}, error_msg={rs.error_msg}")
    output: list[dict[str, str]] = []
    while rs.next():
        row = dict(zip(rs.fields, rs.get_row_data()))
        output.append(row)
    return output


def convert_baostock_row(row: dict[str, Any]) -> dict[str, Any] | None:
    trade_date = str(row.get("date") or "").strip()
    if not trade_date:
        return None

    open_ = to_float(row.get("open"))
    close = to_float(row.get("close"))
    high = to_float(row.get("high"))
    low = to_float(row.get("low"))
    preclose = to_float(row.get("preclose"))
    volume = to_float(row.get("volume"))
    amount = to_float(row.get("amount"))
    turnover_rate = to_float(row.get("turn"))
    change_percent = to_float(row.get("pctChg"))
    change_amount = (close - preclose) if (close is not None and preclose is not None) else None

    amplitude_percent = None
    if high is not None and low is not None and preclose not in (None, 0):
        amplitude_percent = (high - low) / preclose * 100

    return {
        "trade_date": trade_date,
        "open": open_,
        "close": close,
        "high": high,
        "low": low,
        "volume": volume,
        "turnover": amount,
        "amplitude_percent": amplitude_percent,
        "change_percent": change_percent,
        "change_amount": change_amount,
        "turnover_rate": turnover_rate,
        "raw_line": json.dumps(row, ensure_ascii=False),
    }


def save_baostock_kline_rows(
    db_path: Path,
    market: int,
    code: str,
    name: str,
    rows: list[dict[str, Any]],
    adjust_label: str,
    conn: sqlite3.Connection | None = None,
) -> int:
    secid = f"{market}.{code}"
    fetched_at_utc = datetime.now(timezone.utc).isoformat()
    source_url = f"baostock:query_history_k_data_plus:adjust={adjust_label}"

    values = []
    for row in rows:
        converted = convert_baostock_row(row)
        if not converted:
            continue
        values.append(
            (
                source_url,
                market,
                code,
                secid,
                name,
                converted["trade_date"],
                converted["open"],
                converted["close"],
                converted["high"],
                converted["low"],
                converted["volume"],
                converted["turnover"],
                converted["amplitude_percent"],
                converted["change_percent"],
                converted["change_amount"],
                converted["turnover_rate"],
                converted["raw_line"],
                fetched_at_utc,
            )
        )

    if not values:
        return 0

    if conn is None:
        with sqlite3.connect(db_path) as new_conn:
            init_db(new_conn)
            new_conn.executemany(
                """
                INSERT INTO eastmoney_stock_daily_klines (
                    source_url, market, code, secid, name, trade_date,
                    open, close, high, low, volume, turnover,
                    amplitude_percent, change_percent, change_amount, turnover_rate,
                    raw_line, fetched_at_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(code, trade_date) DO UPDATE SET
                    source_url=excluded.source_url,
                    market=excluded.market,
                    secid=excluded.secid,
                    name=COALESCE(excluded.name, eastmoney_stock_daily_klines.name),
                    open=COALESCE(excluded.open, eastmoney_stock_daily_klines.open),
                    close=COALESCE(excluded.close, eastmoney_stock_daily_klines.close),
                    high=COALESCE(excluded.high, eastmoney_stock_daily_klines.high),
                    low=COALESCE(excluded.low, eastmoney_stock_daily_klines.low),
                    volume=COALESCE(excluded.volume, eastmoney_stock_daily_klines.volume),
                    turnover=COALESCE(excluded.turnover, eastmoney_stock_daily_klines.turnover),
                    amplitude_percent=COALESCE(excluded.amplitude_percent, eastmoney_stock_daily_klines.amplitude_percent),
                    change_percent=COALESCE(excluded.change_percent, eastmoney_stock_daily_klines.change_percent),
                    change_amount=COALESCE(excluded.change_amount, eastmoney_stock_daily_klines.change_amount),
                    turnover_rate=COALESCE(excluded.turnover_rate, eastmoney_stock_daily_klines.turnover_rate),
                    raw_line=COALESCE(excluded.raw_line, eastmoney_stock_daily_klines.raw_line),
                    fetched_at_utc=excluded.fetched_at_utc
                """,
                values,
            )
            new_conn.commit()
        return len(values)

    init_db(conn)
    conn.executemany(
            """
            INSERT INTO eastmoney_stock_daily_klines (
                source_url, market, code, secid, name, trade_date,
                open, close, high, low, volume, turnover,
                amplitude_percent, change_percent, change_amount, turnover_rate,
                raw_line, fetched_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code, trade_date) DO UPDATE SET
                source_url=excluded.source_url,
                market=excluded.market,
                secid=excluded.secid,
                name=COALESCE(excluded.name, eastmoney_stock_daily_klines.name),
                open=COALESCE(excluded.open, eastmoney_stock_daily_klines.open),
                close=COALESCE(excluded.close, eastmoney_stock_daily_klines.close),
                high=COALESCE(excluded.high, eastmoney_stock_daily_klines.high),
                low=COALESCE(excluded.low, eastmoney_stock_daily_klines.low),
                volume=COALESCE(excluded.volume, eastmoney_stock_daily_klines.volume),
                turnover=COALESCE(excluded.turnover, eastmoney_stock_daily_klines.turnover),
                amplitude_percent=COALESCE(excluded.amplitude_percent, eastmoney_stock_daily_klines.amplitude_percent),
                change_percent=COALESCE(excluded.change_percent, eastmoney_stock_daily_klines.change_percent),
                change_amount=COALESCE(excluded.change_amount, eastmoney_stock_daily_klines.change_amount),
                turnover_rate=COALESCE(excluded.turnover_rate, eastmoney_stock_daily_klines.turnover_rate),
                raw_line=COALESCE(excluded.raw_line, eastmoney_stock_daily_klines.raw_line),
                fetched_at_utc=excluded.fetched_at_utc
            """,
            values,
        )
    return len(values)


def parse_codes(raw_codes: list[str]) -> list[str]:
    out: list[str] = []
    seen = set()
    for item in raw_codes:
        for token in str(item or "").replace(",", " ").split():
            code = token.strip()
            if not code or code in seen:
                continue
            seen.add(code)
            out.append(code)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Import A-share daily K-line data from BaoStock to SQLite.")
    parser.add_argument("--db", default="stocks.db", help="SQLite DB path")
    parser.add_argument("--batch-id", default="", help="xuangu batch id; default: latest batch")
    parser.add_argument("--codes", nargs="*", default=[], help="Stock codes, e.g. 600000 000001")
    parser.add_argument("--start-date", default="", help="Start date YYYY-MM-DD; default: end_date - days")
    parser.add_argument("--end-date", default="", help="End date YYYY-MM-DD; default: today")
    parser.add_argument("--days", type=int, default=365, help="Lookback days when start-date is empty")
    parser.add_argument("--adjust", choices=["qfq", "hfq", "none"], default="qfq", help="Adjustment mode")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of stocks (0 means no limit)")
    parser.add_argument("--delay", type=float, default=0.2, help="Delay seconds between stocks")
    parser.add_argument(
        "--only-null-turnover",
        action="store_true",
        help="Only update stocks that have NULL turnover_rate rows in [start-date, end-date]",
    )
    args = parser.parse_args()

    db_path = Path(args.db).expanduser().resolve()
    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")

    if args.end_date:
        end_date = datetime.strptime(args.end_date, "%Y-%m-%d").date()
    else:
        end_date = datetime.now().date()
    if args.start_date:
        start_date = datetime.strptime(args.start_date, "%Y-%m-%d").date()
    else:
        start_date = end_date - timedelta(days=max(1, int(args.days)))
    if start_date > end_date:
        raise SystemExit("start-date must be <= end-date")
    start_text = start_date.strftime("%Y-%m-%d")
    end_text = end_date.strftime("%Y-%m-%d")

    adjustflag_map = {"hfq": "1", "qfq": "2", "none": "3"}
    adjustflag = adjustflag_map[args.adjust]

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        explicit_codes = parse_codes(args.codes)
        if explicit_codes:
            name_by_code = {
                str(row["stock_code"]): str(row["stock_name"] or "")
                for row in conn.execute(
                    """
                    SELECT stock_code, MAX(stock_name) AS stock_name
                    FROM xuangu_results
                    WHERE stock_code IN ({})
                    GROUP BY stock_code
                    """.format(",".join(["?"] * len(explicit_codes))),
                    explicit_codes,
                ).fetchall()
            }
            targets = [(code, name_by_code.get(code, "")) for code in explicit_codes]
        else:
            batch_id = choose_batch_id(conn, args.batch_id)
            targets = load_batch_stocks(conn, batch_id)
            if not targets:
                raise SystemExit(f"No stocks found in batch {batch_id}")
            print(f"Using xuangu batch: {batch_id} ({len(targets)} stocks)")

        if args.only_null_turnover:
            allow_codes = filter_codes_with_null_turnover(
                conn, [code for code, _ in targets], start_text, end_text
            )
            targets = [(code, name) for code, name in targets if code in allow_codes]
            print(f"Filtered by NULL turnover_rate: {len(targets)} stocks in {start_text}..{end_text}")

    if args.limit > 0:
        targets = targets[: int(args.limit)]

    if not targets:
        print("No target stocks to update.")
        return

    try:
        import baostock as bs
    except Exception as exc:
        raise RuntimeError("baostock is required. Install with: pip install baostock") from exc

    lg = bs.login()
    if str(getattr(lg, "error_code", "")) != "0":
        raise RuntimeError(f"BaoStock login failed: error_code={lg.error_code}, error_msg={lg.error_msg}")

    updated = 0
    failed = 0
    writer_conn = sqlite3.connect(db_path)
    writer_conn.execute("PRAGMA busy_timeout = 5000")
    writer_conn.execute("PRAGMA journal_mode = WAL")
    writer_conn.execute("PRAGMA synchronous = NORMAL")
    try:
        for idx, (code, name) in enumerate(targets, start=1):
            try:
                rows = fetch_kline_rows_baostock(bs, code, start_text, end_text, adjustflag)
                saved = save_baostock_kline_rows(
                    db_path,
                    infer_market(code),
                    code,
                    name,
                    rows,
                    args.adjust,
                    conn=writer_conn,
                )
                writer_conn.commit()
                updated += 1
                print(
                    f"[{idx}/{len(targets)}] {code} {name or 'UNKNOWN'}: "
                    f"saved {saved} rows via BaoStock (adjust={args.adjust})"
                )
            except Exception as exc:
                failed += 1
                print(f"[{idx}/{len(targets)}] {code} {name or 'UNKNOWN'} FAILED: {exc}")
            if args.delay > 0 and idx < len(targets):
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

    print(
        f"BaoStock import summary: total={len(targets)}, updated={updated}, failed={failed}, "
        f"range={start_text}..{end_text}, adjust={args.adjust}"
    )


if __name__ == "__main__":
    main()
