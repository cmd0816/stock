#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import smtplib
import sqlite3
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_log(log_text: str) -> dict:
    stats = {
        "pending_stocks": None,
        "skipped_covered": None,
        "stocks_ok": None,
        "rows_saved": None,
        "failure_count": 0,
        "all_covered": False,
    }
    m_download = re.search(
        r"Downloading 1-year daily K-line data for (\d+) stocks .* \(skipped (\d+) already covered stocks\)",
        log_text,
    )
    if m_download:
        stats["pending_stocks"] = int(m_download.group(1))
        stats["skipped_covered"] = int(m_download.group(2))

    m_summary = re.search(r"History download summary: stocks_ok=(\d+), rows_saved=(\d+)", log_text)
    if m_summary:
        stats["stocks_ok"] = int(m_summary.group(1))
        stats["rows_saved"] = int(m_summary.group(2))

    m_fail = re.search(r"History download completed with (\d+) failures:", log_text)
    if m_fail:
        stats["failure_count"] = int(m_fail.group(1))

    stats["all_covered"] = "already have at least" in log_text and "skip history download" in log_text
    return stats


def query_batch_status(db_path: Path, batch_id: str, aligned_date: str) -> dict:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT stock_code
            FROM xuangu_results
            WHERE batch_id = ? AND stock_code IS NOT NULL AND stock_code <> ''
            """,
            (batch_id,),
        ).fetchall()
        codes = [str(r[0]) for r in rows if r[0]]
        if not codes:
            return {
                "batch_stock_count": 0,
                "updated_to_aligned_count": 0,
                "total_kline_rows": 0,
            }

        placeholders = ",".join("?" for _ in codes)
        total_kline_rows = conn.execute(
            f"""
            SELECT COUNT(*)
            FROM eastmoney_stock_daily_klines
            WHERE code IN ({placeholders})
            """,
            codes,
        ).fetchone()[0]
        aligned_rows = conn.execute(
            f"""
            SELECT COUNT(*)
            FROM (
              SELECT code, MAX(trade_date) AS latest_trade_date
              FROM eastmoney_stock_daily_klines
              WHERE code IN ({placeholders})
              GROUP BY code
            )
            WHERE latest_trade_date >= ?
            """,
            [*codes, aligned_date],
        ).fetchone()[0]
    return {
        "batch_stock_count": len(codes),
        "updated_to_aligned_count": int(aligned_rows or 0),
        "total_kline_rows": int(total_kline_rows or 0),
    }


def query_top_changes(db_path: Path) -> dict:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if "weekly_screen_runs" not in tables or "weekly_selected_stocks" not in tables:
            return {"available": False, "reason": "周末 Top 表不存在"}

        runs = conn.execute(
            """
            SELECT r.run_id, r.screen_date
            FROM weekly_screen_runs r
            WHERE EXISTS (SELECT 1 FROM weekly_selected_stocks s WHERE s.run_id = r.run_id)
            ORDER BY r.screen_date DESC, r.run_id DESC
            LIMIT 2
            """
        ).fetchall()
        if not runs:
            return {"available": False, "reason": "还没有周末 Top 记录"}

        latest = runs[0]
        prev = runs[1] if len(runs) >= 2 else None

        latest_rows = conn.execute(
            """
            SELECT code, COALESCE(name, '') AS name, rank_no
            FROM weekly_selected_stocks
            WHERE run_id = ?
            ORDER BY rank_no, id
            """,
            (int(latest["run_id"]),),
        ).fetchall()
        latest_map = {str(row["code"]): row for row in latest_rows}

        if prev is None:
            return {
                "available": True,
                "latest_run_id": int(latest["run_id"]),
                "latest_date": str(latest["screen_date"] or ""),
                "prev_run_id": None,
                "prev_date": None,
                "added": [dict(row) for row in latest_rows],
                "removed": [],
                "rank_up": [],
                "rank_down": [],
            }

        prev_rows = conn.execute(
            """
            SELECT code, COALESCE(name, '') AS name, rank_no
            FROM weekly_selected_stocks
            WHERE run_id = ?
            ORDER BY rank_no, id
            """,
            (int(prev["run_id"]),),
        ).fetchall()
        prev_map = {str(row["code"]): row for row in prev_rows}

        added = [dict(row) for row in latest_rows if str(row["code"]) not in prev_map]
        removed = [dict(row) for row in prev_rows if str(row["code"]) not in latest_map]
        rank_up = []
        rank_down = []
        for code, now in latest_map.items():
            old = prev_map.get(code)
            if old is None:
                continue
            old_rank = int(old["rank_no"])
            new_rank = int(now["rank_no"])
            if new_rank < old_rank:
                rank_up.append(
                    {
                        "code": code,
                        "name": str(now["name"] or old["name"] or ""),
                        "old_rank": old_rank,
                        "new_rank": new_rank,
                    }
                )
            elif new_rank > old_rank:
                rank_down.append(
                    {
                        "code": code,
                        "name": str(now["name"] or old["name"] or ""),
                        "old_rank": old_rank,
                        "new_rank": new_rank,
                    }
                )
        rank_up.sort(key=lambda item: (item["new_rank"], item["code"]))
        rank_down.sort(key=lambda item: (item["new_rank"], item["code"]))
        return {
            "available": True,
            "latest_run_id": int(latest["run_id"]),
            "latest_date": str(latest["screen_date"] or ""),
            "prev_run_id": int(prev["run_id"]),
            "prev_date": str(prev["screen_date"] or ""),
            "added": added,
            "removed": removed,
            "rank_up": rank_up,
            "rank_down": rank_down,
        }


def render_top_changes(top_changes: dict) -> str:
    if not top_changes.get("available"):
        return f"- 无法生成 Top 变动：{top_changes.get('reason', '未知原因')}"

    lines = []
    lines.append(
        f"- 最新 run_id={top_changes['latest_run_id']} ({top_changes['latest_date']})"
    )
    if top_changes.get("prev_run_id") is None:
        lines.append("- 上一次 Top 不存在（首次生成），以下均为新增：")
    else:
        lines.append(
            f"- 对比 run_id={top_changes['prev_run_id']} ({top_changes['prev_date']})"
        )

    added = top_changes.get("added", [])
    removed = top_changes.get("removed", [])
    rank_up = top_changes.get("rank_up", [])
    rank_down = top_changes.get("rank_down", [])

    if added:
        lines.append("- 新增:")
        for item in added[:10]:
            lines.append(f"  + #{int(item['rank_no'])} {item['code']} {item.get('name', '')}".rstrip())
    else:
        lines.append("- 新增: 无")

    if removed:
        lines.append("- 移除:")
        for item in removed[:10]:
            lines.append(f"  - #{int(item['rank_no'])} {item['code']} {item.get('name', '')}".rstrip())
    else:
        lines.append("- 移除: 无")

    if rank_up:
        lines.append("- 排名上升:")
        for item in rank_up[:10]:
            lines.append(
                f"  ↑ {item['code']} {item.get('name', '')} #{item['old_rank']} -> #{item['new_rank']}".rstrip()
            )
    else:
        lines.append("- 排名上升: 无")

    if rank_down:
        lines.append("- 排名下降:")
        for item in rank_down[:10]:
            lines.append(
                f"  ↓ {item['code']} {item.get('name', '')} #{item['old_rank']} -> #{item['new_rank']}".rstrip()
            )
    else:
        lines.append("- 排名下降: 无")
    return "\n".join(lines)


def build_message(meta: dict, stats: dict, batch_status: dict, top_changes: dict, tail_text: str) -> tuple[str, str]:
    ok = int(meta["status"]) == 0
    subject_prefix = os.getenv("DAILY_EMAIL_SUBJECT_PREFIX", "[stock]")
    subject = (
        f"{subject_prefix} Daily Update {'SUCCESS' if ok else 'FAILED'} "
        f"{meta['aligned_date']} batch={meta['batch_id']}"
    )

    body = (
        f"Daily K-line update {'成功' if ok else '失败'}\n"
        f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"目标日期: {meta['target_date']}\n"
        f"交易日对齐: {meta['aligned_date']}\n"
        f"选股批次: {meta['batch_id']}\n"
        f"退出状态: {meta['status']}\n\n"
        f"本次下载统计:\n"
        f"- 待更新股票数: {stats['pending_stocks'] if stats['pending_stocks'] is not None else '-'}\n"
        f"- 已覆盖跳过数: {stats['skipped_covered'] if stats['skipped_covered'] is not None else '-'}\n"
        f"- 成功股票数: {stats['stocks_ok'] if stats['stocks_ok'] is not None else '-'}\n"
        f"- 新增/更新K线行数: {stats['rows_saved'] if stats['rows_saved'] is not None else '-'}\n"
        f"- 失败股票数: {stats['failure_count']}\n"
        f"- 是否全部已覆盖: {'是' if stats['all_covered'] else '否'}\n\n"
        f"批次当前覆盖:\n"
        f"- 批次股票总数: {batch_status['batch_stock_count']}\n"
        f"- 已更新到对齐交易日: {batch_status['updated_to_aligned_count']}\n"
        f"- 批次K线总行数: {batch_status['total_kline_rows']}\n\n"
        f"Top 变动列表:\n"
        f"{render_top_changes(top_changes)}\n\n"
        f"日志尾部:\n"
        f"{tail_text}"
    )
    return subject, body


def env_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def send_email(subject: str, body: str) -> None:
    to_addr = os.getenv("DAILY_EMAIL_TO", "").strip()
    if not to_addr:
        return

    smtp_host = os.getenv("SMTP_HOST", "").strip()
    if not smtp_host:
        raise RuntimeError("SMTP_HOST is required when DAILY_EMAIL_TO is set.")
    smtp_port = int(os.getenv("SMTP_PORT", "465"))
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_pass = os.getenv("SMTP_PASS", "")
    from_addr = os.getenv("DAILY_EMAIL_FROM", "").strip() or smtp_user or to_addr
    use_ssl = env_bool("SMTP_USE_SSL", default=True)
    use_starttls = env_bool("SMTP_STARTTLS", default=not use_ssl)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(body)

    if use_ssl:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30) as server:
            if smtp_user:
                server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        return

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
        if use_starttls:
            server.starttls()
        if smtp_user:
            server.login(smtp_user, smtp_pass)
        server.send_message(msg)


def main() -> None:
    meta = {
        "db_path": env_required("DAILY_UPDATE_DB_PATH"),
        "batch_id": env_required("DAILY_UPDATE_BATCH_ID"),
        "target_date": env_required("DAILY_UPDATE_TARGET_DATE"),
        "aligned_date": env_required("DAILY_UPDATE_ALIGNED_DATE"),
        "status": env_required("DAILY_UPDATE_STATUS"),
        "log_file": env_required("DAILY_UPDATE_LOG_FILE"),
    }

    log_path = Path(meta["log_file"])
    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    stats = parse_log(log_text)
    db_path = Path(meta["db_path"])
    batch_status = query_batch_status(db_path, meta["batch_id"], meta["aligned_date"])
    top_changes = query_top_changes(db_path)

    tail_lines = max(10, int(os.getenv("DAILY_EMAIL_LOG_LINES", "80")))
    tail = "\n".join(log_text.splitlines()[-tail_lines:]) if log_text else "(no log)"
    subject, body = build_message(meta, stats, batch_status, top_changes, tail)
    send_email(subject, body)


if __name__ == "__main__":
    main()
