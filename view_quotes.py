#!/usr/bin/env python3
import argparse
import html
import json
import sqlite3
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import parse_qs, urlparse


def fetch_rows(db_path: Path, limit: int) -> List[Dict[str, Any]]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                id, code, name, secid, price, open, high, low,
                prev_close, volume, turnover, change_amount, change_percent,
                turnover_rate, total_market_value, circulating_market_value,
                pe_ttm, pb, fetched_at_utc
            FROM eastmoney_stock_quotes
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def fetch_daily_rows(db_path: Path, limit: int, code: str = "") -> List[Dict[str, Any]]:
    sql = """
        SELECT
            id, code, name, secid, trade_date, open, close, high, low,
            volume, turnover, amplitude_percent, change_percent,
            change_amount, turnover_rate, fetched_at_utc
        FROM eastmoney_stock_daily_klines
    """
    params: List[Any] = []
    if code:
        sql += " WHERE code = ? "
        params.append(code)
    sql += " ORDER BY trade_date DESC LIMIT ? "
    params.append(limit)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def fetch_stock_list(db_path: Path) -> List[Dict[str, Any]]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            WITH latest AS (
                SELECT
                    code,
                    COALESCE(MAX(name), '') AS name,
                    MAX(trade_date) AS last_date,
                    COUNT(*) AS day_count
                FROM eastmoney_stock_daily_klines
                GROUP BY code
            )
            SELECT
                l.code,
                l.name,
                l.last_date,
                l.day_count,
                k.close AS last_close,
                k.change_percent AS last_change_percent
            FROM latest l
            LEFT JOIN eastmoney_stock_daily_klines k
                ON k.code = l.code AND k.trade_date = l.last_date
            ORDER BY l.code
            """
        ).fetchall()
    return [dict(r) for r in rows]


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def fetch_dashboard_checks(db_path: Path) -> Dict[str, Any]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        out: Dict[str, Any] = {}

        if table_exists(conn, "eastmoney_stock_daily_klines"):
            out["kline_coverage"] = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT
                        code,
                        COALESCE(MAX(name), '') AS name,
                        COUNT(*) AS day_count,
                        MIN(trade_date) AS first_date,
                        MAX(trade_date) AS last_date
                    FROM eastmoney_stock_daily_klines
                    GROUP BY code
                    ORDER BY code
                    """
                ).fetchall()
            ]
        else:
            out["kline_coverage"] = []

        if table_exists(conn, "xuangu_batches"):
            out["xuangu_batches"] = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT batch_id, imported_at_utc, row_count, xlsx_path
                    FROM xuangu_batches
                    ORDER BY imported_at_utc DESC
                    LIMIT 10
                    """
                ).fetchall()
            ]
        else:
            out["xuangu_batches"] = []

        if table_exists(conn, "xuangu_results") and table_exists(conn, "xuangu_batches"):
            out["latest_xuangu_results"] = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT stock_code, stock_name
                    FROM xuangu_results
                    WHERE batch_id = (
                        SELECT batch_id FROM xuangu_batches
                        ORDER BY imported_at_utc DESC
                        LIMIT 1
                    )
                    AND stock_code IS NOT NULL
                    ORDER BY stock_code
                    LIMIT 80
                    """
                ).fetchall()
            ]
        else:
            out["latest_xuangu_results"] = []

        if table_exists(conn, "weekly_selected_stocks"):
            out["weekly_selected"] = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT screen_date, code, name, rank_no, total_score, selected_reason
                    FROM weekly_selected_stocks
                    ORDER BY id DESC
                    LIMIT 20
                    """
                ).fetchall()
            ]
        else:
            out["weekly_selected"] = []

        if table_exists(conn, "weekly_review_results"):
            out["weekly_reviews"] = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT
                        code, name, highest_gain_pct, close_gain_pct, max_drawdown_pct,
                        stop_loss_triggered, meets_expectation, notes
                    FROM weekly_review_results
                    ORDER BY id DESC
                    LIMIT 20
                    """
                ).fetchall()
            ]
        else:
            out["weekly_reviews"] = []

    return out


