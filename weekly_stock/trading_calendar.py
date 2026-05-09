from __future__ import annotations

import sqlite3
from datetime import date, datetime
from typing import Iterable, List, Optional, Sequence, Set


def parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def format_date(value: str | date) -> str:
    return parse_date(value).isoformat()


def trade_dates_from_db(conn: sqlite3.Connection) -> List[str]:
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT trade_date
            FROM eastmoney_stock_daily_klines
            WHERE trade_date IS NOT NULL
            ORDER BY trade_date
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [str(row["trade_date"] if isinstance(row, sqlite3.Row) else row[0]) for row in rows]


def trade_dates_from_akshare() -> List[str]:
    try:
        import akshare as ak
    except Exception:
        return []

    try:
        frame = ak.tool_trade_date_hist_sina()
    except Exception:
        return []
    if "trade_date" not in frame:
        return []
    return sorted(str(item)[:10] for item in frame["trade_date"].tolist())


def china_trade_dates(conn: Optional[sqlite3.Connection] = None, prefer_akshare: bool = True) -> List[str]:
    dates = set(trade_dates_from_akshare()) if prefer_akshare else set()
    if conn is not None:
        dates.update(trade_dates_from_db(conn))
    return sorted(dates)


def last_trading_day_on_or_before(target: str | date, trade_dates: Sequence[str]) -> Optional[str]:
    target_text = format_date(target)
    eligible = [item for item in trade_dates if item <= target_text]
    return eligible[-1] if eligible else None


def align_to_last_trading_day(
    target: str | date,
    conn: Optional[sqlite3.Connection] = None,
    prefer_akshare: bool = True,
) -> str:
    trade_dates = china_trade_dates(conn=conn, prefer_akshare=prefer_akshare)
    aligned = last_trading_day_on_or_before(target, trade_dates)
    return aligned or format_date(target)


def weekly_last_trading_days(trade_dates: Iterable[str]) -> Set[str]:
    by_week: dict[tuple[int, int], str] = {}
    for item in sorted(set(str(d)[:10] for d in trade_dates)):
        try:
            day = parse_date(item)
        except ValueError:
            continue
        iso_year, iso_week, _ = day.isocalendar()
        by_week[(iso_year, iso_week)] = item
    return set(by_week.values())
