from __future__ import annotations

import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import db
from .models import Kline, ReviewResult
from .ml import (
    TrainingSample,
    add_cross_sectional_features,
    add_cross_sectional_to_predictions,
    backtest_models,
    build_training_samples,
    features_at,
    train_model,
)
from .scoring import rank_candidates
from .trading_calendar import align_to_last_trading_day


def project_root(config_path: Path) -> Path:
    resolved = config_path.resolve()
    if resolved.parent.name == "config":
        return resolved.parent.parent
    return resolved.parent


def apply_review_feedback_labels(
    samples: List[TrainingSample],
    feedback_labels: Dict[tuple[str, str], int],
    weight: int = 1,
) -> tuple[List[TrainingSample], List[float], Dict[str, int]]:
    if not samples or not feedback_labels:
        return samples, [1.0] * len(samples), {"matched": 0, "relabeled": 0, "extra_weighted": 0}

    use_weight = max(1, int(weight))
    merged: List[TrainingSample] = []
    sample_weights: List[float] = []
    matched = 0
    relabeled = 0
    extra_weighted = 0
    for sample in samples:
        key = (str(sample.code), str(sample.trade_date))
        label = feedback_labels.get(key)
        if label is None:
            merged.append(sample)
            sample_weights.append(1.0)
            continue

        matched += 1
        normalized_label = 1 if int(label) else 0
        if int(sample.label) != normalized_label:
            relabeled += 1
        updated = TrainingSample(
            code=sample.code,
            trade_date=sample.trade_date,
            features=dict(sample.features),
            label=normalized_label,
            future_high_gain_pct=sample.future_high_gain_pct,
            future_close_gain_pct=sample.future_close_gain_pct,
            future_max_drawdown_pct=sample.future_max_drawdown_pct,
        )
        merged.append(updated)
        sample_weights.append(float(use_weight))
        extra_weighted += use_weight - 1
    return merged, sample_weights, {"matched": matched, "relabeled": relabeled, "extra_weighted": extra_weighted}


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


def stock_screen_job(
    config_path: Path,
    config: Dict[str, Any],
    screen_date: Optional[str] = None,
    xuangu_batch_id: Optional[str] = None,
    replace_existing: bool = False,
) -> int:
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
        effective_screen_date = screen_date or date.today().isoformat()
        calendar_cfg = config.get("calendar", {})
        if calendar_cfg.get("align_to_china_trading_day", True):
            effective_screen_date = align_to_last_trading_day(
                effective_screen_date,
                conn=conn,
                prefer_akshare=bool(calendar_cfg.get("prefer_akshare", True)),
            )
        if replace_existing:
            deleted = db.delete_screen_runs(conn, effective_screen_date, xuangu_batch_id)
            if deleted:
                print(f"Deleted {deleted} existing weekly screen run(s) for {effective_screen_date}/{xuangu_batch_id}.")

        klines_by_code = {
            c.code: db.load_klines(conn, c.code, as_of_date=effective_screen_date)
            for c in candidates
        }
        ranked = rank_candidates(candidates, klines_by_code, config)
        top_n = int(config["screening"]["top_n"])
        min_score = float(config["screening"].get("min_score", 0))
        selected = [item for item in ranked if item.score.total >= min_score][:top_n]
        if len(selected) < min(top_n, 3):
            selected = ranked[:top_n]

        run_id = db.create_screen_run(
            conn=conn,
            screen_date=effective_screen_date,
            xuangu_batch_id=xuangu_batch_id,
            config=config,
            screening_text=screening_text,
            candidate_count=len(candidates),
            selected_count=len(selected),
        )
        db.save_screen_results(conn, run_id, ranked, len(selected))
        return run_id


def weekly_review_job(
    config_path: Path,
    config: Dict[str, Any],
    review_date: Optional[str] = None,
    run_id: Optional[int] = None,
    replace_existing: bool = False,
) -> int:
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

        effective_review_date = review_date or date.today().isoformat()
        calendar_cfg = config.get("calendar", {})
        if calendar_cfg.get("align_to_china_trading_day", True):
            effective_review_date = align_to_last_trading_day(
                effective_review_date,
                conn=conn,
                prefer_akshare=bool(calendar_cfg.get("prefer_akshare", True)),
            )

        if replace_existing:
            deleted = db.delete_review_for_run(conn, reviewed_run_id)
            if deleted:
                print(f"Deleted {deleted} existing review run(s) for run_id={reviewed_run_id}.")

        review_id = db.create_review_run(conn, reviewed_run_id, effective_review_date, config)
        results = [
            review_selected_stock(db.load_klines(conn, row["code"], limit=320), row, config)
            for row in selected
        ]
        db.save_review_results(conn, review_id, selected, results)
        return review_id


