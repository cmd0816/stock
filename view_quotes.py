#!/usr/bin/env python3
import argparse
from email import policy
from email.parser import BytesParser
import html
import json
import re
import sqlite3
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import parse_qs, quote, urlparse

from xuangu_to_sqlite import import_xlsx_to_sqlite
from weekly_stock.config import load_config
from weekly_stock import db as weekly_db
from weekly_stock.jobs import stock_screen_job


UploadedFiles = Dict[str, Dict[str, Any]]


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


def delete_xuangu_batch(db_path: Path, batch_id: str) -> int:
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        deleted_results = conn.execute(
            "DELETE FROM xuangu_results WHERE batch_id = ?",
            (batch_id,),
        ).rowcount
        deleted_batches = conn.execute(
            "DELETE FROM xuangu_batches WHERE batch_id = ?",
            (batch_id,),
        ).rowcount
        conn.commit()
    return int((deleted_results or 0) + (deleted_batches or 0))


def read_condition_text(base_dir: Path, form_value: str = "") -> str:
    condition_text = form_value.strip()
    if condition_text:
        return condition_text
    condition_file = base_dir / "screening.txt"
    if condition_file.exists():
        return condition_file.read_text(encoding="utf-8").strip()
    return ""


def parse_post_body(content_type: str, body: bytes) -> tuple[Dict[str, List[str]], UploadedFiles]:
    if content_type.lower().startswith("multipart/form-data"):
        msg = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + body
        )
        form: Dict[str, List[str]] = {}
        files: UploadedFiles = {}
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if not name:
                continue
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if filename:
                if payload:
                    files[name] = {
                        "filename": filename,
                        "content": payload,
                    }
                continue
            charset = part.get_content_charset() or "utf-8"
            value = payload.decode(charset, errors="replace")
            form.setdefault(name, []).append(value)
        return form, files

    text = body.decode("utf-8", errors="ignore")
    return parse_qs(text), {}


def form_value(form: Dict[str, List[str]], name: str, default: str = "") -> str:
    return (form.get(name, [default])[0] or default).strip()


def save_uploaded_xlsx(base_dir: Path, upload: Dict[str, Any], batch_id: str) -> Path:
    content = upload.get("content") or b""
    if not content:
        raise ValueError("选择的 XLSX 文件为空。")
    download_dir = base_dir / "downloads"
    download_dir.mkdir(parents=True, exist_ok=True)
    save_name = f"uploaded_xuangu_{batch_id}.xlsx"
    path = download_dir / save_name
    path.write_bytes(content)
    return path


