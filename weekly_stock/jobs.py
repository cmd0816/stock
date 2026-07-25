from __future__ import annotations

import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import db
from .models import Kline, ReviewResult
from .ml import (
    TrainingSample,
    add_cross_sectional_to_predictions,
    apply_training_label_overrides,
    backtest_models,
    build_training_samples,
    features_at,
    purged_walk_forward_splits,
    split_samples_by_time,
    train_model,
)
from .scoring import rank_candidates
from .trade_simulator import simulate_trade
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
    return apply_training_label_overrides(samples, feedback_labels, weight=weight)


def attach_context_features_to_samples(conn: Any, samples: List[TrainingSample]) -> None:
    if not samples:
        return
    code_dates = [(str(sample.code), str(sample.trade_date)) for sample in samples]
    context_by_key = db.load_ml_context_features(conn, code_dates)
    if not context_by_key:
        return
    for sample in samples:
        ctx = context_by_key.get((str(sample.code), str(sample.trade_date)))
        if not ctx:
            continue
        sample.features.update(ctx)


def attach_context_features_to_prediction_items(
    conn: Any,
    prediction_items: List[Tuple[Any, Dict[str, float], str]],
) -> None:
    if not prediction_items:
        return
    code_dates = [(str(row["code"]), str(trade_date)) for row, _features, trade_date in prediction_items]
    context_by_key = db.load_ml_context_features(conn, code_dates)
    if not context_by_key:
        return
    for row, features, trade_date in prediction_items:
        ctx = context_by_key.get((str(row["code"]), str(trade_date)))
        if not ctx:
            continue
        features.update(ctx)


def normalized_rule_scores(rows: List[Any], enabled: bool = True) -> Dict[str, float]:
    raw_by_code = {str(row["code"]): float(row["total_score"] or 0.0) for row in rows}
    if not enabled:
        return raw_by_code
    unique_scores = sorted(set(raw_by_code.values()))
    if len(unique_scores) <= 1:
        return {code: 50.0 for code in raw_by_code}
    percentile_by_score = {
        score: rank * 100.0 / (len(unique_scores) - 1)
        for rank, score in enumerate(unique_scores)
    }
    return {code: percentile_by_score[score] for code, score in raw_by_code.items()}


def download_review_history_for_selected_stocks(
    db_path: Path,
    selected_rows: List[Any],
    target_date: str,
    config: Dict[str, Any],
) -> Dict[str, int]:
    """Download K-line history for every stock selected in the reviewed run."""
    if not selected_rows:
        return {"total": 0, "updated": 0, "skipped": 0, "failed": 0}

    from download_top_history_akshare import fetch_kline_with_akshare, infer_market, save_akshare_kline_rows

    review_cfg = config.get("review", {})
    days = max(30, int(review_cfg.get("download_days", 365)))
    min_existing_days = max(0, int(review_cfg.get("download_min_existing_days", 200)))
    adjust = str(review_cfg.get("download_adjust", "qfq"))
    source = str(review_cfg.get("download_source", "auto"))
    fail_on_error = bool(review_cfg.get("download_fail_on_error", False))

    end_dt = datetime.strptime(target_date, "%Y-%m-%d") if target_date else datetime.now()
    start_dt = end_dt - timedelta(days=days)
    start_yyyymmdd = start_dt.strftime("%Y%m%d")
    end_yyyymmdd = end_dt.strftime("%Y%m%d")

    updated = 0
    skipped = 0
    failed = 0
    errors: List[str] = []
    total = len(selected_rows)
    print(f"Review history download: run selected stocks={total}, target_date={target_date or end_dt.date().isoformat()}")

    for row in selected_rows:
        code = str(row["code"] or "").strip()
        name = str(row["name"] or "")
        rank_no = int(row["rank_no"] or 0)
        if not code:
            continue

        conn = db.connect(db_path)
        try:
            existing = conn.execute(
                """
                SELECT COUNT(*) AS cnt, MAX(trade_date) AS latest_trade_date
                FROM eastmoney_stock_daily_klines
                WHERE code = ?
                """,
                (code,),
            ).fetchone()
        finally:
            conn.close()
        existing_days = int(existing["cnt"] or 0) if existing else 0
        latest_trade_date = str(existing["latest_trade_date"] or "") if existing else ""
        if (
            existing_days >= min_existing_days
            and latest_trade_date
            and (not target_date or latest_trade_date >= target_date)
        ):
            print(
                f"Skip selected#{rank_no} {code} {name}: already {existing_days} rows, "
                f"latest={latest_trade_date} >= {target_date}"
            )
            skipped += 1
            continue

        try:
            df, source_name = fetch_kline_with_akshare(code, start_yyyymmdd, end_yyyymmdd, adjust, source)
            records = df.to_dict(orient="records")
            if not records:
                raise RuntimeError("AKShare returned empty rows")
            row_count = save_akshare_kline_rows(db_path, infer_market(code), code, name, records, source_name)
            print(f"Updated selected#{rank_no} {code} {name}: saved {row_count} rows via AKShare ({source_name})")
            updated += 1
        except Exception as exc:
            message = f"Failed selected#{rank_no} {code} {name}: {exc}"
            print(message)
            errors.append(message)
            failed += 1

    print(f"Review history download summary: total={total}, updated={updated}, skipped={skipped}, failed={failed}")
    if fail_on_error and errors:
        raise RuntimeError("Review history download failed: " + " | ".join(errors[:3]))
    return {"total": total, "updated": updated, "skipped": skipped, "failed": failed}


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


