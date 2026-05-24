#!/usr/bin/env python3
import argparse
from email import policy
from email.parser import BytesParser
import html
import json
import re
import sqlite3
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import parse_qs, quote, urlparse

from xuangu_to_sqlite import import_xlsx_to_sqlite
from weekly_stock.config import load_config
from weekly_stock import db as weekly_db
from weekly_stock.jobs import ml_predict_job, stock_screen_job
from weekly_stock.trading_calendar import align_to_last_trading_day


UploadedFiles = Dict[str, Dict[str, Any]]
TOP_RESULT_CACHE_TTL_SECONDS = 6 * 60 * 60
_top_result_cache: Dict[str, Dict[str, Any]] = {}


def sqlite_connect_with_retry(db_path: Path, retries: int = 3, sleep_seconds: float = 0.2) -> sqlite3.Connection:
    last_error: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            conn = sqlite3.connect(str(db_path), timeout=12, check_same_thread=False)
            conn.execute("PRAGMA busy_timeout = 12000")
            conn.execute("PRAGMA journal_mode = WAL")
            return conn
        except sqlite3.OperationalError as exc:
            last_error = exc
            if attempt >= retries - 1:
                raise
            time.sleep(sleep_seconds * (attempt + 1))
    if last_error:
        raise last_error
    raise sqlite3.OperationalError(f"failed to open sqlite db: {db_path}")


def fetch_rows(db_path: Path, limit: int) -> List[Dict[str, Any]]:
    try:
        with sqlite_connect_with_retry(db_path) as conn:
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
    except sqlite3.OperationalError:
        return []


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

    try:
        with sqlite_connect_with_retry(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def fetch_latest_daily_rows_by_codes(db_path: Path, codes: List[str]) -> Dict[str, Dict[str, Any]]:
    clean_codes = [str(c).strip() for c in codes if str(c).strip()]
    if not clean_codes:
        return {}
    placeholders = ",".join("?" for _ in clean_codes)
    sql = f"""
        WITH ranked AS (
            SELECT
                id, code, name, secid, trade_date, open, close, high, low,
                volume, turnover, amplitude_percent, change_percent,
                change_amount, turnover_rate, fetched_at_utc,
                ROW_NUMBER() OVER (PARTITION BY code ORDER BY trade_date DESC, id DESC) AS rn
            FROM eastmoney_stock_daily_klines
            WHERE code IN ({placeholders})
        )
        SELECT
            id, code, name, secid, trade_date, open, close, high, low,
            volume, turnover, amplitude_percent, change_percent,
            change_amount, turnover_rate, fetched_at_utc
        FROM ranked
        WHERE rn = 1
    """
    try:
        with sqlite_connect_with_retry(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, clean_codes).fetchall()
        return {str(r["code"]): dict(r) for r in rows if r["code"]}
    except sqlite3.OperationalError:
        return {}


def fetch_stock_list(db_path: Path) -> List[Dict[str, Any]]:
    try:
        with sqlite_connect_with_retry(db_path) as conn:
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
            ORDER BY
                CASE WHEN k.change_percent IS NULL THEN 1 ELSE 0 END,
                k.change_percent DESC,
                l.code
            """
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def delete_xuangu_batch(db_path: Path, batch_id: str) -> int:
    with sqlite_connect_with_retry(db_path) as conn:
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


def normalize_top_sort_mode(value: str) -> str:
    mode = (value or "").strip().lower()
    return "rule" if mode == "rule" else "ml"


def cache_top_result(sort_mode: str, run: Dict[str, Any], rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    mode = normalize_top_sort_mode(sort_mode)
    _top_result_cache[mode] = {
        "cached_at": time.time(),
        "run": dict(run or {}),
        "rows": [dict(r) for r in rows],
    }


def get_cached_top_result(sort_mode: str) -> tuple[Dict[str, Any], List[Dict[str, Any]]] | None:
    mode = normalize_top_sort_mode(sort_mode)
    item = _top_result_cache.get(mode)
    if not item:
        return None
    cached_at = float(item.get("cached_at") or 0)
    if cached_at <= 0 or (time.time() - cached_at) > TOP_RESULT_CACHE_TTL_SECONDS:
        _top_result_cache.pop(mode, None)
        return None
    run = dict(item.get("run") or {})
    rows = [dict(r) for r in (item.get("rows") or [])]
    if not rows:
        return None
    return run, rows


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
    config_path = db_path.parent / "config/weekly_strategy.yaml"
    try:
        cfg = load_config(config_path)
        screening_top_n = int(cfg.get("screening", {}).get("top_n", 5))
    except Exception:
        screening_top_n = 5
    screening_top_n = max(1, min(50, screening_top_n))

    try:
        with sqlite_connect_with_retry(db_path) as conn:
            conn.row_factory = sqlite3.Row
            out: Dict[str, Any] = {"screening_top_n": screening_top_n}

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
                            CASE
                                WHEN review_start_date IS NULL OR notes LIKE 'K线不足%' THEN '待复盘'
                                WHEN meets_expectation = 1 THEN '成功'
                                ELSE '失败'
                            END AS result,
                            code, name, highest_gain_pct, close_gain_pct, max_drawdown_pct,
                            CASE WHEN stop_loss_triggered = 1 THEN '是' ELSE '否' END AS stop_loss_triggered,
                            CASE WHEN meets_expectation = 1 THEN '是' ELSE '否' END AS meets_expectation,
                            notes
                        FROM weekly_review_results
                        ORDER BY id DESC
                        LIMIT 20
                        """
                    ).fetchall()
                ]
                latest_review_row = conn.execute(
                    """
                    SELECT review_id
                    FROM weekly_review_runs
                    ORDER BY review_id DESC
                    LIMIT 1
                    """
                ).fetchone()
                if latest_review_row is not None:
                    latest_review_id = int(latest_review_row["review_id"] if isinstance(latest_review_row, sqlite3.Row) else latest_review_row[0])
                    summary_row = conn.execute(
                        """
                        SELECT
                            COUNT(*) AS total_count,
                            SUM(CASE WHEN review_start_date IS NULL OR notes LIKE 'K线不足%' THEN 1 ELSE 0 END) AS pending_count,
                            SUM(CASE WHEN review_start_date IS NOT NULL AND notes NOT LIKE 'K线不足%' THEN 1 ELSE 0 END) AS reviewed_count,
                            SUM(CASE WHEN review_start_date IS NOT NULL AND notes NOT LIKE 'K线不足%' AND meets_expectation = 1 THEN 1 ELSE 0 END) AS success_count
                        FROM weekly_review_results
                        WHERE review_id = ?
                        """,
                        (latest_review_id,),
                    ).fetchone()
                    total_count = int((summary_row["total_count"] if isinstance(summary_row, sqlite3.Row) else summary_row[0]) or 0)
                    pending_count = int((summary_row["pending_count"] if isinstance(summary_row, sqlite3.Row) else summary_row[1]) or 0)
                    reviewed_count = int((summary_row["reviewed_count"] if isinstance(summary_row, sqlite3.Row) else summary_row[2]) or 0)
                    success_count = int((summary_row["success_count"] if isinstance(summary_row, sqlite3.Row) else summary_row[3]) or 0)
                    success_rate_pct = (success_count / reviewed_count * 100.0) if reviewed_count else 0.0
                    out["weekly_review_summary"] = {
                        "review_id": latest_review_id,
                        "total_count": total_count,
                        "pending_count": pending_count,
                        "reviewed_count": reviewed_count,
                        "success_count": success_count,
                        "success_rate_pct": success_rate_pct,
                    }
                else:
                    out["weekly_review_summary"] = None
            else:
                out["weekly_reviews"] = []
                out["weekly_review_summary"] = None

            return out
    except sqlite3.OperationalError:
        return {
            "screening_top_n": screening_top_n,
            "selected_xuangu_batch_id": "",
            "latest_xuangu_results": [],
            "weekly_selected": [],
            "weekly_reviews": [],
            "weekly_review_summary": None,
            "xuangu_batches": [],
            "kline_coverage": [],
        }


