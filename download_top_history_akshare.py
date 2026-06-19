#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from eastmoney_to_sqlite import init_db
from weekly_stock import db


def infer_market(code: str) -> int:
    text = str(code or "").strip()
    if text.startswith(("6", "9")):
        return 1
    return 0


def normalize_trade_date(value: Any) -> str:
    text = str(value).strip()
    if not text:
        return ""
    if " " in text:
        text = text.split(" ", 1)[0]
    if "-" in text:
        return text
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return text


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


def pick_first(item: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        if key in item and item.get(key) is not None:
            return item.get(key)
    return None


def extract_turnover_rate(item: dict[str, Any], source_name: str) -> float | None:
    direct = to_float(pick_first(item, ["换手率", "turnover_rate"]))
    if direct is not None:
        return direct
    turnover = to_float(item.get("turnover"))
    if turnover is None:
        return None
    if source_name == "stock_zh_a_daily":
        return turnover
    return turnover * 100


def fetch_kline_with_akshare(symbol: str, start_yyyymmdd: str, end_yyyymmdd: str, adjust: str, source: str = "auto"):
    try:
        import akshare as ak
    except Exception as exc:
        raise RuntimeError("akshare is required. Install with: pip install akshare") from exc

    source = (source or "auto").lower().strip()
    use_em = source in ("auto", "em", "eastmoney")
    use_sina = source in ("auto", "sina")
    errors = []

    if use_em:
        try:
            df = ak.stock_zh_a_hist(
                symbol=symbol,
                period="daily",
                start_date=start_yyyymmdd,
                end_date=end_yyyymmdd,
                adjust=adjust,
            )
            if len(df) > 0:
                return df, "stock_zh_a_hist"
        except Exception as exc:
            errors.append(f"stock_zh_a_hist: {exc}")
        if not use_sina:
            raise RuntimeError(" | ".join(errors))

    if use_sina:
        try:
            symbol_sina = ("sh" if str(symbol).startswith(("6", "9")) else "sz") + str(symbol)
            df = ak.stock_zh_a_daily(symbol=symbol_sina, adjust=adjust)
            if "date" in df.columns:
                start_dt = datetime.strptime(start_yyyymmdd, "%Y%m%d").date()
                end_dt = datetime.strptime(end_yyyymmdd, "%Y%m%d").date()
                date_series = df["date"]
                mask = (date_series >= start_dt) & (date_series <= end_dt)
                df = df.loc[mask].copy()
            if len(df) > 0:
                return df, "stock_zh_a_daily"
        except Exception as exc:
            errors.append(f"stock_zh_a_daily: {exc}")

    raise RuntimeError("all akshare sources failed: " + " | ".join(errors))


def save_akshare_kline_rows(db_path: Path, market: int, code: str, name: str, rows: list[dict[str, Any]], source_name: str = "stock_zh_a_hist") -> int:
    secid = f"{market}.{code}"
    fetched_at_utc = datetime.now(timezone.utc).isoformat()
    source_url = f"akshare:{source_name}"
    sql = """
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
    """

    values = []
    for item in rows:
        trade_date = normalize_trade_date(item.get("日期") or item.get("date"))
        if not trade_date:
            continue
        values.append(
            (
                source_url,
                market,
                code,
                secid,
                name or str(item.get("股票名称") or ""),
                trade_date,
                to_float(pick_first(item, ["开盘", "open"])),
                to_float(pick_first(item, ["收盘", "close"])),
                to_float(pick_first(item, ["最高", "high"])),
                to_float(pick_first(item, ["最低", "low"])),
                to_float(pick_first(item, ["成交量", "volume", "vol"])),
                to_float(pick_first(item, ["成交额", "amount"])),
                to_float(pick_first(item, ["振幅", "amplitude"])),
                to_float(pick_first(item, ["涨跌幅", "pct_chg"])),
                to_float(pick_first(item, ["涨跌额", "change"])),
                extract_turnover_rate(item, source_name),
                json.dumps(item, ensure_ascii=False, default=str),
                fetched_at_utc,
            )
        )

    with sqlite3.connect(db_path) as conn:
        init_db(conn)
        conn.executemany(sql, values)
        conn.commit()
    return len(values)


def main() -> None:
    parser = argparse.ArgumentParser(description="Download/update selected stocks daily K-line with AKShare stock_zh_a_hist.")
    parser.add_argument("--db", default="stocks.db", help="SQLite DB path")
    parser.add_argument("--run-id", type=int, required=True, help="weekly_screen_runs.run_id")
    parser.add_argument("--top-n", type=int, default=10, help="How many ranked stocks to update (default: 10)")
    parser.add_argument("--all-selected", action="store_true", help="Update every selected stock for the run; ignores --top-n")
    parser.add_argument("--target-date", default="", help="Target end date YYYY-MM-DD. Default: today")
    parser.add_argument("--days", type=int, default=365, help="Lookback days (default: 365)")
    parser.add_argument("--adjust", default="qfq", choices=["", "qfq", "hfq"], help="Adjust mode")
    parser.add_argument("--source", default="auto", choices=["auto", "em", "eastmoney", "sina"], help="Data source: auto (default), em/eastmoney, or sina")
    args = parser.parse_args()

    db_path = Path(args.db).expanduser().resolve()
    top_n = 0 if args.all_selected else max(1, int(args.top_n))
    lookback_days = max(30, int(args.days))
    if args.target_date:
        end_dt = datetime.strptime(args.target_date, "%Y-%m-%d")
    else:
        end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=lookback_days)
    start_yyyymmdd = start_dt.strftime("%Y%m%d")
    end_yyyymmdd = end_dt.strftime("%Y%m%d")

    with db.connect(db_path) as conn:
        if top_n > 0:
            rows = conn.execute(
                """
                SELECT code, name, rank_no
                FROM weekly_selected_stocks
                WHERE run_id = ?
                ORDER BY rank_no
                LIMIT ?
                """,
                (int(args.run_id), top_n),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT code, name, rank_no
                FROM weekly_selected_stocks
                WHERE run_id = ?
                ORDER BY rank_no
                """,
                (int(args.run_id),),
            ).fetchall()

    if not rows:
        raise SystemExit(f"No selected stocks found for run_id={args.run_id}")

    updated = 0
    failed = 0
    for row in rows:
        code = str(row["code"] or "").strip()
        name = str(row["name"] or "")
        rank_no = int(row["rank_no"] or 0)
        if not code:
            continue
        market = infer_market(code)
        try:
            df, source_name = fetch_kline_with_akshare(code, start_yyyymmdd, end_yyyymmdd, args.adjust, args.source)
            records = df.to_dict(orient="records")
            if not records:
                raise RuntimeError("AKShare returned empty rows")
            row_count = save_akshare_kline_rows(db_path, market, code, name, records, source_name)
            print(f"Updated Top#{rank_no} {code} {name}: saved {row_count} rows via AKShare ({source_name})")
            updated += 1
        except Exception as exc:
            print(f"Failed Top#{rank_no} {code} {name}: {exc}")
            failed += 1

    print(f"Summary: updated={updated}, failed={failed}, total={len(rows)}")


if __name__ == "__main__":
    main()