def build_screen_selection(
    config_path: Path,
    config: Dict[str, Any],
    screen_date: Optional[str] = None,
    xuangu_batch_id: Optional[str] = None,
) -> Dict[str, Any]:
    root = project_root(config_path)
    db_path = root / config["database"]["path"]

    with db.connect(db_path) as conn:
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

        return {
            "screen_date": effective_screen_date,
            "xuangu_batch_id": xuangu_batch_id,
            "candidate_count": len(candidates),
            "selected_count": len(selected),
            "ranked": ranked,
            "selected": selected,
        }


def stock_screen_preview_job(
    config_path: Path,
    config: Dict[str, Any],
    screen_date: Optional[str] = None,
    xuangu_batch_id: Optional[str] = None,
) -> Dict[str, Any]:
    if config["screening"].get("run_xuangu"):
        run_xuangu_download(config_path, config)
    return build_screen_selection(
        config_path=config_path,
        config=config,
        screen_date=screen_date,
        xuangu_batch_id=xuangu_batch_id,
    )


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

    result = build_screen_selection(
        config_path=config_path,
        config=config,
        screen_date=screen_date,
        xuangu_batch_id=xuangu_batch_id,
    )
    effective_screen_date = str(result["screen_date"])
    effective_batch_id = result["xuangu_batch_id"]
    ranked = result["ranked"]
    selected = result["selected"]

    with db.connect(db_path) as conn:
        db.ensure_weekly_tables(conn)
        if replace_existing:
            deleted = db.delete_screen_runs(conn, effective_screen_date, effective_batch_id)
            if deleted:
                print(f"Deleted {deleted} existing weekly screen run(s) for {effective_screen_date}/{effective_batch_id}.")

        run_id = db.create_screen_run(
            conn=conn,
            screen_date=effective_screen_date,
            xuangu_batch_id=effective_batch_id,
            config=config,
            screening_text=screening_text,
            candidate_count=int(result["candidate_count"]),
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

        review_cfg = config.get("review", {})
        if review_cfg.get("download_before_review", True):
            download_review_history_for_selected_stocks(
                db_path=db_path,
                selected_rows=selected,
                target_date=effective_review_date,
                config=config,
            )

        if replace_existing:
            deleted = db.delete_review_for_run(conn, reviewed_run_id)
            if deleted:
                print(f"Deleted {deleted} existing review run(s) for run_id={reviewed_run_id}.")

        review_id = db.create_review_run(conn, reviewed_run_id, effective_review_date, config)
        results = [
            review_selected_stock(
                db.load_klines(
                    conn,
                    row["code"],
                    limit=320,
                    as_of_date=effective_review_date,
                ),
                row,
                config,
            )
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
        screen_date = str(selected_rows[0]["screen_date"])

        train_codes = db.all_downloaded_codes(conn)
        if ml_cfg.get("train_on_selected_universe", False):
            train_codes = db.selected_codes_for_run(conn, source_run_id)
        klines_by_code = {
            code: db.load_klines(
                conn,
                code,
                limit=int(ml_cfg.get("history_limit", 320)),
                as_of_date=screen_date,
            )
            for code in train_codes
        }
        samples = build_training_samples(klines_by_code, ml_cfg)
        attach_context_features_to_samples(conn, samples)
        sample_weights: Optional[List[float]] = None
        if ml_cfg.get("use_review_feedback_labels", False):
            recent_runs = int(ml_cfg.get("review_feedback_recent_runs", 0))
            feedback = db.review_feedback_labels(
                conn,
                recent_runs=recent_runs,
                as_of_date=screen_date,
            )
            # A historical replay may only learn from reviews that were already
            # available before its screening date.
            feedback = {
                key: label
                for key, label in feedback.items()
                if str(key[1]) < screen_date
            }
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
        db.save_ml_training_samples(
            conn,
            model_run_id,
            source_run_id,
            samples,
            storage_mode=str(ml_cfg.get("training_sample_storage_mode", "zlib")),
            keep_model_runs=int(ml_cfg.get("training_sample_keep_model_runs", 3)),
        )

        # Build cross-sectional features over the same downloaded universe used
        # for training, then take the selected rows from that snapshot. Computing
        # ranks only inside Top-N gives the same feature a different meaning at
        # train and prediction time.
        prediction_universe: List[tuple[Any, Dict[str, float], str]] = []
        for code, klines in klines_by_code.items():
            if not klines or klines[-1].trade_date != screen_date:
                continue
            features = features_at(klines, len(klines) - 1)
            if features is None:
                continue
            prediction_universe.append(({"code": code}, features, screen_date))

        attach_context_features_to_prediction_items(conn, prediction_universe)

        if ml_cfg.get("use_cross_sectional_features", True):
            add_cross_sectional_to_predictions(
                [(trade_date, features) for _row, features, trade_date in prediction_universe]
            )

        universe_features = {
            str(row["code"]): (features, trade_date)
            for row, features, trade_date in prediction_universe
        }
        prediction_items: List[tuple[Any, Dict[str, float], str]] = []
        for row in selected_rows:
            item = universe_features.get(str(row["code"]))
            if item is not None:
                prediction_items.append((row, item[0], item[1]))

        rule_scores = normalized_rule_scores(
            selected_rows,
            enabled=bool(ml_cfg.get("normalize_rule_score_for_blend", True)),
        )
        predictions = []
        for row, features, feature_trade_date in prediction_items:
            code = str(row["code"])
            probability = model.predict_probability(features)
            baseline_probability = None
            if hasattr(model, "predict_baseline_probability"):
                baseline_probability = model.predict_baseline_probability(features)
            raw_rule_score = float(row["total_score"] or 0)
            rule_score = rule_scores.get(code, raw_rule_score)
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
                        "feature_trade_date": feature_trade_date,
                        "cross_section_size": len(prediction_universe),
                        "raw_rule_score": raw_rule_score,
                        "normalized_rule_score": rule_score,
                        "baseline_probability_up": round(baseline_probability, 4)
                        if baseline_probability is not None
                        else None,
                    },
                    "reason": model.explain(features),
                }
            )
        predictions.sort(key=lambda item: (item["predicted_score"], item["probability_up"]), reverse=True)
        db.save_ml_predictions(conn, model_run_id, source_run_id, predictions)

        # ML re-ranking: reorder selected stocks by predicted_score when enabled
        rerank_enabled = bool(ml_cfg.get("rerank_after_predict", False))
        if rerank_enabled:
            code_to_score = {p["code"]: p["predicted_score"] for p in predictions}
            selected_rows = db.selected_stocks_for_run(conn, source_run_id)
            reranked = sorted(
                selected_rows,
                key=lambda r: (code_to_score.get(str(r["code"]), 0.0),),
                reverse=True,
            )
            for new_rank, row in enumerate(reranked, start=1):
                conn.execute(
                    "UPDATE weekly_selected_stocks SET rank_no = ? WHERE id = ?",
                    (new_rank, int(row["id"])),
                )
            conn.commit()
            print(
                f"ML rerank completed: reordered {len(reranked)} selected stocks by predicted_score."
            )

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
        attach_context_features_to_samples(conn, samples)
        feedback = None
        if ml_cfg.get("use_review_feedback_labels", False):
            mode = str(ml_cfg.get("backtest_mode", "purged_walk_forward")).lower()
            if mode in {"purged_walk_forward", "walk_forward"}:
                folds = purged_walk_forward_splits(
                    samples,
                    train_ratio=float(ml_cfg.get("backtest_train_ratio", 0.7)),
                    fold_count=int(ml_cfg.get("backtest_walk_forward_folds", 5)),
                )
                feedback_cutoff = folds[0].test_start_date if folds else None
            else:
                _train_samples, test_samples = split_samples_by_time(
                    samples,
                    train_ratio=float(ml_cfg.get("backtest_train_ratio", 0.7)),
                )
                feedback_cutoff = min(
                    (sample.trade_date for sample in test_samples),
                    default=None,
                )
            feedback = db.review_feedback_labels(
                conn,
                recent_runs=int(ml_cfg.get("review_feedback_recent_runs", 0)),
                as_of_date=feedback_cutoff,
            )
        rule_candidates = db.rule_backtest_candidates(conn)
    min_samples = int(ml_cfg.get("min_train_samples", 30))
    if len(samples) < min_samples:
        raise RuntimeError(
            f"Not enough ML backtest samples: {len(samples)} < {min_samples}. "
            "Download more K-line history or lower ml.min_train_samples."
        )
    return backtest_models(
        samples,
        ml_cfg,
        feedback_labels=feedback,
        rule_candidates=rule_candidates,
    )


