"""Date-specific coverage checks; never replace a missing market by today's Top-N."""
from __future__ import annotations

import bisect
import sqlite3

from .db import stock_kline_filter_sql


def market_coverage(conn: sqlite3.Connection, as_of_date: str, window: int = 60) -> dict:
    # Historical checks cannot include codes that first appear after the cutoff.
    first_dates = sorted(row[0] for row in conn.execute(
        f"SELECT MIN(trade_date) FROM eastmoney_stock_daily_klines "
        f"WHERE trade_date <= ? AND close > 0 AND {stock_kline_filter_sql()} GROUP BY code",
        (as_of_date,),
    ))
    days = list(conn.execute(
        f"SELECT trade_date, COUNT(DISTINCT code) FROM eastmoney_stock_daily_klines "
        f"WHERE trade_date <= ? AND close > 0 AND {stock_kline_filter_sql()} "
        "GROUP BY trade_date ORDER BY trade_date DESC LIMIT ?",
        (as_of_date, max(1, window)),
    ))
    points = [
        {"date": day, "available": count, "expected": bisect.bisect_right(first_dates, day),
         "ratio": count / max(1, bisect.bisect_right(first_dates, day))}
        for day, count in days
    ]
    if not points or points[0]["date"] != as_of_date:
        points.insert(0, {"date": as_of_date, "available": 0, "expected": len(first_dates), "ratio": 0.0})
    return {"as_of_date": as_of_date, "days": points, "minimum_ratio": min(p["ratio"] for p in points)}


def require_market_coverage(conn: sqlite3.Connection, as_of_date: str, cfg: dict) -> dict:
    threshold = float(cfg.get("min_market_coverage", 0.90))
    if not 0 <= threshold <= 1:
        raise ValueError("min_market_coverage must be between 0 and 1")
    report = market_coverage(conn, as_of_date, int(cfg.get("lookback_trading_days", 60)))
    failures = [p for p in report["days"] if p["ratio"] < threshold]
    if failures:
        point = failures[0]
        raise RuntimeError(
            f"行情覆盖不足，预测/回测不可用：{point['date']} "
            f"{point['available']}/{point['expected']} ({point['ratio']:.1%}) < {threshold:.0%}。"
            f"请运行 refresh_market_history.py --end-date {as_of_date} 补齐统一股票池；"
            "不能用少量最新候选股代替全市场横截面。"
        )
    return report