def ml_predict_job(config_path: Path, config: Dict[str, Any], run_id: Optional[int] = None) -> int:
    root = project_root(config_path)
    db_path = root / config["database"]["path"]
    ml_cfg = config.get("ml", {})
    with db.connect(db_path) as conn:
        db.ensure_weekly_tables(conn)
        source_run_id = run_id if run_id is not None else db.latest_selected_run(conn)
        if source_run_id is None:
            raise RuntimeError("No weekly selected stocks found. Run stock_screen_job first.")

        selected_rows = db.selected_stocks_for_run(conn, source_run_id)
        if not selected_rows:
            raise RuntimeError(f"Run {source_run_id} has no selected stocks.")

        train_codes = db.all_downloaded_codes(conn)
        if ml_cfg.get("train_on_selected_universe", False):
            train_codes = db.selected_codes_for_run(conn, source_run_id)
        klines_by_code = {
            code: db.load_klines(conn, code, limit=int(ml_cfg.get("history_limit", 320)))
            for code in train_codes
        }
        samples = build_training_samples(klines_by_code, ml_cfg)
        sample_weights: Optional[List[float]] = None
        if ml_cfg.get("use_review_feedback_labels", False):
            recent_runs = int(ml_cfg.get("review_feedback_recent_runs", 0))
            feedback = db.review_feedback_labels(conn, recent_runs=recent_runs)
            feedback_weight = int(ml_cfg.get("review_feedback_weight", 1))
            samples, sample_weights, feedback_stats = apply_review_feedback_labels(samples, feedback, weight=feedback_weight)
            if feedback_stats["matched"] > 0:
                print(
                    "Applied review feedback labels: "
                    f"matched={feedback_stats['matched']} "
                    f"relabeled={feedback_stats['relabeled']} "
                    f"extra_weighted={feedback_stats['extra_weighted']}"
                )
        min_samples = int(ml_cfg.get("min_train_samples", 30))
        if len(samples) < min_samples:
            raise RuntimeError(
                f"Not enough ML training samples: {len(samples)} < {min_samples}. "
                "Download more K-line history or lower ml.min_train_samples."
            )
        model = train_model(samples, ml_cfg, sample_weights=sample_weights)
        positive_count = sum(1 for sample in samples if sample.label == 1)
        model_run_id = db.create_ml_model_run(
            conn=conn,
            source_run_id=source_run_id,
            model_name=str(ml_cfg.get("model_name", "centroid_v1")),
            config=ml_cfg,
            train_sample_count=len(samples),
            positive_sample_count=positive_count,
            model_json=model.to_json(),
        )
        db.save_ml_training_samples(conn, model_run_id, source_run_id, samples)

        prediction_items: List[tuple[Any, Dict[str, float], str]] = []
        for row in selected_rows:
            code = str(row["code"])
            klines = db.load_klines(conn, code, limit=int(ml_cfg.get("history_limit", 320)))
            if not klines:
                continue
            features = features_at(klines, len(klines) - 1)
            if features is None:
                continue
            trade_date = klines[-1].trade_date
            prediction_items.append((row, features, trade_date))

        if ml_cfg.get("use_cross_sectional_features", True):
            add_cross_sectional_to_predictions(
                [(trade_date, features) for _row, features, trade_date in prediction_items]
            )

        predictions = []
        for row, features, _trade_date in prediction_items:
            code = str(row["code"])
            probability = model.predict_probability(features)
            baseline_probability = None
            if hasattr(model, "predict_baseline_probability"):
                baseline_probability = model.predict_baseline_probability(features)
            rule_score = float(row["total_score"] or 0)
            rule_weight = float(ml_cfg.get("rule_score_weight", 0.35))
            predicted_score = probability * 100 * (1 - rule_weight) + rule_score * rule_weight
            predictions.append(
                {
                    "code": code,
                    "name": row["name"],
                    "probability_up": round(probability, 4),
                    "predicted_score": round(predicted_score, 2),
                    "features": {
                        **features,
                        "baseline_probability_up": round(baseline_probability, 4)
                        if baseline_probability is not None
                        else None,
                    },
                    "reason": model.explain(features),
                }
            )
        predictions.sort(key=lambda item: (item["predicted_score"], item["probability_up"]), reverse=True)
        db.save_ml_predictions(conn, model_run_id, source_run_id, predictions)
        return model_run_id