def build_kline_svg(rows_desc: List[Dict[str, Any]]) -> str:
    data = list(reversed(rows_desc[-120:]))
    points = [r for r in data if all(r.get(k) is not None for k in ("open", "close", "high", "low"))]
    if not points:
        return "<div style='padding:16px;color:#6b7280'>No K-line data available.</div>"

    lows = [float(r["low"]) for r in points]
    highs = [float(r["high"]) for r in points]
    min_p, max_p = min(lows), max(highs)
    if max_p <= min_p:
        max_p = min_p + 1.0

    pad_left, pad_right, pad_top, pad_bottom = 42, 12, 20, 24
    candle_w, gap = 6, 2
    plot_h = 280
    plot_w = max(700, len(points) * (candle_w + gap))
    svg_w = pad_left + plot_w + pad_right
    svg_h = pad_top + plot_h + pad_bottom

    def y(px: float) -> float:
        ratio = (px - min_p) / (max_p - min_p)
        return pad_top + (1.0 - ratio) * plot_h

    elems = []
    # Grid lines
    for t in [0.0, 0.25, 0.5, 0.75, 1.0]:
        yy = pad_top + t * plot_h
        elems.append(f"<line x1='{pad_left}' y1='{yy:.1f}' x2='{pad_left + plot_w}' y2='{yy:.1f}' stroke='#e5e7eb' stroke-width='1'/>")

    for i, r in enumerate(points):
        x = pad_left + i * (candle_w + gap)
        o = float(r["open"])
        c = float(r["close"])
        h = float(r["high"])
        l = float(r["low"])
        up = c >= o
        color = "#dc2626" if up else "#16a34a"

        y_open = y(o)
        y_close = y(c)
        y_high = y(h)
        y_low = y(l)
        body_top = min(y_open, y_close)
        body_h = max(1.2, abs(y_close - y_open))
        cx = x + candle_w / 2

        elems.append(f"<line x1='{cx:.2f}' y1='{y_high:.2f}' x2='{cx:.2f}' y2='{y_low:.2f}' stroke='{color}' stroke-width='1'/>")
        elems.append(
            f"<rect x='{x:.2f}' y='{body_top:.2f}' width='{candle_w:.2f}' height='{body_h:.2f}' "
            f"fill='{color}' stroke='{color}' stroke-width='1'/>"
        )

    last_date = html.escape(str(points[-1].get("trade_date", "")))
    first_date = html.escape(str(points[0].get("trade_date", "")))
    max_txt = f"{max_p:,.2f}"
    min_txt = f"{min_p:,.2f}"
    axis = (
        f"<text x='6' y='{pad_top + 10}' font-size='12' fill='#6b7280'>{max_txt}</text>"
        f"<text x='6' y='{pad_top + plot_h}' font-size='12' fill='#6b7280'>{min_txt}</text>"
        f"<text x='{pad_left}' y='{svg_h - 6}' font-size='12' fill='#6b7280'>{first_date}</text>"
        f"<text x='{pad_left + plot_w - 95}' y='{svg_h - 6}' font-size='12' fill='#6b7280'>{last_date}</text>"
    )
    return (
        f"<svg viewBox='0 0 {svg_w} {svg_h}' width='100%' height='{svg_h}' "
        f"xmlns='http://www.w3.org/2000/svg' role='img' aria-label='K line chart'>"
        + "".join(elems)
        + axis
        + "</svg>"
    )