def fetch_dashboard_checks(db_path: Path, batch_id: str = "") -> Dict[str, Any]:
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
            selected_batch_id = batch_id
            if not selected_batch_id:
                row = conn.execute(
                    """
                    SELECT batch_id FROM xuangu_batches
                    ORDER BY imported_at_utc DESC
                    LIMIT 1
                    """
                ).fetchone()
                selected_batch_id = str(row["batch_id"]) if row else ""
            out["selected_xuangu_batch_id"] = selected_batch_id
            out["latest_xuangu_results"] = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT row_no, stock_code, stock_name, row_json
                    FROM xuangu_results
                    WHERE batch_id = ?
                    ORDER BY row_no
                    LIMIT 1000
                    """,
                    (selected_batch_id,),
                ).fetchall()
            ]
        else:
            out["selected_xuangu_batch_id"] = ""
            out["latest_xuangu_results"] = []

        if table_exists(conn, "weekly_selected_stocks"):
            out["weekly_selected"] = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT run_id, screen_date, code, name, rank_no, total_score, selected_reason
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
  <title>东方财富行情快照</title>
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
      <h2>东方财富行情快照</h2>
      <div>
        <a href="/daily">日 K 线</a>
        <a href="/screening">选股页面</a>
        <a href="/api/quotes">JSON 接口</a>
      </div>
    </div>
    <div class="meta">自动刷新：20 秒 | 生成时间：{html.escape(now)} | 行数：{len(rows)}</div>
    <div class="card">
      <table>
        <thead>
          <tr>
            <th>ID</th><th>代码</th><th>名称</th><th>证券ID</th><th>现价</th><th>开盘</th>
            <th>最高</th><th>最低</th><th>昨收</th><th>涨跌幅</th><th>成交量</th>
            <th>成交额</th><th>抓取时间 UTC</th>
          </tr>
        </thead>
        <tbody>
          {''.join(row_html) if row_html else '<tr><td colspan="13">暂无数据。</td></tr>'}
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


def render_weekly_selected_table(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "<div class='empty'>还没有生成周末入选股票。</div>"
    columns = [
        ("run_id", "运行ID"),
        ("screen_date", "选股日期"),
        ("rank_no", "排名"),
        ("code", "代码"),
        ("name", "名称"),
        ("total_score", "总分"),
        ("selected_reason", "入选原因"),
    ]
    head = "".join(f"<th>{html.escape(label)}</th>" for _, label in columns)
    body = []
    for row in rows:
        cells = []
        code = str(row.get("code") or "")
        daily_url = f"/daily?code={quote(code)}" if code else "#"
        for key, _ in columns:
            value = row.get(key)
            if isinstance(value, float):
                text = f"{value:.2f}"
            elif value is None:
                text = "-"
            else:
                text = str(value)
            if key in {"code", "name"} and code:
                cell = f"<a class='daily-link' href='{daily_url}' title='查看日 K 线'>{html.escape(text)}</a>"
            else:
                cell = html.escape(text)
            cells.append(f"<td>{cell}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f"""
      <div class="table-wrap">
        <table>
          <thead><tr>{head}</tr></thead>
          <tbody>{''.join(body)}</tbody>
        </table>
      </div>
    """


def re_like_number(value: str) -> bool:
    text = value.strip().replace(",", "")
    if not text:
        return False
    return re.fullmatch(r"-?\d+(?:\.\d+)?%?", text) is not None


def render_xuangu_batch_table(rows: List[Dict[str, Any]], selected_batch_id: str) -> str:
    if not rows:
        return "<div class='empty'>还没有条件选股批次。</div>"
    body = []
    for row in rows:
        batch_id = str(row.get("batch_id") or "")
        view_url = f"/screening?batch_id={quote(batch_id)}"
        view_label = "当前批次" if batch_id == selected_batch_id else "查看股票"
        body.append(
            "<tr>"
            f"<td>{html.escape(batch_id)}</td>"
            f"<td>{html.escape(str(row.get('imported_at_utc') or '-'))}</td>"
            f"<td>{html.escape(str(row.get('row_count') or 0))}</td>"
            f"<td>{html.escape(str(row.get('xlsx_path') or '-'))}</td>"
            "<td class='actions'>"
            f"<a href='{view_url}'>{view_label}</a>"
            f"<form method='post' action='/actions/delete_xuangu_batch' onsubmit=\"return confirm('确认删除批次 {html.escape(batch_id)} 及其导入股票吗？');\">"
            f"<input type='hidden' name='batch_id' value='{html.escape(batch_id)}' />"
            "<button type='submit'>删除</button>"
            "</form>"
            "</td>"
            "</tr>"
        )
    return f"""
      <div class="table-wrap">
        <table>
          <thead>
            <tr><th>批次</th><th>导入时间 UTC</th><th>行数</th><th>文件</th><th>操作</th></tr>
          </thead>
          <tbody>{''.join(body)}</tbody>
        </table>
      </div>
    """


def render_xuangu_import_form(message: str = "", error: str = "") -> str:
    msg_html = f"<div class='notice ok'>{html.escape(message)}</div>" if message else ""
    err_html = f"<div class='notice error'>{html.escape(error)}</div>" if error else ""
    return f"""
      <div class="import-box">
        {msg_html}
        {err_html}
        <form method="post" action="/actions/import_xuangu_xlsx" enctype="multipart/form-data">
          <div class="form-grid">
            <label>
              <span>选择 XLSX 文件</span>
              <input class="file-input" type="file" name="xlsx_file" accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" required />
            </label>
            <label>
              <span>批次号</span>
              <input name="batch_id" placeholder="留空使用今天 YYYYMMDD" />
            </label>
            <label>
              <span>来源 URL</span>
              <input name="source_url" value="https://xuangu.eastmoney.com/" />
            </label>
          </div>
          <label class="textarea-label">
            <span>选股条件</span>
            <textarea name="condition_text" rows="3" placeholder="留空时自动读取 screening.txt"></textarea>
          </label>
          <div class="form-actions">
            <label class="checkline">
              <input type="checkbox" name="replace_existing" value="1" checked />
              <span>如果同批次已存在，先删除旧导入再重新导入</span>
            </label>
            <button type="submit">导入 XLSX</button>
          </div>
        </form>
      </div>
    """


def render_weekly_screen_form(selected_batch_id: str) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    return f"""
      <div class="import-box compact">
        <form method="post" action="/actions/run_weekly_screen">
          <div class="form-grid weekly-form-grid">
            <label>
              <span>选股批次</span>
              <input name="xuangu_batch_id" value="{html.escape(selected_batch_id)}" placeholder="例如 20260508" required />
            </label>
            <label>
              <span>选股日期</span>
              <input name="screen_date" value="{html.escape(today)}" placeholder="YYYY-MM-DD" />
            </label>
            <label>
              <span>Top N</span>
              <input name="top_n" type="number" min="1" max="20" value="5" />
            </label>
          </div>
          <div class="form-actions">
            <span class="form-hint">根据当前批次候选股和已下载 K 线打分排序。</span>
            <button type="submit">生成下周 Top 股票</button>
          </div>
        </form>
        <form method="post" action="/actions/clear_weekly_top" onsubmit="return confirm('确认清空 Top 股票列表和关联复盘结果吗？');">
          <div class="form-actions clear-actions">
            <span class="form-hint">清空当前所有 Top 股票、候选打分和关联复盘结果。</span>
            <button class="danger-btn" type="submit">清空 Top 列表</button>
          </div>
        </form>
      </div>
    """


def render_xuangu_result_table(rows: List[Dict[str, Any]], selected_batch_id: str) -> str:
    if not rows:
        return f"<div class='empty'>批次 {html.escape(selected_batch_id or '-')} 没有导入行。</div>"

    parsed_rows: List[Dict[str, Any]] = []
    headers: List[str] = []
    recognized = 0
    for row in rows:
        raw_text = str(row.get("row_json") or "{}")
        try:
            raw = json.loads(raw_text)
        except Exception:
            raw = {"原始数据": raw_text}
        if row.get("stock_code"):
            recognized += 1
        if not isinstance(raw, dict):
            raw = {"原始数据": raw}
        for key in raw.keys():
            key_text = str(key)
            if key_text not in headers:
                headers.append(key_text)
        parsed_rows.append(raw)

    warning = ""
    if recognized == 0:
        warning = (
            "<div class='notice'>这个批次有导入行，但没有识别到股票代码。"
            "通常是旧批次在修复前导入，或者东方财富导出的字段结构变了。"
            "请用上面的导入功能勾选覆盖后重新导入 XLSX。</div>"
        )

    if not headers:
        return warning + f"<div class='empty'>批次 {html.escape(selected_batch_id or '-')} 没有可显示字段。</div>"

    def cell_class(header: str, value: Any) -> str:
        if header in {"代码", "股票代码", "证券代码", "名称", "股票名称", "证券简称", "股票简称", "上市板块"}:
            return "txt"
        if isinstance(value, (int, float)):
            return "num"
        if isinstance(value, str) and re_like_number(value):
            return "num"
        return "txt"

    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body = []
    for row in parsed_rows:
        cells = []
        for header in headers:
            value = row.get(header)
            text = "-" if value is None or value == "" else str(value)
            cls = cell_class(header, value)
            cells.append(f"<td class='{cls}'>{html.escape(text)}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")

    table_min_width = max(980, len(headers) * 138)
    table = f"""
      <div class="xlsx-summary">
        显示 {len(parsed_rows)} 行，识别到股票代码 {recognized} 行。
      </div>
      <div class="table-wrap xlsx-wrap">
        <table class="xlsx-table" style="min-width:{table_min_width}px">
          <thead><tr>{head}</tr></thead>
          <tbody>{''.join(body)}</tbody>
        </table>
      </div>
    """
    return warning + table


def build_checks_html(checks: Dict[str, Any], message: str = "", error: str = "") -> str:
    selected_batch_id = str(checks.get("selected_xuangu_batch_id") or "")
    batches = render_xuangu_batch_table(checks.get("xuangu_batches", []), selected_batch_id)
    import_form = render_xuangu_import_form(message, error)
    weekly_screen_form = render_weekly_screen_form(selected_batch_id)
    latest_results = render_xuangu_result_table(checks.get("latest_xuangu_results", []), selected_batch_id)
    weekly_selected = render_weekly_selected_table(checks.get("weekly_selected", []))
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
  <title>选股页面</title>
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
    .xlsx-summary {{
      padding: 9px 12px;
      color: #475569;
      font-size: 13px;
      border-bottom: 1px solid var(--line);
      background: #fff;
    }}
    .xlsx-wrap {{
      max-height: 560px;
      border-top: 0;
    }}
    .xlsx-table {{
      width: max-content;
      border-collapse: separate;
      border-spacing: 0;
      font-size: 12px;
      background: #fff;
    }}
    .xlsx-table th,
    .xlsx-table td {{
      border-right: 1px solid var(--line);
      border-bottom: 1px solid var(--line);
      padding: 5px 8px;
      white-space: nowrap;
      line-height: 1.25;
    }}
    .xlsx-table th {{
      position: sticky;
      top: 0;
      z-index: 1;
      color: #111827;
      background: #f8fafc;
      font-weight: 800;
      text-align: center;
    }}
    .xlsx-table td.num {{
      text-align: right;
      font-variant-numeric: tabular-nums;
    }}
    .xlsx-table td.txt {{
      text-align: left;
    }}
    .xlsx-table tbody tr:nth-child(even) {{
      background: #fafafa;
    }}
    .xlsx-table tbody tr:hover {{
      background: #fff7ed;
    }}
    .actions {{
      display: flex;
      align-items: center;
      gap: 10px;
      white-space: nowrap;
    }}
    .actions form {{
      margin: 0;
    }}
    .actions button {{
      border: 1px solid #fecaca;
      background: #fff5f5;
      color: #b91c1c;
      border-radius: 6px;
      padding: 4px 8px;
      cursor: pointer;
    }}
    .actions button:hover {{
      background: #fee2e2;
    }}
    .import-box {{
      padding: 14px;
    }}
    .import-box.compact {{
      border-bottom: 1px solid var(--line);
      background: #fff;
    }}
    .form-grid {{
      display: grid;
      grid-template-columns: 1.4fr 1fr 1.4fr;
      gap: 12px;
      margin-bottom: 12px;
    }}
    .weekly-form-grid {{
      grid-template-columns: 1fr 1fr 0.55fr;
    }}
    label span {{
      display: block;
      color: #475569;
      font-size: 12px;
      font-weight: 700;
      margin-bottom: 5px;
    }}
    input, textarea {{
      width: 100%;
      box-sizing: border-box;
      border: 1px solid #cbd5e1;
      border-radius: 8px;
      color: #111827;
      background: #fff;
      font: inherit;
      font-size: 13px;
      padding: 8px 10px;
    }}
    .file-input {{
      padding: 6px 8px;
      background: #f8fafc;
    }}
    textarea {{
      resize: vertical;
    }}
    .textarea-label {{
      display: block;
      margin-bottom: 10px;
    }}
    .form-actions {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
    }}
    .checkline {{
      display: flex;
      align-items: center;
      gap: 8px;
      color: #475569;
      font-size: 13px;
    }}
    .checkline input {{
      width: auto;
    }}
    .checkline span {{
      display: inline;
      margin: 0;
      font-size: 13px;
      font-weight: 500;
    }}
    .form-actions button {{
      border: 1px solid #fb923c;
      background: #f97316;
      color: white;
      border-radius: 8px;
      padding: 8px 14px;
      cursor: pointer;
      font-weight: 700;
    }}
    .form-actions button:hover {{
      background: #ea580c;
    }}
    .clear-actions {{
      margin-top: 10px;
      padding-top: 10px;
      border-top: 1px dashed var(--line);
    }}
    .form-actions button.danger-btn {{
      border-color: #fecaca;
      background: #fff5f5;
      color: #b91c1c;
    }}
    .form-actions button.danger-btn:hover {{
      background: #fee2e2;
    }}
    .form-hint {{
      color: var(--muted);
      font-size: 13px;
    }}
    .daily-link {{
      color: #2563eb;
      font-weight: 700;
      text-decoration: none;
    }}
    .daily-link:hover {{
      color: #ea580c;
      text-decoration: underline;
    }}
    .candidate-details {{
      border-top: 1px solid var(--line);
      background: #fff;
    }}
    .candidate-details summary {{
      cursor: pointer;
      padding: 10px 14px;
      color: #334155;
      font-weight: 700;
      user-select: none;
    }}
    .empty {{
      padding: 16px;
      color: var(--muted);
      font-size: 13px;
    }}
    .notice {{
      margin: 12px;
      padding: 10px 12px;
      border: 1px solid #fde68a;
      background: #fffbeb;
      color: #92400e;
      border-radius: 8px;
      font-size: 13px;
    }}
    .import-box .notice {{
      margin: 0 0 12px;
    }}
    .notice.ok {{
      border-color: #bbf7d0;
      background: #f0fdf4;
      color: #166534;
    }}
    .notice.error {{
      border-color: #fecaca;
      background: #fef2f2;
      color: #991b1b;
    }}
    @media (max-width: 900px) {{
      .form-grid {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="head">
      <h2>选股页面</h2>
      <div>
        <a href="/">行情快照</a>
        <a href="/daily">日 K 线</a>
        <a href="/api/screening">JSON 接口</a>
      </div>
    </div>
    <div class="grid">
      <section class="card"><h3>导入条件选股 XLSX</h3>{import_form}</section>
      <section class="card"><h3>最近条件选股批次</h3>{batches}</section>
      <section class="card"><h3>周末入选股票（批次 {html.escape(selected_batch_id or '-')}）</h3>{weekly_screen_form}{weekly_selected}<details class="candidate-details"><summary>查看本批次候选股票列表</summary>{latest_results}</details></section>
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
    selected_name_raw = str(rows[0].get("name") or "").strip() if rows else ""
    if not selected_name_raw and code:
        selected_stock = next((s for s in stock_list if str(s.get("code") or "") == code), None)
        selected_name_raw = str((selected_stock or {}).get("name") or "").strip()
    selected_label = " ".join(part for part in (selected_name_raw, code) if part)
    title_suffix = f" - {selected_label}" if selected_label else ""
    selected_name = html.escape(selected_name_raw or "-")
    selected_code = html.escape(code or "-")
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta http-equiv="refresh" content="30" />
  <title>东方财富日 K 线{html.escape(title_suffix)}</title>
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
        <h3>股票列表</h3>
        <div class="stocks">
          {''.join(stock_links) if stock_links else "<div style='padding:12px;color:#6b7280'>No stocks found.</div>"}
        </div>
      </aside>
      <main class="main">
        <div class="head">
          <h2>东方财富日 K 线{html.escape(title_suffix)}</h2>
          <div>
            <a href="/">行情快照</a>
            <a href="/screening">选股页面</a>
            <a href="/api/daily{('?code=' + html.escape(code)) if code else ''}">JSON 接口</a>
            <a href="/api/stocks">股票列表接口</a>
          </div>
        </div>
        <div class="meta">
          自动刷新：30 秒 | 生成时间：{html.escape(now)} | 行数：{len(rows)} | 当前股票：{selected_name}（{selected_code}）
        </div>
        <div class="card">
          <div class="hover-row">
            <div id="hoverInfo" class="hover-info">移动鼠标到图表上查看当日明细。</div>
            <div class="chart-controls">
              <button id="zoomInBtn" type="button">放大</button>
              <button id="zoomOutBtn" type="button">缩小</button>
              <button id="zoomResetBtn" type="button">重置</button>
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
            <span><b style="color:#dc2626">阳线</b></span>
            <span><b style="color:#16a34a">阴线</b></span>
            <span><b style="color:#f59e0b">MA5</b></span>
            <span><b style="color:#3b82f6">MA10</b></span>
            <span><b style="color:#ef4444">MA20</b></span>
            <span><b style="color:#14b8a6">MA30</b></span>
            <span><b style="color:#8b5cf6">MA60</b></span>
            <span><b style="color:#0f766e">MA120</b></span>
            <span><b style="color:#374151">MA250</b></span>
            <span><b style="color:#0369a1">筹码图</b>（红=下方获利/支撑，绿=上方套牢/压力，估算）</span>
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
        if (hoverInfo) hoverInfo.textContent = '暂无日 K 数据。';
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
        hoverInfo.textContent = '暂无有效 K 线数据。';
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
          // b=0 is the lowest price bin, so draw it at the bottom of the panel.
          const y0 = pad.top + (plotH * (bins - b - 1) / bins);
          const y1 = pad.top + (plotH * (bins - b) / bins);
          const bw = chipPlotW * (chip[b] / maxChip);
          const color = centerV >= chipAnchorClose ? 'rgba(22,163,74,0.45)' : 'rgba(220,38,38,0.45)';
          chipCtx.fillStyle = color;
          chipCtx.fillRect(chipPadL, y0, bw, Math.max(1, y1 - y0 - 1));
        }}
        chipCtx.strokeStyle = '#cbd5e1';
        chipCtx.strokeRect(chipPadL, pad.top, chipPlotW, plotH);
        chipCtx.fillStyle = '#64748b';
        chipCtx.font = '12px sans-serif';
        chipCtx.fillText('筹码', chipPadL, pad.top - 6);
        const chipAnchorDate = points[chipEndAbs].trade_date || '';
        chipCtx.fillText(`截至 ${{chipAnchorDate}}`, chipPadL + 38, pad.top - 6);
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
            `日期：<b>${{p.trade_date}}</b> | 开：<b>${{fmt(p.open)}}</b> 高：<b>${{fmt(p.high)}}</b> 低：<b>${{fmt(p.low)}}</b> 收：<b>${{fmt(p.close)}}</b> ` +
            `| 涨跌：<b>${{fmt(p.change_amount)}} (${{cp}})</b> | 成交量：<b>${{fmtInt(p.volume)}}</b> | 成交额：<b>${{fmt(p.turnover, 0)}}</b>`;

          const tips = [
            `日期：${{p.trade_date}}`,
            `开盘：${{fmt(p.open)}}`,
            `最高：${{fmt(p.high)}}`,
            `最低：${{fmt(p.low)}}`,
            `收盘：${{fmt(p.close)}}`,
            `涨跌：${{fmt(p.change_amount)}} (${{cp}})`,
            `换手率：${{fmt(p.turnover_rate)}}%`,
            `成交量：${{fmtInt(p.volume)}}`,
          ];
          tooltip.innerHTML = tips.join('<br>');
          tooltip.style.display = 'block';
        }} else {{
          hoverInfo.textContent = '移动鼠标到图表上查看当日明细。';
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
        def do_POST(self):
            parsed = urlparse(self.path)
            content_length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(content_length)
            form, files = parse_post_body(self.headers.get("Content-Type", ""), body)

            if parsed.path == "/actions/delete_xuangu_batch":
                batch_id = form_value(form, "batch_id")
                if batch_id:
                    delete_xuangu_batch(db_path, batch_id)
                self.send_response(303)
                self.send_header("Location", "/screening")
                self.end_headers()
                return

            if parsed.path == "/actions/import_xuangu_xlsx":
                try:
                    base_dir = db_path.parent
                    upload = files.get("xlsx_file")
                    if not upload:
                        raise ValueError("请选择 XLSX 文件。")
                    batch_id_arg = form_value(form, "batch_id") or datetime.now().strftime("%Y%m%d")
                    xlsx_path = save_uploaded_xlsx(base_dir, upload, batch_id_arg)
                    if not xlsx_path.exists():
                        raise FileNotFoundError(f"找不到 XLSX 文件：{xlsx_path}")
                    if xlsx_path.suffix.lower() != ".xlsx":
                        raise ValueError(f"文件不是 .xlsx：{xlsx_path}")

                    condition_text = read_condition_text(
                        base_dir,
                        form_value(form, "condition_text"),
                    )
                    source_url = (
                        form_value(form, "source_url") or "https://xuangu.eastmoney.com/"
                    ).strip()
                    replace_existing = form_value(form, "replace_existing") == "1"
                    batch_id, sheet_count, row_count = import_xlsx_to_sqlite(
                        db_path=db_path,
                        xlsx_path=xlsx_path,
                        source_url=source_url,
                        condition_text=condition_text,
                        batch_id=batch_id_arg,
                        replace_existing=replace_existing,
                    )
                    msg = quote(f"导入成功：批次 {batch_id}，sheet {sheet_count}，股票行 {row_count}")
                    self.send_response(303)
                    self.send_header("Location", f"/screening?batch_id={quote(batch_id)}&msg={msg}")
                    self.end_headers()
                    return
                except Exception as exc:
                    self.send_response(303)
                    self.send_header("Location", f"/screening?err={quote(str(exc))}")
                    self.end_headers()
                    return

            if parsed.path == "/actions/run_weekly_screen":
                try:
                    base_dir = db_path.parent
                    config_path = base_dir / "config/weekly_strategy.yaml"
                    config = load_config(config_path)
                    config["database"]["path"] = str(db_path)
                    config.setdefault("screening", {})
                    config["screening"]["run_xuangu"] = False
                    top_n = int(form_value(form, "top_n", "5") or "5")
                    config["screening"]["top_n"] = max(1, min(20, top_n))
                    xuangu_batch_id = form_value(form, "xuangu_batch_id")
                    if not xuangu_batch_id:
                        raise ValueError("请先选择或填写选股批次。")
                    screen_date = form_value(form, "screen_date") or datetime.now().strftime("%Y-%m-%d")
                    run_id = stock_screen_job(
                        config_path=config_path,
                        config=config,
                        screen_date=screen_date,
                        xuangu_batch_id=xuangu_batch_id,
                        replace_existing=True,
                    )
                    msg = quote(f"已生成下周 Top {config['screening']['top_n']} 股票：run_id={run_id}")
                    self.send_response(303)
                    self.send_header("Location", f"/screening?batch_id={quote(xuangu_batch_id)}&msg={msg}")
                    self.end_headers()
                    return
                except Exception as exc:
                    self.send_response(303)
                    self.send_header("Location", f"/screening?err={quote(str(exc))}")
                    self.end_headers()
                    return

            if parsed.path == "/actions/clear_weekly_top":
                try:
                    with weekly_db.connect(db_path) as conn:
                        weekly_db.ensure_weekly_tables(conn)
                        deleted = weekly_db.delete_all_screen_runs(conn)
                    msg = quote(f"已清空 Top 列表：删除 {deleted} 次选股运行")
                    self.send_response(303)
                    self.send_header("Location", f"/screening?msg={msg}")
                    self.end_headers()
                    return
                except Exception as exc:
                    self.send_response(303)
                    self.send_header("Location", f"/screening?err={quote(str(exc))}")
                    self.end_headers()
                    return

            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")

        def do_GET(self):
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            code = (query.get("code", [""])[0] or "").strip()
            selected_batch_id = (query.get("batch_id", [""])[0] or "").strip()
            message = (query.get("msg", [""])[0] or "").strip()
            error = (query.get("err", [""])[0] or "").strip()

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

            if parsed.path in {"/api/checks", "/api/screening"}:
                checks = fetch_dashboard_checks(db_path, batch_id=selected_batch_id)
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

            if parsed.path in {"/checks", "/screening"}:
                checks = fetch_dashboard_checks(db_path, batch_id=selected_batch_id)
                body = build_checks_html(checks, message=message, error=error).encode("utf-8")
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
    print(f"打开 http://{args.host}:{args.port}/")
    print(f"使用数据库：{db_path}")
    server.serve_forever()


if __name__ == "__main__":
    main()