def review_selected_stock(klines: List[Kline], selected_row: Any, config: Dict[str, Any]) -> ReviewResult:
    screen_date = selected_row["screen_date"]
    horizon = int(config["review"]["horizon_trading_days"])
    stop_loss_fraction = float(config["review"]["stop_loss_pct"])
    stop_loss_pct = stop_loss_fraction * 100
    expected_high = float(config["review"]["expected_high_gain_pct"]) * 100
    expected_close = float(config["review"]["expected_close_gain_pct"]) * 100

    previous = [k for k in klines if k.trade_date <= screen_date and k.close is not None]
    future = [k for k in klines if k.trade_date > screen_date and k.close is not None][:horizon]
    if not previous or len(future) < horizon:
        last_trade_date = klines[-1].trade_date if klines else None
        reasons = []
        if not previous:
            reasons.append("缺少选股日及之前的收盘K线")
        if len(future) < horizon:
            reasons.append(f"选股日之后只有 {len(future)}/{horizon} 个交易日K线")
        if last_trade_date:
            reasons.append(f"当前最新K线日期={last_trade_date}")
        interval_note = (
            f"复盘区间 {future[0].trade_date} 到 {future[-1].trade_date}，"
            f"共 {len(future)}/{horizon} 个交易日；"
            if future
            else f"复盘区间尚未开始，共 0/{horizon} 个交易日；"
        )
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
            best_exit_meets_expectation=False,
            is_complete=False,
            notes="K线不足，复盘未完成；" + interval_note + "；".join(reasons),
        )

    base = previous[-1]
    base_idx = max(
        idx
        for idx, row in enumerate(klines)
        if row.trade_date <= screen_date and row.close is not None
    )
    outcome = simulate_trade(
        klines,
        base_idx,
        horizon,
        high_target_pct=float(config["review"]["expected_high_gain_pct"]),
        close_target_pct=float(config["review"]["expected_close_gain_pct"]),
        stop_loss_pct=stop_loss_fraction,
        target_logic=str(config["review"].get("positive_target_logic", "any")),
        use_exit_rules=bool(config["review"].get("use_trade_exit_rules", True)),
        exit_on_break_ma20=bool(config["review"].get("exit_on_break_ma20", False)),
    )
    if outcome is None:
        return ReviewResult(
            code=selected_row["code"],
            name=selected_row["name"],
            base_trade_date=base.trade_date,
            review_start_date=future[0].trade_date,
            review_end_date=future[-1].trade_date,
            highest_gain_pct=None,
            close_gain_pct=None,
            max_drawdown_pct=None,
            stop_loss_triggered=False,
            meets_expectation=False,
            best_exit_meets_expectation=False,
            is_complete=False,
            notes="K线字段不完整，复盘未完成",
        )

    notes = [
        f"复盘区间 {future[0].trade_date} 到 {future[-1].trade_date}，共 {len(future)}/{horizon} 个交易日",
        f"区间最高涨幅 {outcome.highest_gain_pct:.2f}%，"
        f"区间最大回撤 {outcome.max_drawdown_pct:.2f}%",
    ]
    success_reasons = []
    failure_reasons = []
    if outcome.high_target_hit:
        success_reasons.append(
            f"退出前触及止盈目标 {expected_high:.2f}%"
        )
    elif outcome.highest_gain_pct >= expected_high:
        failure_reasons.append(
            f"区间最高涨幅 {outcome.highest_gain_pct:.2f}% 达标，但发生在策略退出之后"
        )
    else:
        failure_reasons.append(
            f"最高涨幅 {outcome.highest_gain_pct:.2f}% 低于目标 {expected_high:.2f}%，"
            f"差 {expected_high - outcome.highest_gain_pct:.2f}%"
        )
    if outcome.close_target_hit:
        success_reasons.append(
            f"策略退出收益 {outcome.realized_gain_pct:.2f}% 达到目标 {expected_close:.2f}%"
        )
    else:
        failure_reasons.append(
            f"策略退出收益 {outcome.realized_gain_pct:.2f}% 低于目标 {expected_close:.2f}%"
        )
    if outcome.stop_loss_triggered:
        failure_reasons.append(f"触发止损线 {-stop_loss_pct:.2f}%")
    else:
        success_reasons.append(
            f"策略退出前未触发止损线 {-stop_loss_pct:.2f}%"
        )

    exit_labels = {
        "take_profit": "止盈",
        "stop_loss": "止损",
        "break_ma20": "跌破MA20",
        "horizon": "持有期结束",
    }
    notes.append(
        f"退出方式：{exit_labels.get(outcome.exit_reason, outcome.exit_reason)}，"
        f"退出日期 {outcome.exit_trade_date}，策略收益 {outcome.realized_gain_pct:.2f}%"
    )

    if outcome.label:
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
        highest_gain_pct=outcome.highest_gain_pct,
        close_gain_pct=outcome.realized_gain_pct,
        max_drawdown_pct=outcome.max_drawdown_pct,
        stop_loss_triggered=outcome.stop_loss_triggered,
        meets_expectation=bool(outcome.label),
        best_exit_meets_expectation=outcome.best_exit_target_hit,
        is_complete=True,
        notes="；".join(notes),
    )
