from __future__ import annotations

import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import db
from .models import Kline, ReviewResult
from .scoring import rank_candidates


def project_root(config_path: Path) -> Path:
    resolved = config_path.resolve()
    if resolved.parent.name == "config":
        return resolved.parent.parent
    return resolved.parent


def run_xuangu_download(config_path: Path, config: Dict[str, Any]) -> None:
    root = project_root(config_path)
    script = root / "xuangu_to_sqlite.py"
    if not script.exists():
        raise FileNotFoundError(f"xuangu_to_sqlite.py not found at {script}")
    cmd = [
        sys.executable,
        str(script),
        "--condition-file",
        str(root / config["paths"]["screening_file"]),
        "--db",
        str(root / config["database"]["path"]),
        "--download-dir",
        str(root / config["paths"]["download_dir"]),
        "--browser-engine",
        "firefox",
        "--browser-headed",
        "--manual-download",
    ]
    subprocess.run(cmd, check=True)


def stock_screen_job(config_path: Path, config: Dict[str, Any], screen_date: Optional[str] = None, xuangu_batch_id: Optional[str] = None) -> int:
    root = project_root(config_path)
    db_path = root / config["database"]["path"]
    screening_path = root / config["paths"]["screening_file"]
    screening_text = screening_path.read_text(encoding="utf-8").strip() if screening_path.exists() else ""

    if config["screening"].get("run_xuangu"):
        run_xuangu_download(config_path, config)

    with db.connect(db_path) as conn:
        db.ensure_weekly_tables(conn)
        if xuangu_batch_id is None:
            xuangu_batch_id = db.latest_xuangu_batch_id(conn)
        candidates = db.load_xuangu_candidates(conn, xuangu_batch_id)
        if not candidates:
            raise RuntimeError("No xuangu candidates found. Run xuangu download first.")

        klines_by_code = {c.code: db.load_klines(conn, c.code) for c in candidates}
        ranked = rank_candidates(candidates, klines_by_code, config)
        top_n = int(config["screening"]["top_n"])
        min_score = float(config["screening"].get("min_score", 0))
        selected = [item for item in ranked if item.score.total >= min_score][:top_n]
        if len(selected) < min(top_n, 3):
            selected = ranked[:top_n]

        run_id = db.create_screen_run(
            conn=conn,
            screen_date=screen_date or date.today().isoformat(),
            xuangu_batch_id=xuangu_batch_id,
            config=config,
            screening_text=screening_text,
            candidate_count=len(candidates),
            selected_count=len(selected),
        )
        db.save_screen_results(conn, run_id, ranked, len(selected))
        return run_id


def weekly_review_job(config_path: Path, config: Dict[str, Any], review_date: Optional[str] = None, run_id: Optional[int] = None) -> int:
    root = project_root(config_path)
    db_path = root / config["database"]["path"]
    with db.connect(db_path) as conn:
        db.ensure_weekly_tables(conn)
        reviewed_run_id = run_id if run_id is not None else db.latest_selected_run_without_review(conn)
        if reviewed_run_id is None:
            raise RuntimeError("No selected weekly screen run needs review.")
        selected = db.selected_stocks_for_run(conn, reviewed_run_id)
        if not selected:
            raise RuntimeError(f"Run {reviewed_run_id} has no selected stocks.")

        review_id = db.create_review_run(conn, reviewed_run_id, review_date or date.today().isoformat(), config)
        results = [
            review_selected_stock(db.load_klines(conn, row["code"], limit=320), row, config)
            for row in selected
        ]
        db.save_review_results(conn, review_id, selected, results)
        return review_id


def review_selected_stock(klines: List[Kline], selected_row: Any, config: Dict[str, Any]) -> ReviewResult:
    screen_date = selected_row["screen_date"]
    horizon = int(config["review"]["horizon_trading_days"])
    stop_loss_pct = float(config["review"]["stop_loss_pct"]) * 100
    expected_high = float(config["review"]["expected_high_gain_pct"]) * 100
    expected_close = float(config["review"]["expected_close_gain_pct"]) * 100

    previous = [k for k in klines if k.trade_date <= screen_date and k.close is not None]
    future = [k for k in klines if k.trade_date > screen_date and k.close is not None][:horizon]
    if not previous or not future:
        return ReviewResult(
            code=selected_row["code"],
            name=selected_row["name"],
            base_trade_date=previous[-1].trade_date if previous else None,
            review_start_date=future[0].trade_date if future else None,
            review_end_date=future[-1].trade_date if future else None,
            highest_gain_pct=None,
            close_gain_pct=None,
            max_drawdown_pct=None,
            stop_loss_triggered=False,
            meets_expectation=False,
            notes="K线不足，无法完整复盘",
        )

    base = previous[-1]
    base_close = base.close or 0
    highs = [k.high for k in future if k.high is not None]
    lows = [k.low for k in future if k.low is not None]
    closes = [k.close for k in future if k.close is not None]
    highest_gain = (max(highs) / base_close - 1) * 100 if base_close and highs else None
    close_gain = (closes[-1] / base_close - 1) * 100 if base_close and closes else None
    max_drawdown = (min(lows) / base_close - 1) * 100 if base_close and lows else None
    stop_loss = max_drawdown is not None and max_drawdown <= -stop_loss_pct
    meets = (highest_gain is not None and highest_gain >= expected_high) or (
        close_gain is not None and close_gain >= expected_close
    )

    notes = []
    if highest_gain is not None:
        notes.append(f"下周最高涨幅 {highest_gain:.2f}%")
    if close_gain is not None:
        notes.append(f"下周收盘涨幅 {close_gain:.2f}%")
    if stop_loss:
        notes.append("触发止损")
    notes.append("符合预期" if meets else "未达到预期")

    return ReviewResult(
        code=selected_row["code"],
        name=selected_row["name"],
        base_trade_date=base.trade_date,
        review_start_date=future[0].trade_date,
        review_end_date=future[-1].trade_date,
        highest_gain_pct=highest_gain,
        close_gain_pct=close_gain,
        max_drawdown_pct=max_drawdown,
        stop_loss_triggered=stop_loss,
        meets_expectation=meets,
        notes="；".join(notes),
    )