def build_html(rows: List[Dict[str, Any]]) -> str:
    def fmt(v: Any) -> str:
        if v is None:
            return "-"
        if isinstance(v, float):
            return f"{v:,.2f}"
        return str(v)

    row_html = []
    for r in rows:
        cp = r.get("change_percent")
        color = "#b91c1c" if (cp is not None and cp > 0) else "#1d4ed8" if (cp is not None and cp < 0) else "#111827"
        row_html.append(
            "<tr>"
            f"<td>{r['id']}</td>"
            f"<td>{html.escape(str(r['code']))}</td>"
            f"<td>{html.escape(str(r['name'] or ''))}</td>"
            f"<td>{html.escape(str(r['secid']))}</td>"
            f"<td>{fmt(r['price'])}</td>"
            f"<td>{fmt(r['open'])}</td>"
            f"<td>{fmt(r['high'])}</td>"
            f"<td>{fmt(r['low'])}</td>"
            f"<td>{fmt(r['prev_close'])}</td>"
            f"<td style='color:{color}'>{fmt(r['change_percent'])}%</td>"
            f"<td>{fmt(r['volume'])}</td>"
            f"<td>{fmt(r['turnover'])}</td>"
            f"<td>{fmt(r['fetched_at_utc'])}</td>"
            "</tr>"
        )

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta http-equiv="refresh" content="20" />
  <title>Eastmoney Quotes</title>
  <style>
    :root {{
      --bg: #f3f4f6;
      --fg: #111827;
      --card: #ffffff;
      --line: #e5e7eb;
    }}
    body {{
      margin: 0;
      background: radial-gradient(circle at 20% 10%, #dbeafe, transparent 30%), var(--bg);
      color: var(--fg);
      font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
    }}
    .wrap {{
      max-width: 1200px;
      margin: 24px auto;
      padding: 0 16px;
    }}
    .head {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 12px;
    }}
    .card {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 14px;
      overflow: auto;
      box-shadow: 0 8px 20px rgba(0,0,0,0.05);
    }}
    table {{
      border-collapse: collapse;
      width: 100%;
      min-width: 980px;
      font-size: 14px;
    }}
    th, td {{
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      white-space: nowrap;
      text-align: left;
    }}
    th {{
      position: sticky;
      top: 0;
      background: #f9fafb;
      z-index: 1;
    }}
    .meta {{
      font-size: 13px;
      color: #4b5563;
      margin-bottom: 10px;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="head">
      <h2>Eastmoney Quote Snapshots</h2>
      <div>
        <a href="/daily">Daily Kline</a>
        <a href="/checks">Data Checks</a>
        <a href="/api/quotes">JSON API</a>
      </div>
    </div>
    <div class="meta">Auto-refresh: 20s | Generated: {html.escape(now)} | Rows: {len(rows)}</div>
    <div class="card">
      <table>
        <thead>
          <tr>
            <th>ID</th><th>Code</th><th>Name</th><th>SecID</th><th>Price</th><th>Open</th>
            <th>High</th><th>Low</th><th>Prev Close</th><th>Change %</th><th>Volume</th>
            <th>Turnover</th><th>Fetched UTC</th>
          </tr>
        </thead>
        <tbody>
          {''.join(row_html) if row_html else '<tr><td colspan="13">No rows yet.</td></tr>'}
        </tbody>
      </table>
    </div>
  </div>
</body>
</html>"""


def render_simple_table(rows: List[Dict[str, Any]], columns: List[tuple[str, str]], empty: str) -> str:
    if not rows:
        return f"<div class='empty'>{html.escape(empty)}</div>"
    head = "".join(f"<th>{html.escape(label)}</th>" for _, label in columns)
    body = []
    for row in rows:
        cells = []
        for key, _ in columns:
            value = row.get(key)
            if isinstance(value, float):
                text = f"{value:.2f}"
            elif value is None:
                text = "-"
            else:
                text = str(value)
            cells.append(f"<td>{html.escape(text)}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f"""
      <div class="table-wrap">
        <table>
          <thead><tr>{head}</tr></thead>
          <tbody>{''.join(body)}</tbody>
        </table>
      </div>
    """


def build_checks_html(checks: Dict[str, Any]) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    kline = render_simple_table(
        checks.get("kline_coverage", []),
        [
            ("code", "代码"),
            ("name", "名称"),
            ("day_count", "K线天数"),
            ("first_date", "开始日期"),
            ("last_date", "结束日期"),
        ],
        "还没有 K 线数据。",
    )
    batches = render_simple_table(
        checks.get("xuangu_batches", []),
        [
            ("batch_id", "批次"),
            ("imported_at_utc", "导入时间 UTC"),
            ("row_count", "行数"),
            ("xlsx_path", "文件"),
        ],
        "还没有条件选股批次。",
    )
    latest_results = render_simple_table(
        checks.get("latest_xuangu_results", []),
        [("stock_code", "代码"), ("stock_name", "名称")],
        "最近一次选股批次没有结果。",
    )
    weekly_selected = render_simple_table(
        checks.get("weekly_selected", []),
        [
            ("screen_date", "选股日期"),
            ("code", "代码"),
            ("name", "名称"),
            ("rank_no", "排名"),
            ("total_score", "总分"),
            ("selected_reason", "入选原因"),
        ],
        "还没有周末入选记录。",
    )
    weekly_reviews = render_simple_table(
        checks.get("weekly_reviews", []),
        [
            ("code", "代码"),
            ("name", "名称"),
            ("highest_gain_pct", "最高涨幅%"),
            ("close_gain_pct", "收盘涨幅%"),
            ("max_drawdown_pct", "最大回撤%"),
            ("stop_loss_triggered", "止损"),
            ("meets_expectation", "符合预期"),
            ("notes", "复盘备注"),
        ],
        "还没有复盘记录。",
    )

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta http-equiv="refresh" content="30" />
  <title>Data Checks</title>
  <style>
    :root {{
      --bg: #f3f4f6;
      --fg: #111827;
      --card: #ffffff;
      --line: #e5e7eb;
      --muted: #64748b;
    }}
    body {{
      margin: 0;
      background: linear-gradient(180deg, #f8fafc, var(--bg));
      color: var(--fg);
      font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
    }}
    .wrap {{
      max-width: 1360px;
      margin: 24px auto;
      padding: 0 16px;
    }}
    .head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
      margin-bottom: 12px;
    }}
    .head a {{
      margin-left: 10px;
    }}
    .meta {{
      color: var(--muted);
      font-size: 13px;
      margin-bottom: 14px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: 1fr;
      gap: 14px;
    }}
    .card {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 10px;
      box-shadow: 0 8px 20px rgba(15,23,42,0.05);
      overflow: hidden;
    }}
    .card h3 {{
      margin: 0;
      padding: 12px 14px;
      font-size: 15px;
      border-bottom: 1px solid var(--line);
      background: #f8fafc;
    }}
    .table-wrap {{
      overflow: auto;
    }}
    table {{
      width: 100%;
      min-width: 720px;
      border-collapse: collapse;
      font-size: 13px;
    }}
    th, td {{
      text-align: left;
      padding: 9px 11px;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
    }}
    th {{
      white-space: nowrap;
      background: #ffffff;
      color: #334155;
      font-weight: 700;
    }}
    td {{
      color: #111827;
    }}
    .empty {{
      padding: 16px;
      color: var(--muted);
      font-size: 13px;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="head">
      <h2>数据检查</h2>
      <div>
        <a href="/">Snapshot</a>
        <a href="/daily">Daily Kline</a>
        <a href="/api/checks">JSON API</a>
      </div>
    </div>
    <div class="meta">Auto-refresh: 30s | Generated: {html.escape(now)}</div>
    <div class="grid">
      <section class="card"><h3>K 线覆盖</h3>{kline}</section>
      <section class="card"><h3>最近条件选股批次</h3>{batches}</section>
      <section class="card"><h3>最近一次选股结果</h3>{latest_results}</section>
      <section class="card"><h3>周末入选股票</h3>{weekly_selected}</section>
      <section class="card"><h3>最近复盘结果</h3>{weekly_reviews}</section>
    </div>
  </div>
</body>
</html>"""


def build_daily_html(rows: List[Dict[str, Any]], code: str, stock_list: List[Dict[str, Any]]) -> str:
    chart_rows = list(reversed(rows))
    chart_data_json = json.dumps(chart_rows, ensure_ascii=False).replace("</", "<\\/")
    stock_links = []
    for s in stock_list:
        s_code = str(s.get("code", ""))
        active = "active" if s_code == code else ""
        s_name = html.escape(str(s.get("name", "")))
        cp = s.get("last_change_percent")
        cp_txt = f"{cp:.2f}%" if isinstance(cp, float) else "-"
        cp_cls = "up" if isinstance(cp, float) and cp > 0 else "down" if isinstance(cp, float) and cp < 0 else ""
        stock_links.append(
            f"<a class='stk {active}' href='/daily?code={html.escape(s_code)}'>"
            f"<span class='c'>{html.escape(s_code)}</span>"
            f"<span class='n'>{s_name}</span>"
            f"<span class='p {cp_cls}'>{cp_txt}</span>"
            "</a>"
        )

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    title_suffix = f" ({code})" if code else ""
    selected_name = html.escape(str(rows[0].get("name", ""))) if rows else ""
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta http-equiv="refresh" content="30" />
  <title>Eastmoney Daily Kline</title>
  <style>
    :root {{
      --bg: #f3f4f6;
      --fg: #111827;
      --card: #ffffff;
      --line: #e5e7eb;
    }}
    body {{
      margin: 0;
      background: radial-gradient(circle at 20% 10%, #dcfce7, transparent 30%), var(--bg);
      color: var(--fg);
      font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
    }}
    .wrap {{
      max-width: 1360px;
      margin: 24px auto;
      padding: 0 16px;
    }}
    .layout {{
      display: grid;
      grid-template-columns: 280px 1fr;
      gap: 16px;
    }}
    .side {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 14px;
      box-shadow: 0 8px 20px rgba(0,0,0,0.05);
      overflow: hidden;
      align-self: start;
      position: sticky;
      top: 12px;
    }}
    .side h3 {{
      margin: 0;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      font-size: 15px;
      background: #f9fafb;
    }}
    .stocks {{
      max-height: 74vh;
      overflow: auto;
      display: flex;
      flex-direction: column;
    }}
    .stk {{
      display: grid;
      grid-template-columns: 78px 1fr 70px;
      gap: 8px;
      padding: 10px 12px;
      text-decoration: none;
      color: inherit;
      border-bottom: 1px solid var(--line);
      font-size: 13px;
    }}
    .stk:hover {{
      background: #f3f4f6;
    }}
    .stk.active {{
      background: #ecfeff;
      border-left: 3px solid #0284c7;
      padding-left: 9px;
    }}
    .stk .c {{
      font-weight: 700;
      font-family: "IBM Plex Mono", monospace;
    }}
    .stk .n {{
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .stk .p {{
      text-align: right;
      font-variant-numeric: tabular-nums;
    }}
    .stk .p.up {{
      color: #b91c1c;
    }}
    .stk .p.down {{
      color: #1d4ed8;
    }}
    .main {{
      min-width: 0;
    }}
    .head {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 12px;
      gap: 10px;
      flex-wrap: wrap;
    }}
    .head a {{
      margin-right: 10px;
    }}
    .card {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 14px;
      overflow: auto;
      box-shadow: 0 8px 20px rgba(0,0,0,0.05);
      margin-bottom: 14px;
    }}
    .chart-wrap {{
      position: relative;
      padding: 10px;
    }}
    .chart-grid {{
      display: grid;
      grid-template-columns: 1fr 300px;
      gap: 10px;
      align-items: stretch;
    }}
    #klineChart {{
      width: 100%;
      height: 420px;
      display: block;
      background: #ffffff;
      border-radius: 10px;
    }}
    #chipChart {{
      width: 100%;
      height: 420px;
      display: block;
      background: #ffffff;
      border-radius: 10px;
    }}
    .hover-info {{
      padding: 8px 12px;
      border-bottom: 1px solid var(--line);
      font-size: 13px;
      color: #111827;
      background: #f9fafb;
    }}
    .hover-row {{
      display: grid;
      grid-template-columns: 1fr auto;
      align-items: center;
      gap: 10px;
      border-bottom: 1px solid var(--line);
      background: #f9fafb;
    }}
    .hover-row .hover-info {{
      border-bottom: 0;
    }}
    .chart-controls {{
      display: flex;
      align-items: center;
      gap: 6px;
      padding-right: 10px;
    }}
    .chart-controls button {{
      border: 1px solid #cbd5e1;
      border-radius: 6px;
      background: #fff;
      color: #0f172a;
      font-size: 12px;
      padding: 4px 8px;
      cursor: pointer;
    }}
    .chart-controls button:hover {{
      background: #f1f5f9;
    }}
    .zoom-label {{
      font-size: 12px;
      color: #475569;
      min-width: 80px;
      text-align: right;
    }}
    .tooltip {{
      position: absolute;
      pointer-events: none;
      min-width: 200px;
      padding: 8px 10px;
      border: 1px solid #d1d5db;
      background: rgba(255,255,255,0.96);
      border-radius: 8px;
      box-shadow: 0 6px 16px rgba(0,0,0,0.12);
      font-size: 12px;
      color: #111827;
      display: none;
      z-index: 2;
    }}
    .legend {{
      padding: 8px 12px 10px;
      border-top: 1px solid var(--line);
      font-size: 12px;
      color: #4b5563;
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
    }}
    .legend b {{ font-weight: 600; }}
    .meta {{
      font-size: 13px;
      color: #4b5563;
      margin-bottom: 10px;
    }}
    @media (max-width: 1024px) {{
      .layout {{
        grid-template-columns: 1fr;
      }}
      .side {{
        position: static;
      }}
      .stocks {{
        max-height: 200px;
      }}
      .chart-grid {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="layout">
      <aside class="side">
        <h3>Stock List</h3>
        <div class="stocks">
          {''.join(stock_links) if stock_links else "<div style='padding:12px;color:#6b7280'>No stocks found.</div>"}
        </div>
      </aside>
      <main class="main">
        <div class="head">
          <h2>Eastmoney Daily Kline{html.escape(title_suffix)}</h2>
          <div>
            <a href="/">Snapshot Page</a>
            <a href="/checks">Data Checks</a>
            <a href="/api/daily{('?code=' + html.escape(code)) if code else ''}">JSON API</a>
            <a href="/api/stocks">Stock List API</a>
          </div>
        </div>
        <div class="meta">
          Auto-refresh: 30s | Generated: {html.escape(now)} | Rows: {len(rows)} | Selected: {selected_name}
        </div>
        <div class="card">
          <div class="hover-row">
            <div id="hoverInfo" class="hover-info">Move pointer over chart to inspect daily values.</div>
            <div class="chart-controls">
              <button id="zoomInBtn" type="button">Zoom In</button>
              <button id="zoomOutBtn" type="button">Zoom Out</button>
              <button id="zoomResetBtn" type="button">Reset</button>
              <span id="zoomLabel" class="zoom-label">-</span>
            </div>
          </div>
          <div class="chart-wrap">
            <div class="chart-grid">
              <canvas id="klineChart"></canvas>
              <canvas id="chipChart"></canvas>
            </div>
            <div id="chartTooltip" class="tooltip"></div>
          </div>
          <div class="legend">
            <span><b style="color:#dc2626">Candle Up</b></span>
            <span><b style="color:#16a34a">Candle Down</b></span>
            <span><b style="color:#f59e0b">MA5</b></span>
            <span><b style="color:#3b82f6">MA10</b></span>
            <span><b style="color:#ef4444">MA20</b></span>
            <span><b style="color:#14b8a6">MA30</b></span>
            <span><b style="color:#8b5cf6">MA60</b></span>
            <span><b style="color:#0f766e">MA120</b></span>
            <span><b style="color:#374151">MA250</b></span>
            <span><b style="color:#0369a1">Chip Diagram</b> (estimated volume by price)</span>
          </div>
        </div>
      </main>
    </div>
  </div>
  <script id="klineData" type="application/json">{chart_data_json}</script>
  <script>
    (() => {{
      const rows = JSON.parse(document.getElementById('klineData').textContent || '[]');
      const canvas = document.getElementById('klineChart');
      const chipCanvas = document.getElementById('chipChart');
      const tooltip = document.getElementById('chartTooltip');
      const hoverInfo = document.getElementById('hoverInfo');
      const zoomInBtn = document.getElementById('zoomInBtn');
      const zoomOutBtn = document.getElementById('zoomOutBtn');
      const zoomResetBtn = document.getElementById('zoomResetBtn');
      const zoomLabel = document.getElementById('zoomLabel');
      if (!canvas || !chipCanvas || !zoomInBtn || !zoomOutBtn || !zoomResetBtn || rows.length === 0) {{
        if (hoverInfo) hoverInfo.textContent = 'No daily rows found.';
        return;
      }}

      const dpr = window.devicePixelRatio || 1;
      const ctx = canvas.getContext('2d');
      const chipCtx = chipCanvas.getContext('2d');
      const pad = {{ left: 58, right: 20, top: 18, bottom: 34 }};
      const chartH = 420;
      const maPeriods = [5, 10, 20, 30, 60, 120, 250];
      const maColors = {{
        5: '#f59e0b',
        10: '#3b82f6',
        20: '#ef4444',
        30: '#14b8a6',
        60: '#8b5cf6',
        120: '#0f766e',
        250: '#374151',
      }};

      const points = rows
        .map((r) => ({{
          trade_date: r.trade_date,
          open: Number(r.open),
          close: Number(r.close),
          high: Number(r.high),
          low: Number(r.low),
          volume: Number(r.volume),
          turnover: Number(r.turnover),
          change_percent: Number(r.change_percent),
          change_amount: Number(r.change_amount),
          turnover_rate: Number(r.turnover_rate),
        }}))
        .filter((r) => ![r.open, r.close, r.high, r.low].some((v) => Number.isNaN(v)));

      if (points.length === 0) {{
        hoverInfo.textContent = 'No valid K-line points found.';
        return;
      }}

      const computeMA = (period) => {{
        const out = new Array(points.length).fill(null);
        let sum = 0;
        for (let i = 0; i < points.length; i++) {{
          sum += points[i].close;
          if (i >= period) sum -= points[i - period].close;
          if (i >= period - 1) out[i] = sum / period;
        }}
        return out;
      }};

      const maSeries = {{}};
      maPeriods.forEach((p) => maSeries[p] = computeMA(p));
      let crossIndexAbs = null;
      let visibleCount = Math.min(120, points.length);
      let endIndex = points.length - 1;
      const MIN_VISIBLE = Math.min(20, points.length);

      const fmt = (v, n = 2) => Number.isFinite(v) ? v.toLocaleString(undefined, {{ minimumFractionDigits: n, maximumFractionDigits: n }}) : '-';
      const fmtInt = (v) => Number.isFinite(v) ? Math.round(v).toLocaleString() : '-';
      const clamp = (v, mn, mx) => Math.max(mn, Math.min(mx, v));

      const getStartIndex = () => Math.max(0, endIndex - visibleCount + 1);
      const getVisiblePoints = () => {{
        const start = getStartIndex();
        return points.slice(start, endIndex + 1).map((p, i) => ({{ ...p, __absIdx: start + i }}));
      }};

      const setZoomLabel = () => {{
        if (!zoomLabel) return;
        const start = getStartIndex();
        zoomLabel.textContent = `${{visibleCount}}d (${{start + 1}}-${{endIndex + 1}})`;
      }};

      const applyZoom = (nextCount, anchorAbsIdx = null) => {{
        const oldCount = visibleCount;
        const prevStart = getStartIndex();
        visibleCount = clamp(nextCount, MIN_VISIBLE, points.length);
        if (anchorAbsIdx === null) {{
          endIndex = clamp(endIndex, visibleCount - 1, points.length - 1);
          setZoomLabel();
          render();
          return;
        }}
        const anchor = clamp(anchorAbsIdx, 0, points.length - 1);
        const ratio = oldCount <= 1 ? 1 : (anchor - prevStart) / (oldCount - 1);
        let newStart = Math.round(anchor - ratio * (visibleCount - 1));
        newStart = clamp(newStart, 0, Math.max(0, points.length - visibleCount));
        endIndex = newStart + visibleCount - 1;
        setZoomLabel();
        render();
      }};

      const render = () => {{
        const visible = getVisiblePoints();
        if (visible.length === 0) return;
        const startAbs = visible[0].__absIdx;
        const width = canvas.clientWidth || 900;
        canvas.width = Math.floor(width * dpr);
        canvas.height = Math.floor(chartH * dpr);
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, width, chartH);
        const chipW = chipCanvas.clientWidth || 280;
        chipCanvas.width = Math.floor(chipW * dpr);
        chipCanvas.height = Math.floor(chartH * dpr);
        chipCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
        chipCtx.clearRect(0, 0, chipW, chartH);

        const plotW = width - pad.left - pad.right;
        const plotH = chartH - pad.top - pad.bottom;
        if (plotW <= 20 || plotH <= 20) return;

        const allValues = [];
        for (let i = 0; i < visible.length; i++) {{
          const pAbs = visible[i].__absIdx;
          allValues.push(visible[i].low, visible[i].high);
          for (const p of maPeriods) {{
            const mv = maSeries[p][pAbs];
            if (mv !== null) allValues.push(mv);
          }}
        }}
        let minV = Math.min(...allValues);
        let maxV = Math.max(...allValues);
        if (!(maxV > minV)) maxV = minV + 1;
        const margin = (maxV - minV) * 0.06;
        minV -= margin;
        maxV += margin;

        const xStep = plotW / Math.max(1, visible.length - 1);
        const candleW = Math.max(3, Math.min(9, xStep * 0.65));
        const yOf = (v) => pad.top + (maxV - v) * plotH / (maxV - minV);

        // Grid
        ctx.strokeStyle = '#e5e7eb';
        ctx.lineWidth = 1;
        for (let i = 0; i <= 4; i++) {{
          const y = pad.top + (plotH * i / 4);
          ctx.beginPath();
          ctx.moveTo(pad.left, y);
          ctx.lineTo(width - pad.right, y);
          ctx.stroke();
        }}

        // Candles
        for (let i = 0; i < visible.length; i++) {{
          const p = visible[i];
          const x = pad.left + i * xStep;
          const yo = yOf(p.open);
          const yc = yOf(p.close);
          const yh = yOf(p.high);
          const yl = yOf(p.low);
          const up = p.close >= p.open;
          const color = up ? '#dc2626' : '#16a34a';
          ctx.strokeStyle = color;
          ctx.fillStyle = color;
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.moveTo(x, yh);
          ctx.lineTo(x, yl);
          ctx.stroke();
          const top = Math.min(yo, yc);
          const h = Math.max(1, Math.abs(yc - yo));
          ctx.fillRect(x - candleW / 2, top, candleW, h);
        }}

        // MAs
        for (const period of maPeriods) {{
          const ser = maSeries[period];
          ctx.strokeStyle = maColors[period];
          ctx.lineWidth = 1.4;
          ctx.beginPath();
          let started = false;
          for (let i = 0; i < visible.length; i++) {{
            const absIdx = visible[i].__absIdx;
            const v = ser[absIdx];
            if (v === null) continue;
            const x = pad.left + i * xStep;
            const y = yOf(v);
            if (!started) {{
              ctx.moveTo(x, y);
              started = true;
            }} else {{
              ctx.lineTo(x, y);
            }}
          }}
          if (started) ctx.stroke();
        }}

        // Axis labels
        ctx.fillStyle = '#6b7280';
        ctx.font = '12px sans-serif';
        ctx.fillText(fmt(maxV), 6, pad.top + 10);
        ctx.fillText(fmt(minV), 6, pad.top + plotH);
        ctx.fillText(visible[0].trade_date || '', pad.left, chartH - 8);
        const endTxt = visible[visible.length - 1].trade_date || '';
        const tw = ctx.measureText(endTxt).width;
        ctx.fillText(endTxt, Math.max(pad.left, width - pad.right - tw), chartH - 8);

        // Chip diagram (estimated volume-by-price distribution from OHLC range and volume)
        // Anchor chip window to crosshair day if present; otherwise use current visible end.
        const bins = 52;
        const chip = new Array(bins).fill(0);
        const chipEndAbs = (crossIndexAbs !== null) ? crossIndexAbs : endIndex;
        const recentStart = Math.max(0, chipEndAbs - 249);
        const recent = points.slice(recentStart, chipEndAbs + 1);
        const step = (maxV - minV) / bins;
        for (const p of recent) {{
          if (!Number.isFinite(p.volume) || p.volume <= 0) continue;
          const lo = Math.max(minV, p.low);
          const hi = Math.min(maxV, p.high);
          if (hi <= lo) continue;
          const start = Math.max(0, Math.floor((lo - minV) / step));
          const end = Math.min(bins - 1, Math.floor((hi - minV) / step));
          const span = hi - lo;
          for (let b = start; b <= end; b++) {{
            const bLo = minV + b * step;
            const bHi = bLo + step;
            const overlap = Math.max(0, Math.min(hi, bHi) - Math.max(lo, bLo));
            if (overlap > 0) chip[b] += p.volume * (overlap / span);
          }}
        }}
        const maxChip = Math.max(...chip, 1);
        const chipPadL = 6;
        const chipPadR = 48;
        const chipPlotW = chipW - chipPadL - chipPadR;
        const chipAnchorClose = points[chipEndAbs].close;
        for (let b = 0; b < bins; b++) {{
          const centerV = minV + (b + 0.5) * step;
          const y0 = pad.top + (plotH * b / bins);
          const y1 = pad.top + (plotH * (b + 1) / bins);
          const bw = chipPlotW * (chip[b] / maxChip);
          const color = centerV >= chipAnchorClose ? 'rgba(220,38,38,0.45)' : 'rgba(22,163,74,0.45)';
          chipCtx.fillStyle = color;
          chipCtx.fillRect(chipPadL, y0, bw, Math.max(1, y1 - y0 - 1));
        }}
        chipCtx.strokeStyle = '#cbd5e1';
        chipCtx.strokeRect(chipPadL, pad.top, chipPlotW, plotH);
        chipCtx.fillStyle = '#64748b';
        chipCtx.font = '12px sans-serif';
        chipCtx.fillText('Chip', chipPadL, pad.top - 6);
        const chipAnchorDate = points[chipEndAbs].trade_date || '';
        chipCtx.fillText(`as of ${{chipAnchorDate}}`, chipPadL + 38, pad.top - 6);
        chipCtx.fillText(fmt(maxV), chipPadL + chipPlotW + 4, pad.top + 9);
        chipCtx.fillText(fmt(minV), chipPadL + chipPlotW + 4, pad.top + plotH);

        // Crosshair
        if (crossIndexAbs !== null && crossIndexAbs >= startAbs && crossIndexAbs <= endIndex) {{
          const localIdx = crossIndexAbs - startAbs;
          const p = points[crossIndexAbs];
          const x = pad.left + localIdx * xStep;
          const y = yOf(p.close);
          ctx.strokeStyle = '#64748b';
          ctx.setLineDash([4, 4]);
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.moveTo(x, pad.top);
          ctx.lineTo(x, pad.top + plotH);
          ctx.moveTo(pad.left, y);
          ctx.lineTo(width - pad.right, y);
          ctx.stroke();
          ctx.setLineDash([]);

          // horizontal cross line on chip panel
          chipCtx.strokeStyle = '#64748b';
          chipCtx.setLineDash([4, 4]);
          chipCtx.lineWidth = 1;
          chipCtx.beginPath();
          chipCtx.moveTo(chipPadL, y);
          chipCtx.lineTo(chipPadL + chipPlotW, y);
          chipCtx.stroke();
          chipCtx.setLineDash([]);

          const cp = Number.isFinite(p.change_percent) ? `${{fmt(p.change_percent)}}%` : '-';
          hoverInfo.innerHTML =
            `Date: <b>${{p.trade_date}}</b> | O: <b>${{fmt(p.open)}}</b> H: <b>${{fmt(p.high)}}</b> L: <b>${{fmt(p.low)}}</b> C: <b>${{fmt(p.close)}}</b> ` +
            `| Chg: <b>${{fmt(p.change_amount)}} (${{cp}})</b> | Vol: <b>${{fmtInt(p.volume)}}</b> | Turnover: <b>${{fmt(p.turnover, 0)}}</b>`;

          const tips = [
            `Date: ${{p.trade_date}}`,
            `Open: ${{fmt(p.open)}}`,
            `High: ${{fmt(p.high)}}`,
            `Low: ${{fmt(p.low)}}`,
            `Close: ${{fmt(p.close)}}`,
            `Change: ${{fmt(p.change_amount)}} (${{cp}})`,
            `Turnover Rate: ${{fmt(p.turnover_rate)}}%`,
            `Volume: ${{fmtInt(p.volume)}}`,
          ];
          tooltip.innerHTML = tips.join('<br>');
          tooltip.style.display = 'block';
        }} else {{
          hoverInfo.textContent = 'Move pointer over chart to inspect daily values.';
          tooltip.style.display = 'none';
        }}
      }};

      const pickIndex = (clientX) => {{
        const rect = canvas.getBoundingClientRect();
        const x = clientX - rect.left;
        const plotW = rect.width - pad.left - pad.right;
        if (x < pad.left || x > rect.width - pad.right) return null;
        const visible = getVisiblePoints();
        const xStep = plotW / Math.max(1, visible.length - 1);
        const localIdx = Math.max(0, Math.min(visible.length - 1, Math.round((x - pad.left) / xStep)));
        return visible[localIdx].__absIdx;
      }};

      canvas.addEventListener('mousemove', (ev) => {{
        crossIndexAbs = pickIndex(ev.clientX);
        const rect = canvas.getBoundingClientRect();
        tooltip.style.left = `${{Math.min(rect.width - 220, Math.max(8, ev.clientX - rect.left + 12))}}px`;
        tooltip.style.top = `${{Math.max(8, ev.clientY - rect.top - 12)}}px`;
        render();
      }});

      canvas.addEventListener('mouseleave', () => {{
        crossIndexAbs = null;
        render();
      }});

      canvas.addEventListener('wheel', (ev) => {{
        ev.preventDefault();
        const anchor = pickIndex(ev.clientX);
        if (ev.deltaY < 0) {{
          applyZoom(visibleCount - 20, anchor);
        }} else {{
          applyZoom(visibleCount + 20, anchor);
        }}
      }}, {{ passive: false }});

      zoomInBtn.addEventListener('click', () => applyZoom(visibleCount - 20, crossIndexAbs));
      zoomOutBtn.addEventListener('click', () => applyZoom(visibleCount + 20, crossIndexAbs));
      zoomResetBtn.addEventListener('click', () => {{
        visibleCount = Math.min(120, points.length);
        endIndex = points.length - 1;
        crossIndexAbs = null;
        setZoomLabel();
        render();
      }});

      window.addEventListener('resize', render);
      setZoomLabel();
      render();
    }})();
  </script>
</body>
</html>"""


def make_handler(db_path: Path, limit: int):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            code = (query.get("code", [""])[0] or "").strip()

            if parsed.path == "/api/quotes":
                rows = fetch_rows(db_path, limit)
                payload = json.dumps(rows, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return

            if parsed.path == "/api/stocks":
                stocks = fetch_stock_list(db_path)
                payload = json.dumps(stocks, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return

            if parsed.path == "/api/daily":
                rows = fetch_daily_rows(db_path, limit, code=code)
                payload = json.dumps(rows, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return

            if parsed.path == "/api/checks":
                checks = fetch_dashboard_checks(db_path)
                payload = json.dumps(checks, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return

            if parsed.path == "/":
                rows = fetch_rows(db_path, limit)
                body = build_html(rows).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if parsed.path == "/checks":
                checks = fetch_dashboard_checks(db_path)
                body = build_checks_html(checks).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if parsed.path == "/daily":
                stocks = fetch_stock_list(db_path)
                selected_code = code or (stocks[0]["code"] if stocks else "")
                rows = fetch_daily_rows(db_path, limit, code=selected_code)
                body = build_daily_html(rows, code=selected_code, stock_list=stocks).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Show SQLite stock snapshots in a web page.")
    parser.add_argument("--db", default="stocks.db", help="Path to SQLite DB (default: stocks.db)")
    parser.add_argument("--host", default="127.0.0.1", help="Host bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Port (default: 8000)")
    parser.add_argument("--limit", type=int, default=200, help="Max rows to show (default: 200)")
    args = parser.parse_args()

    db_path = Path(args.db).expanduser().resolve()
    if not db_path.exists():
        raise FileNotFoundError(f"DB not found: {db_path}")

    server = ThreadingHTTPServer((args.host, args.port), make_handler(db_path, args.limit))
    print(f"Open http://{args.host}:{args.port}/")
    print(f"Using DB: {db_path}")
    server.serve_forever()


if __name__ == "__main__":
    main()