def fetch_top_selected_stocks(
    db_path: Path,
    run_id: int | None = None,
    sort_mode: str = "ml",
) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    sort_mode = normalize_top_sort_mode(sort_mode)
    for attempt in range(3):
        try:
            with sqlite_connect_with_retry(db_path) as conn:
                conn.row_factory = sqlite3.Row
                empty_run: Dict[str, Any] = {}
                if not table_exists(conn, "weekly_screen_runs") or not table_exists(conn, "weekly_selected_stocks"):
                    return empty_run, []

                run_row = None
                if run_id is not None:
                    run_row = conn.execute(
                        """
                        SELECT run_id, screen_date, xuangu_batch_id, selected_count, created_at_utc
                        FROM weekly_screen_runs
                        WHERE run_id = ?
                        """,
                        (run_id,),
                    ).fetchone()

                if run_row is None:
                    run_row = conn.execute(
                        """
                        SELECT
                            r.run_id,
                            r.screen_date,
                            r.xuangu_batch_id,
                            r.selected_count,
                            r.created_at_utc
                        FROM weekly_screen_runs r
                        WHERE EXISTS (
                            SELECT 1 FROM weekly_selected_stocks s WHERE s.run_id = r.run_id
                        )
                        ORDER BY r.screen_date DESC, r.run_id DESC
                        LIMIT 1
                        """
                    ).fetchone()

                if run_row is None:
                    return empty_run, []

                if table_exists(conn, "weekly_ml_predictions"):
                    order_by_sql = """
                            CASE WHEN p.predicted_score IS NULL THEN 1 ELSE 0 END,
                            p.predicted_score DESC,
                            s.rank_no,
                            s.id
                    """
                    if sort_mode == "rule":
                        order_by_sql = "s.rank_no, s.id"
                    rows = conn.execute(
                        """
                        SELECT
                            s.id, s.run_id, s.screen_date, s.code, s.name, s.rank_no,
                            s.total_score, s.selected_reason, s.status, s.created_at_utc,
                            p.probability_up AS ml_probability_up,
                            p.predicted_score AS ml_predicted_score,
                            p.reason AS ml_reason
                        FROM weekly_selected_stocks s
                        LEFT JOIN weekly_ml_predictions p
                            ON p.source_run_id = s.run_id AND p.code = s.code
                        WHERE s.run_id = ?
                        ORDER BY
                        """
                        + order_by_sql
                        + """
                        """,
                        (int(run_row["run_id"]),),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT
                            id, run_id, screen_date, code, name, rank_no,
                            total_score, selected_reason, status, created_at_utc
                        FROM weekly_selected_stocks
                        WHERE run_id = ?
                        ORDER BY rank_no, id
                        """,
                        (int(run_row["run_id"]),),
                    ).fetchall()
                return dict(run_row), [dict(r) for r in rows]
        except sqlite3.OperationalError:
            if attempt >= 2:
                return {}, []
            time.sleep(0.15 * (attempt + 1))
    return {}, []


def fetch_weekly_review_history(
    db_path: Path,
    *,
    review_id: int | None = None,
    limit: int = 200,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "runs": [],
        "selected_review_id": None,
        "selected_summary": None,
        "selected_results": [],
    }
    try:
        with sqlite_connect_with_retry(db_path) as conn:
            conn.row_factory = sqlite3.Row
            if not table_exists(conn, "weekly_review_runs") or not table_exists(conn, "weekly_review_results"):
                return out

        rows = conn.execute(
            """
            SELECT
                wr.review_id,
                wr.reviewed_run_id,
                wr.review_date,
                wr.created_at_utc,
                r.screen_date,
                r.xuangu_batch_id,
                COUNT(rr.id) AS total_count,
                SUM(CASE WHEN rr.review_start_date IS NULL OR rr.notes LIKE 'K线不足%' THEN 1 ELSE 0 END) AS pending_count,
                SUM(CASE WHEN rr.review_start_date IS NOT NULL AND rr.notes NOT LIKE 'K线不足%' THEN 1 ELSE 0 END) AS reviewed_count,
                SUM(CASE WHEN rr.review_start_date IS NOT NULL AND rr.notes NOT LIKE 'K线不足%' AND rr.meets_expectation = 1 THEN 1 ELSE 0 END) AS success_count
            FROM weekly_review_runs wr
            LEFT JOIN weekly_review_results rr
                ON rr.review_id = wr.review_id
            LEFT JOIN weekly_screen_runs r
                ON r.run_id = wr.reviewed_run_id
            GROUP BY wr.review_id
            ORDER BY wr.review_id DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()

        runs: List[Dict[str, Any]] = []
        for row in rows:
            total_count = int((row["total_count"] or 0))
            pending_count = int((row["pending_count"] or 0))
            reviewed_count = int((row["reviewed_count"] or 0))
            success_count = int((row["success_count"] or 0))
            success_rate_pct = (success_count / reviewed_count * 100.0) if reviewed_count else 0.0
            runs.append(
                {
                    "review_id": int(row["review_id"]),
                    "reviewed_run_id": int(row["reviewed_run_id"]),
                    "review_date": str(row["review_date"] or ""),
                    "created_at_utc": str(row["created_at_utc"] or ""),
                    "screen_date": str(row["screen_date"] or ""),
                    "xuangu_batch_id": str(row["xuangu_batch_id"] or ""),
                    "total_count": total_count,
                    "pending_count": pending_count,
                    "reviewed_count": reviewed_count,
                    "success_count": success_count,
                    "success_rate_pct": success_rate_pct,
                }
            )
        out["runs"] = runs
        if not runs:
            return out

        valid_ids = {int(item["review_id"]) for item in runs}
        selected_review_id = review_id if review_id in valid_ids else int(runs[0]["review_id"])
        out["selected_review_id"] = selected_review_id
        out["selected_summary"] = next((item for item in runs if int(item["review_id"]) == selected_review_id), None)

        detail_rows = conn.execute(
            """
            WITH latest_code_ml AS (
                SELECT p1.code, p1.probability_up, p1.predicted_score
                FROM weekly_ml_predictions p1
                JOIN (
                    SELECT code, MAX(id) AS max_id
                    FROM weekly_ml_predictions
                    GROUP BY code
                ) t
                  ON t.code = p1.code
                 AND t.max_id = p1.id
            )
            SELECT
                CASE
                    WHEN rr.review_start_date IS NULL OR rr.notes LIKE 'K线不足%' THEN '待复盘'
                    WHEN rr.meets_expectation = 1 THEN '成功'
                    ELSE '失败'
                END AS result,
                rr.code,
                rr.name,
                COALESCE(p_run.probability_up, p_latest.probability_up) AS ml_probability_up,
                COALESCE(p_run.predicted_score, p_latest.predicted_score) AS ml_predicted_score,
                rr.highest_gain_pct,
                rr.close_gain_pct,
                rr.max_drawdown_pct,
                CASE WHEN rr.stop_loss_triggered = 1 THEN '是' ELSE '否' END AS stop_loss_triggered,
                CASE WHEN rr.meets_expectation = 1 THEN '是' ELSE '否' END AS meets_expectation,
                rr.notes
            FROM weekly_review_results rr
            LEFT JOIN weekly_review_runs wr
                ON wr.review_id = rr.review_id
            LEFT JOIN weekly_ml_predictions p_run
                ON p_run.source_run_id = wr.reviewed_run_id
               AND p_run.code = rr.code
            LEFT JOIN latest_code_ml p_latest
                ON p_latest.code = rr.code
            WHERE rr.review_id = ?
            ORDER BY
                CASE WHEN COALESCE(p_run.predicted_score, p_latest.predicted_score) IS NULL THEN 1 ELSE 0 END,
                COALESCE(p_run.predicted_score, p_latest.predicted_score) DESC,
                rr.id DESC
            """,
            (selected_review_id,),
        ).fetchall()
        out["selected_results"] = [dict(row) for row in detail_rows]
        return out
    except sqlite3.OperationalError:
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


def render_xuangu_batch_table(rows: List[Dict[str, Any]], selected_batch_id: str, view_path: str = "/imports") -> str:
    if not rows:
        return "<div class='empty'>还没有条件选股批次。</div>"
    body = []
    for row in rows:
        batch_id = str(row.get("batch_id") or "")
        view_url = f"{view_path}?batch_id={quote(batch_id)}"
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


def render_weekly_screen_form(selected_batch_id: str, default_top_n: int = 5) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    top_n = max(1, min(50, int(default_top_n)))
    return f"""
      <div class="import-box compact">
        <form method="post" action="/actions/run_weekly_screen">
          <div class="form-grid weekly-form-grid">
            <label>
              <span>选股日期</span>
              <input name="screen_date" value="{html.escape(today)}" placeholder="YYYY-MM-DD" />
            </label>
            <label>
              <span>Top N</span>
              <input name="top_n" type="number" min="1" max="50" value="{top_n}" />
            </label>
          </div>
          <div class="form-actions">
            <span class="form-hint">系统会将“选股日期”自动对齐到最近交易日，并用该日期生成固定批次（YYYYMMDD）；同一天多次执行会复用同一批次。当前导入批次：{html.escape(selected_batch_id or '-')}。</span>
            <button type="submit">生成下周 Top 股票（含 ML）</button>
          </div>
        </form>
        <div class="form-actions top-link-actions">
          <span class="form-hint">集中查看最后一次 Top 股票的日线形态。</span>
          <a class="button-link" href="/top">打开 Top 日线图</a>
          <a class="button-link" href="/reviews">查看复盘历史</a>
        </div>
        <form method="post" action="/actions/clear_weekly_top" onsubmit="return confirm('确认清空 Top 股票列表吗？复盘结果会保留。');">
          <div class="form-actions clear-actions">
            <span class="form-hint">清空当前所有 Top 股票和候选打分（保留复盘结果）。</span>
            <button class="danger-btn" type="submit">清空 Top 列表</button>
          </div>
        </form>
      </div>
    """


def render_review_runs_table(rows: List[Dict[str, Any]], selected_review_id: int | None) -> str:
    if not rows:
        return "<div class='empty'>还没有复盘历史记录。</div>"
    body: List[str] = []
    for row in rows:
        rid = int(row.get("review_id") or 0)
        total_count = int(row.get("total_count") or 0)
        pending_count = int(row.get("pending_count") or 0)
        reviewed_count = int(row.get("reviewed_count") or 0)
        success_count = int(row.get("success_count") or 0)
        success_rate_pct = float(row.get("success_rate_pct") or 0.0)
        active = " class='active-row'" if selected_review_id is not None and rid == selected_review_id else ""
        body.append(
            "<tr"
            + active
            + ">"
            + f"<td>{rid}</td>"
            + f"<td>{html.escape(str(row.get('review_date') or '-'))}</td>"
            + f"<td>{html.escape(str(row.get('screen_date') or '-'))}</td>"
            + f"<td>{html.escape(str(row.get('xuangu_batch_id') or '-'))}</td>"
            + f"<td>{html.escape(str(row.get('reviewed_run_id') or '-'))}</td>"
            + f"<td>{total_count}</td>"
            + f"<td>{pending_count}</td>"
            + f"<td>{reviewed_count}</td>"
            + f"<td>{success_count}</td>"
            + f"<td>{success_rate_pct:.1f}%</td>"
            + f"<td>{html.escape(str(row.get('created_at_utc') or '-'))}</td>"
            + f"<td><a href='/reviews?review_id={rid}'>查看</a></td>"
            + "</tr>"
        )
    return f"""
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>复盘ID</th><th>复盘日期</th><th>选股日期</th><th>批次</th><th>Run ID</th>
              <th>总数</th><th>待复盘</th><th>完成复盘</th><th>成功数</th><th>成功率</th><th>创建时间 UTC</th><th>操作</th>
            </tr>
          </thead>
          <tbody>{''.join(body)}</tbody>
        </table>
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


def build_checks_html(
    checks: Dict[str, Any],
    message: str = "",
    error: str = "",
    *,
    include_import_sections: bool = False,
) -> str:
    selected_batch_id = str(checks.get("selected_xuangu_batch_id") or "")
    default_top_n = int(checks.get("screening_top_n") or 5)
    import_form = render_xuangu_import_form(message, error)
    batches = render_xuangu_batch_table(checks.get("xuangu_batches", []), selected_batch_id, view_path="/imports")
    weekly_screen_form = render_weekly_screen_form(selected_batch_id, default_top_n=default_top_n)
    latest_results = render_xuangu_result_table(checks.get("latest_xuangu_results", []), selected_batch_id)
    weekly_selected = render_weekly_selected_table(checks.get("weekly_selected", []))
    weekly_review_summary = checks.get("weekly_review_summary") or {}
    review_summary_html = ""
    if weekly_review_summary:
        review_summary_html = (
            "<div class='notice'>"
            f"最近一次复盘成功率：{float(weekly_review_summary.get('success_rate_pct') or 0.0):.1f}% "
            f"（成功 {int(weekly_review_summary.get('success_count') or 0)} / 完成复盘 {int(weekly_review_summary.get('reviewed_count') or 0)}；"
            f"待复盘 {int(weekly_review_summary.get('pending_count') or 0)} / 总数 {int(weekly_review_summary.get('total_count') or 0)}）"
            "</div>"
        )
    msg_html = f"<div class='notice ok'>{html.escape(message)}</div>" if message and not include_import_sections else ""
    err_html = f"<div class='notice error'>{html.escape(error)}</div>" if error and not include_import_sections else ""
    meta = (
        f"当前导入批次：{html.escape(selected_batch_id or '-')}"
        if include_import_sections
        else f"当前仅显示最近条件选股批次：{html.escape(selected_batch_id or '-')}"
    )
    page_title = "导入页面" if include_import_sections else "选股页面"
    page_sections = (
        f"<section class='card'><h3>导入条件选股 XLSX</h3>{import_form}</section>"
        f"<section class='card'><h3>最近条件选股批次</h3>{batches}</section>"
        f"<section class='card'><h3>当前批次股票明细（批次 {html.escape(selected_batch_id or '-')}）</h3>{latest_results}</section>"
        if include_import_sections
        else f"<section class='card'><h3>周末入选股票（批次 {html.escape(selected_batch_id or '-')}）</h3>{weekly_screen_form}{weekly_selected}<details class='candidate-details'><summary>查看本批次候选股票列表</summary>{latest_results}</details></section>"
             f"<section class='card'><h3>复盘结果</h3>{review_summary_html}<div class='form-actions'><span class='form-hint'>复盘历史已独立到新页面，可查看每次复盘成功率和明细。</span><a class='button-link' href='/reviews'>打开复盘历史页面</a></div></section>"
    )

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{page_title}</title>
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
    .top-link-actions {{
      margin-top: 10px;
    }}
    .button-link {{
      display: inline-block;
      border: 1px solid #bfdbfe;
      background: #eff6ff;
      color: #1d4ed8;
      border-radius: 8px;
      padding: 8px 14px;
      text-decoration: none;
      font-weight: 700;
    }}
    .button-link:hover {{
      background: #dbeafe;
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
      <h2>{page_title}</h2>
      <div>
        <a href="/imports">导入页面</a>
        <a href="/screening">选股页面</a>
        <a href="/reviews">复盘页面</a>
        <a href="/daily">日 K 线</a>
        <a href="/top">Top 日线图</a>
      </div>
    </div>
    <div class="meta">{meta}</div>
    {msg_html}
    {err_html}
    <div class="grid">
      {page_sections}
    </div>
  </div>
</body>
</html>"""


def build_reviews_html(review_data: Dict[str, Any], message: str = "", error: str = "") -> str:
    runs = review_data.get("runs", [])
    selected_review_id = review_data.get("selected_review_id")
    selected_summary = review_data.get("selected_summary") or {}
    selected_results = review_data.get("selected_results", [])
    runs_table = render_review_runs_table(runs, selected_review_id)
    details_table = render_simple_table(
        selected_results,
        [
            ("result", "结果"),
            ("code", "代码"),
            ("name", "名称"),
            ("ml_predicted_score", "ML综合分"),
            ("ml_probability_up", "ML概率"),
            ("highest_gain_pct", "最高涨幅%"),
            ("close_gain_pct", "收盘涨幅%"),
            ("max_drawdown_pct", "最大回撤%"),
            ("stop_loss_triggered", "止损"),
            ("meets_expectation", "符合预期"),
            ("notes", "成功/失败原因"),
        ],
        "该复盘暂无结果。",
    )
    summary_html = "<div class='empty'>暂无复盘摘要。</div>"
    if selected_summary:
        summary_html = (
            "<div class='notice'>"
            f"当前复盘ID：{int(selected_summary.get('review_id') or 0)}，"
            f"成功率：{float(selected_summary.get('success_rate_pct') or 0.0):.1f}% "
            f"（成功 {int(selected_summary.get('success_count') or 0)} / 完成复盘 {int(selected_summary.get('reviewed_count') or 0)}；"
            f"待复盘 {int(selected_summary.get('pending_count') or 0)} / 总数 {int(selected_summary.get('total_count') or 0)}）"
            "</div>"
        )
    msg_html = f"<div class='notice ok'>{html.escape(message)}</div>" if message else ""
    err_html = f"<div class='notice error'>{html.escape(error)}</div>" if error else ""
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>复盘历史</title>
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
      min-width: 820px;
      border-collapse: collapse;
      font-size: 13px;
    }}
    th, td {{
      border-bottom: 1px solid var(--line);
      text-align: left;
      padding: 8px 10px;
      vertical-align: top;
    }}
    th {{
      background: #f9fafb;
      position: sticky;
      top: 0;
      z-index: 1;
    }}
    td a {{
      color: #2563eb;
      text-decoration: none;
      font-weight: 700;
    }}
    td a:hover {{
      color: #ea580c;
      text-decoration: underline;
    }}
    .active-row {{
      background: #ecfeff;
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
  </style>
</head>
<body>
  <div class="wrap">
    <div class="head">
      <h2>复盘历史页面</h2>
      <div>
        <a href="/imports">导入页面</a>
        <a href="/screening">选股页面</a>
        <a href="/reviews">复盘页面</a>
        <a href="/daily">日 K 线</a>
        <a href="/top">Top 日线图</a>
      </div>
    </div>
    <div class="meta">可点击历史复盘记录查看明细；同一 Run 支持多次复盘追加。</div>
    {msg_html}
    {err_html}
    <div class="grid">
      <section class="card">
        <h3>复盘历史列表</h3>
        {runs_table}
      </section>
      <section class="card">
        <h3>复盘明细</h3>
        {summary_html}
        {details_table}
      </section>
    </div>
  </div>
</body>
</html>"""


def build_daily_html(
    rows: List[Dict[str, Any]],
    code: str,
    stock_list: List[Dict[str, Any]],
    *,
    page_heading: str = "东方财富日 K 线",
    sidebar_title: str = "股票列表",
    list_path: str = "/daily",
    list_extra_query: str = "",
    meta_prefix: str = "",
    head_extra_html: str = "",
) -> str:
    chart_rows = list(reversed(rows))
    chart_data_json = json.dumps(chart_rows, ensure_ascii=False).replace("</", "<\\/")
    stock_links = []
    for s in stock_list:
        s_code = str(s.get("code", ""))
        active = "active" if s_code == code else ""
        s_name = html.escape(str(s.get("name", "")))
        rank_no = s.get("rank_no")
        code_label = f"#{rank_no} {s_code}" if rank_no else s_code
        cp = s.get("last_change_percent")
        cp_txt = str(s.get("side_text") or (f"{cp:.2f}%" if isinstance(cp, (int, float)) else "-"))
        cp_cls = str(s.get("side_class") or ("up" if isinstance(cp, (int, float)) and cp > 0 else "down" if isinstance(cp, (int, float)) and cp < 0 else ""))
        href = f"{list_path}?code={quote(s_code)}{list_extra_query}"
        stock_links.append(
            f"<a class='stk {active}' href='{html.escape(href)}'>"
            f"<span class='c'>{html.escape(code_label)}</span>"
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
    head_extra = head_extra_html or ""
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(page_heading)}{html.escape(title_suffix)}</title>
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
      grid-template-columns: 96px minmax(0, 1fr) 70px;
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
      white-space: nowrap;
      font-variant-numeric: tabular-nums;
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
    .head-right {{
      display: flex;
      flex-direction: column;
      align-items: flex-end;
      gap: 8px;
      margin-left: auto;
    }}
    .sort-switch {{
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
    }}
    .sort-chip {{
      border: 1px solid #cbd5e1;
      border-radius: 999px;
      padding: 4px 10px;
      font-size: 12px;
      color: #0f172a;
      text-decoration: none;
      background: #fff;
    }}
    .sort-chip:hover {{
      background: #f8fafc;
    }}
    .sort-chip.active {{
      border-color: #0f766e;
      background: #ccfbf1;
      color: #115e59;
      font-weight: 600;
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
      .head-right {{
        align-items: flex-start;
        width: 100%;
      }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="layout">
      <aside class="side">
        <h3>{html.escape(sidebar_title)}（{len(stock_list)}）</h3>
        <div class="stocks">
          {''.join(stock_links) if stock_links else "<div style='padding:12px;color:#6b7280'>No stocks found.</div>"}
        </div>
      </aside>
      <main class="main">
        <div class="head">
          <h2>{html.escape(page_heading)}{html.escape(title_suffix)}</h2>
          <div class="head-right">
            <div>
              <a href="/screening">选股页面</a>
              <a href="/reviews">复盘页面</a>
              <a href="/top">Top 日线图</a>
            </div>
            {head_extra}
          </div>
        </div>
        <div class="meta">
          {html.escape(meta_prefix) if meta_prefix else "手动刷新"} | 生成时间：{html.escape(now)} | 行数：{len(rows)} | 当前股票：{selected_name}（{selected_code}）
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
            <span><b style="color:#64748b">成交量</b></span>
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
      const chartH = 460;
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
      const toNumOrNaN = (v) => {{
        if (v === null || v === undefined) return Number.NaN;
        if (typeof v === 'string' && v.trim() === '') return Number.NaN;
        const n = Number(v);
        return Number.isFinite(n) ? n : Number.NaN;
      }};

      const points = rows
        .map((r) => ({{
          trade_date: r.trade_date,
          open: toNumOrNaN(r.open),
          close: toNumOrNaN(r.close),
          high: toNumOrNaN(r.high),
          low: toNumOrNaN(r.low),
          volume: toNumOrNaN(r.volume),
          turnover: toNumOrNaN(r.turnover),
          change_percent: toNumOrNaN(r.change_percent),
          change_amount: toNumOrNaN(r.change_amount),
          turnover_rate: toNumOrNaN(r.turnover_rate),
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
      const fmtPct = (v) => Number.isFinite(v) ? `${{fmt(v)}}%` : '-';
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
        const totalPlotH = chartH - pad.top - pad.bottom;
        const volumeGap = 12;
        const volumePlotH = Math.max(60, Math.min(120, Math.round(totalPlotH * 0.22)));
        const plotH = totalPlotH - volumePlotH - volumeGap;
        const volumeTop = pad.top + plotH + volumeGap;
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
        const maxVolume = Math.max(...visible.map((p) => (Number.isFinite(p.volume) && p.volume > 0 ? p.volume : 0)), 1);

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
        for (let i = 0; i <= 2; i++) {{
          const y = volumeTop + (volumePlotH * i / 2);
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

        // Volume bars
        for (let i = 0; i < visible.length; i++) {{
          const p = visible[i];
          const x = pad.left + i * xStep;
          const up = p.close >= p.open;
          const color = up ? 'rgba(220,38,38,0.42)' : 'rgba(22,163,74,0.42)';
          const vol = Number.isFinite(p.volume) && p.volume > 0 ? p.volume : 0;
          const vh = maxVolume > 0 ? (vol / maxVolume) * volumePlotH : 0;
          ctx.fillStyle = color;
          ctx.fillRect(x - candleW / 2, volumeTop + volumePlotH - vh, candleW, Math.max(1, vh));
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
        ctx.fillText('VOL', 10, volumeTop + 12);
        ctx.fillText(fmtInt(maxVolume), 6, volumeTop + volumePlotH);
        ctx.fillText(visible[0].trade_date || '', pad.left, chartH - 8);
        const endTxt = visible[visible.length - 1].trade_date || '';
        const tw = ctx.measureText(endTxt).width;
        ctx.fillText(endTxt, Math.max(pad.left, width - pad.right - tw), chartH - 8);

        // Chip diagram (estimated cost distribution from OHLC range and turnover).
        // Anchor chip window to crosshair day if present; otherwise use current visible end.
        const bins = 52;
        const chip = new Array(bins).fill(0);
        const chipEndAbs = (crossIndexAbs !== null) ? crossIndexAbs : endIndex;
        const recentStart = Math.max(0, chipEndAbs - 249);
        const recent = points.slice(recentStart, chipEndAbs + 1);
        const step = (maxV - minV) / bins;
        const maxDailyVolume = Math.max(...recent.map((p) => Number.isFinite(p.volume) ? p.volume : 0), 1);
        const addChipAtPriceRange = (p, amount) => {{
          const lo = Math.max(minV, p.low);
          const hi = Math.min(maxV, p.high);
          if (!(hi > lo) || !(amount > 0)) return;
          const start = Math.max(0, Math.floor((lo - minV) / step));
          const end = Math.min(bins - 1, Math.floor((hi - minV) / step));
          const span = hi - lo;
          for (let b = start; b <= end; b++) {{
            const bLo = minV + b * step;
            const bHi = bLo + step;
            const overlap = Math.max(0, Math.min(hi, bHi) - Math.max(lo, bLo));
            if (overlap > 0) chip[b] += amount * (overlap / span);
          }}
        }};
        for (const p of recent) {{
          let turnover = Number.isFinite(p.turnover_rate) && p.turnover_rate > 0
            ? p.turnover_rate / 100
            : 0;
          if (!(turnover > 0) && Number.isFinite(p.volume) && p.volume > 0) {{
            turnover = 0.02 + 0.10 * (p.volume / maxDailyVolume);
          }}
          turnover = clamp(turnover, 0.001, 0.95);
          const keep = 1 - turnover;
          for (let b = 0; b < bins; b++) {{
            chip[b] *= keep;
          }}
          addChipAtPriceRange(p, turnover);
        }}
        const maxChip = Math.max(...chip, 0.000001);
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
          const barInset = 1.5;
          chipCtx.fillRect(chipPadL, y0 + barInset, bw, Math.max(1, y1 - y0 - barInset * 2));
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
          ctx.lineTo(x, volumeTop + volumePlotH);
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
            `换手率：${{fmtPct(p.turnover_rate)}}`,
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

      const stocksPanel = document.querySelector('.stocks');
      const getStocksScrollKey = () => {{
        const params = new URLSearchParams(window.location.search || '');
        params.delete('code');
        const suffix = params.toString();
        return `stocks-scroll:${{window.location.pathname}}?${{suffix}}`;
      }};
      const saveStocksScroll = () => {{
        if (!stocksPanel) return;
        try {{
          sessionStorage.setItem(getStocksScrollKey(), String(stocksPanel.scrollTop || 0));
        }} catch (_err) {{
          // ignore storage errors
        }}
      }};
      const restoreStocksScroll = () => {{
        if (!stocksPanel) return;
        try {{
          const raw = sessionStorage.getItem(getStocksScrollKey());
          if (!raw) return;
          const top = Number(raw);
          if (Number.isFinite(top) && top >= 0) {{
            stocksPanel.scrollTop = top;
          }}
        }} catch (_err) {{
          // ignore storage errors
        }}
      }};
      restoreStocksScroll();

      const stockAnchors = Array.from(document.querySelectorAll('.stocks a.stk'));
      stockAnchors.forEach((a) => {{
        a.addEventListener('click', () => {{
          saveStocksScroll();
        }});
      }});
      const isTypingTarget = (target) => {{
        if (!target) return false;
        const tag = String(target.tagName || '').toLowerCase();
        if (tag === 'input' || tag === 'textarea' || tag === 'select') return true;
        return Boolean(target.isContentEditable);
      }};
      window.addEventListener('keydown', (ev) => {{
        if (ev.defaultPrevented) return;
        if (ev.key !== 'ArrowUp' && ev.key !== 'ArrowDown') return;
        if (isTypingTarget(ev.target)) return;
        if (!stockAnchors.length) return;

        const activeIdx = stockAnchors.findIndex((a) => a.classList.contains('active'));
        const baseIdx = activeIdx >= 0 ? activeIdx : 0;
        let nextIdx = baseIdx;
        if (ev.key === 'ArrowUp') {{
          nextIdx = baseIdx <= 0 ? stockAnchors.length - 1 : baseIdx - 1;
        }} else {{
          nextIdx = baseIdx >= stockAnchors.length - 1 ? 0 : baseIdx + 1;
        }}
        const href = stockAnchors[nextIdx].getAttribute('href');
        if (!href) return;
        ev.preventDefault();
        saveStocksScroll();
        window.location.href = href;
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
                self.send_header("Location", "/imports")
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
                    self.send_header("Location", f"/imports?batch_id={quote(batch_id)}&msg={msg}")
                    self.end_headers()
                    return
                except Exception as exc:
                    self.send_response(303)
                    self.send_header("Location", f"/imports?err={quote(str(exc))}")
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
                    top_n_default = int(config.get("screening", {}).get("top_n", 5))
                    top_n = int(form_value(form, "top_n", str(top_n_default)) or str(top_n_default))
                    config["screening"]["top_n"] = max(1, min(50, top_n))
                    screen_date = form_value(form, "screen_date") or datetime.now().strftime("%Y-%m-%d")
                    calendar_cfg = config.get("calendar", {})
                    effective_screen_date = screen_date
                    if calendar_cfg.get("align_to_china_trading_day", True):
                        with weekly_db.connect(db_path) as conn:
                            effective_screen_date = align_to_last_trading_day(
                                screen_date,
                                conn=conn,
                                prefer_akshare=bool(calendar_cfg.get("prefer_akshare", True)),
                            )
                    xuangu_batch_id = effective_screen_date.replace("-", "")
                    run_id = stock_screen_job(
                        config_path=config_path,
                        config=config,
                        screen_date=effective_screen_date,
                        xuangu_batch_id=xuangu_batch_id,
                        replace_existing=True,
                    )
                    msg_text = (
                        f"已生成下周 Top {config['screening']['top_n']} 股票：run_id={run_id}；"
                        f"选股日期={effective_screen_date}；批次={xuangu_batch_id}"
                    )
                    try:
                        model_run_id = ml_predict_job(config_path=config_path, config=config, run_id=run_id)
                        msg_text += f"；已生成 ML 预测：model_run_id={model_run_id}"
                    except Exception as ml_exc:
                        msg_text += f"；Top 已生成，但 ML 预测失败：{ml_exc}"
                    msg = quote(msg_text)
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

            if parsed.path == "/actions/run_ml_predict":
                try:
                    base_dir = db_path.parent
                    config_path = base_dir / "config/weekly_strategy.yaml"
                    config = load_config(config_path)
                    config["database"]["path"] = str(db_path)
                    run_id_text = form_value(form, "run_id")
                    run_id = int(run_id_text) if run_id_text.isdigit() else None
                    model_run_id = ml_predict_job(config_path=config_path, config=config, run_id=run_id)
                    msg = quote(f"已生成 ML 预测：model_run_id={model_run_id}")
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
            self._safe_write(b"Not found")

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
                self._safe_write(payload)
                return

            if parsed.path == "/api/stocks":
                stocks = fetch_stock_list(db_path)
                payload = json.dumps(stocks, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self._safe_write(payload)
                return

            if parsed.path == "/api/daily":
                rows = fetch_daily_rows(db_path, limit, code=code)
                payload = json.dumps(rows, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self._safe_write(payload)
                return

            if parsed.path in {"/api/checks", "/api/screening"}:
                checks = fetch_dashboard_checks(db_path, batch_id=selected_batch_id)
                payload = json.dumps(checks, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self._safe_write(payload)
                return

            if parsed.path == "/api/reviews":
                review_id_text = (query.get("review_id", [""])[0] or "").strip()
                review_id = int(review_id_text) if review_id_text.isdigit() else None
                data = fetch_weekly_review_history(db_path, review_id=review_id)
                payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self._safe_write(payload)
                return

            if parsed.path == "/api/top":
                run_id_text = (query.get("run_id", [""])[0] or "").strip()
                run_id = int(run_id_text) if run_id_text.isdigit() else None
                sort_mode = normalize_top_sort_mode((query.get("sort", ["ml"])[0] or "ml"))
                run, selected_rows = fetch_top_selected_stocks(db_path, run_id=run_id, sort_mode=sort_mode)
                if not selected_rows:
                    fallback_run, fallback_rows = fetch_top_selected_stocks(db_path, run_id=None, sort_mode=sort_mode)
                    if fallback_rows:
                        run, selected_rows = fallback_run, fallback_rows
                from_cache = False
                if selected_rows:
                    cache_top_result(sort_mode, run, selected_rows)
                else:
                    cached = get_cached_top_result(sort_mode)
                    if cached is not None:
                        run, selected_rows = cached
                        from_cache = True
                payload = json.dumps(
                    {"run": run, "selected": selected_rows, "sort_mode": sort_mode, "from_cache": from_cache},
                    ensure_ascii=False,
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self._safe_write(payload)
                return

            if parsed.path == "/":
                self.send_response(303)
                self.send_header("Location", "/screening")
                self.end_headers()
                return

            if parsed.path in {"/checks", "/screening"}:
                checks = fetch_dashboard_checks(db_path, batch_id=selected_batch_id)
                body = build_checks_html(checks, message=message, error=error).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self._safe_write(body)
                return

            if parsed.path in {"/reviews", "/review"}:
                review_id_text = (query.get("review_id", [""])[0] or "").strip()
                review_id = int(review_id_text) if review_id_text.isdigit() else None
                review_data = fetch_weekly_review_history(db_path, review_id=review_id)
                body = build_reviews_html(review_data, message=message, error=error).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self._safe_write(body)
                return

            if parsed.path in {"/imports", "/xuangu"}:
                checks = fetch_dashboard_checks(db_path, batch_id=selected_batch_id)
                body = build_checks_html(
                    checks,
                    message=message,
                    error=error,
                    include_import_sections=True,
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self._safe_write(body)
                return

            if parsed.path == "/top":
                run_id_text = (query.get("run_id", [""])[0] or "").strip()
                run_id = int(run_id_text) if run_id_text.isdigit() else None
                sort_mode = normalize_top_sort_mode((query.get("sort", ["ml"])[0] or "ml"))
                run, selected_rows = fetch_top_selected_stocks(db_path, run_id=run_id, sort_mode=sort_mode)
                if not selected_rows:
                    fallback_run, fallback_rows = fetch_top_selected_stocks(db_path, run_id=None, sort_mode=sort_mode)
                    if fallback_rows:
                        run, selected_rows = fallback_run, fallback_rows
                using_cached_top = False
                if selected_rows:
                    cache_top_result(sort_mode, run, selected_rows)
                else:
                    cached = get_cached_top_result(sort_mode)
                    if cached is not None:
                        run, selected_rows = cached
                        using_cached_top = True
                top_codes = [str(row.get("code") or "") for row in selected_rows if row.get("code")]
                selected_code = code if code in top_codes else (top_codes[0] if top_codes else "")
                latest_by_code = fetch_latest_daily_rows_by_codes(db_path, top_codes)
                top_stock_list = []
                for row in selected_rows:
                    stock_code = str(row.get("code") or "")
                    latest = latest_by_code.get(stock_code, {}) if stock_code else {}
                    ml_probability = row.get("ml_probability_up")
                    side_text = ""
                    side_class = ""
                    if isinstance(ml_probability, (int, float)):
                        side_text = f"ML {ml_probability * 100:.0f}%"
                        side_class = "up" if ml_probability >= 0.5 else "down"
                    top_stock_list.append(
                        {
                            "code": stock_code,
                            "name": row.get("name") or "",
                            "rank_no": row.get("rank_no"),
                            "last_change_percent": latest.get("change_percent"),
                            "side_text": side_text,
                            "side_class": side_class,
                        }
                    )
                rows = fetch_daily_rows(db_path, limit, code=selected_code) if selected_code else []
                effective_run_id = str(run.get("run_id") or "")
                query_parts = [f"sort={quote(sort_mode)}"]
                run_query = "".join(f"&{part}" for part in query_parts)
                mode_label = "ML 综合分" if sort_mode == "ml" else "规则排名"

                def build_top_href(target_sort: str) -> str:
                    parts: List[str] = [f"sort={target_sort}"]
                    if selected_code:
                        parts.append(f"code={quote(selected_code)}")
                    return "/top?" + "&".join(parts)

                sort_switch_html = (
                    "<div class='sort-switch'>"
                    f"<a class='sort-chip {'active' if sort_mode == 'ml' else ''}' href='{html.escape(build_top_href('ml'))}'>按 ML 综合分</a>"
                    f"<a class='sort-chip {'active' if sort_mode == 'rule' else ''}' href='{html.escape(build_top_href('rule'))}'>按规则排名</a>"
                    "</div>"
                )
                meta_parts = ["Top 股票列表", "手动刷新"]
                if run:
                    meta_parts.append(f"Run {run.get('run_id')}")
                    meta_parts.append(f"选股日期：{run.get('screen_date') or '-'}")
                    meta_parts.append(f"批次：{run.get('xuangu_batch_id') or '-'}")
                meta_parts.append(f"排序：{mode_label}")
                if using_cached_top:
                    meta_parts.append("数据库短暂不可读，显示最近一次成功结果")
                body = build_daily_html(
                    rows,
                    code=selected_code,
                    stock_list=top_stock_list,
                    page_heading="Top 股票日 K 线",
                    sidebar_title="Top 股票列表",
                    list_path="/top",
                    list_extra_query=run_query,
                    meta_prefix=" | ".join(meta_parts),
                    head_extra_html=sort_switch_html,
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self._safe_write(body)
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
                self._safe_write(body)
                return

            self.send_response(404)
            self.end_headers()
            self._safe_write(b"Not found")

        def _safe_write(self, payload: bytes) -> None:
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                # Client closed the connection before receiving the full response.
                return

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
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