def ml_backtest_job(config_path: Path, config: Dict[str, Any]) -> List[Any]:
    root = project_root(config_path)
    db_path = root / config["database"]["path"]
    ml_cfg = config.get("ml", {})
    with db.connect(db_path) as conn:
        db.ensure_weekly_tables(conn)
        train_codes = db.all_downloaded_codes(conn)
        klines_by_code = {
            code: db.load_klines(conn, code, limit=int(ml_cfg.get("history_limit", 320)))
            for code in train_codes
        }
    samples = build_training_samples(klines_by_code, ml_cfg)
    min_samples = int(ml_cfg.get("min_train_samples", 30))
    if len(samples) < min_samples:
        raise RuntimeError(
            f"Not enough ML backtest samples: {len(samples)} < {min_samples}. "
            "Download more K-line history or lower ml.min_train_samples."
        )
    return backtest_models(samples, ml_cfg)


def review_selected_stock(klines: List[Kline], selected_row: Any, config: Dict[str, Any]) -> ReviewResult:
    screen_date = selected_row["screen_date"]
    horizon = int(config["review"]["horizon_trading_days"])
    stop_loss_pct = float(config["review"]["stop_loss_pct"]) * 100
    expected_high = float(config["review"]["expected_high_gain_pct"]) * 100
    expected_close = float(config["review"]["expected_close_gain_pct"]) * 100

    previous = [k for k in klines if k.trade_date <= screen_date and k.close is not None]
    future = [k for k in klines if k.trade_date > screen_date and k.close is not None][:horizon]
    if not previous or not future:
        last_trade_date = klines[-1].trade_date if klines else None
        reasons = []
        if not previous:
            reasons.append("缺少选股日及之前的收盘K线")
        if not future:
            reasons.append("缺少选股日之后的K线")
        if last_trade_date:
            reasons.append(f"当前最新K线日期={last_trade_date}")
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
            notes="K线不足，无法完整复盘；" + "；".join(reasons),
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
    target_hit = (highest_gain is not None and highest_gain >= expected_high) or (
        close_gain is not None and close_gain >= expected_close
    )
    meets = target_hit and not stop_loss

    notes = [f"复盘区间 {future[0].trade_date} 到 {future[-1].trade_date}，共 {len(future)}/{horizon} 个交易日"]
    success_reasons = []
    failure_reasons = []
    if highest_gain is not None:
        if highest_gain >= expected_high:
            success_reasons.append(f"最高涨幅 {highest_gain:.2f}% 达到目标 {expected_high:.2f}%")
        else:
            failure_reasons.append(
                f"最高涨幅 {highest_gain:.2f}% 低于目标 {expected_high:.2f}%，差 {expected_high - highest_gain:.2f}%"
            )
    if close_gain is not None:
        if close_gain >= expected_close:
            success_reasons.append(f"收盘涨幅 {close_gain:.2f}% 达到目标 {expected_close:.2f}%")
        else:
            failure_reasons.append(
                f"收盘涨幅 {close_gain:.2f}% 低于目标 {expected_close:.2f}%，差 {expected_close - close_gain:.2f}%"
            )
    if stop_loss:
        failure_reasons.append(f"最大回撤 {max_drawdown:.2f}% 触发止损线 {-stop_loss_pct:.2f}%")
    elif max_drawdown is not None:
        success_reasons.append(f"最大回撤 {max_drawdown:.2f}%，未触发止损线 {-stop_loss_pct:.2f}%")

    if meets:
        notes.append("成功原因：" + "；".join(success_reasons))
    else:
        notes.append("失败原因：" + "；".join(failure_reasons or ["未命中上涨目标"]))
        if success_reasons:
            notes.append("有利因素：" + "；".join(success_reasons))

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
