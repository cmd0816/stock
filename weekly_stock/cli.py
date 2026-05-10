from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean

from . import db
from .config import load_config
from .jobs import ml_backtest_job, ml_predict_job, project_root, stock_screen_job, weekly_review_job


def print_ml_predictions(config_path: Path, config: dict, model_run_id: int) -> None:
    root = project_root(config_path)
    db_path = root / config["database"]["path"]
    with db.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT model_run_id, source_run_id, train_sample_count, positive_sample_count
            FROM weekly_ml_model_runs
            WHERE model_run_id = ?
            """,
            (model_run_id,),
        ).fetchone()
        if row is None:
            print("No ML model run found.")
            return
        predictions = db.ml_predictions_for_run(conn, int(row["source_run_id"]))

    train_count = int(row["train_sample_count"])
    positive_count = int(row["positive_sample_count"])
    positive_rate = positive_count / train_count * 100 if train_count else 0
    print(
        f"ML model_run_id={row['model_run_id']} source_run_id={row['source_run_id']} "
        f"train_samples={train_count} positive_rate={positive_rate:.1f}%"
    )
    if not predictions:
        print("No ML predictions generated.")
        return

    print("rank code   name        prob_up baseline predicted_score reason")
    for rank, pred in enumerate(predictions, start=1):
        code = str(pred["code"])
        name = str(pred["name"] or "")[:8]
        prob = float(pred["probability_up"]) * 100
        score = float(pred["predicted_score"])
        try:
            features = json.loads(str(pred["feature_json"] or "{}"))
        except Exception:
            features = {}
        baseline = features.get("baseline_probability_up")
        baseline_txt = f"{float(baseline) * 100:>6.1f}%" if baseline is not None else "     -"
        reason = str(pred["reason"] or "").replace("\n", " ")[:80]
        print(f"{rank:>4} {code:<6} {name:<8} {prob:>6.1f}% {baseline_txt} {score:>15.2f} {reason}")


def print_screen_runs(config_path: Path, config: dict, limit: int) -> None:
    root = project_root(config_path)
    db_path = root / config["database"]["path"]
    with db.connect(db_path) as conn:
        db.ensure_weekly_tables(conn)
        rows = db.screen_runs(conn, limit=limit)
    if not rows:
        print("No weekly screen runs found.")
        return
    print("run_id screen_date  batch_id  candidates selected ml_predictions model     created_at")
    for row in rows:
        print(
            f"{row['run_id']:>6} "
            f"{str(row['screen_date'] or '-'):<11} "
            f"{str(row['xuangu_batch_id'] or '-'):<9} "
            f"{int(row['candidate_count'] or 0):>10} "
            f"{int(row['selected_count'] or 0):>8} "
            f"{int(row['ml_prediction_count'] or 0):>14} "
            f"{str(row['latest_model_name'] or '-'):>9} "
            f"{row['created_at_utc']}"
        )


def print_backtest_metrics(metrics: list) -> None:
    if not metrics:
        print("No ML backtest metrics generated.")
        return
    print("model               train test positive accuracy precision recall top_k_hit avg_close avg_high avg_drawdown")
    for item in metrics:
        print(
            f"{item.model_name:<18} "
            f"{item.train_count:>5} "
            f"{item.test_count:>4} "
            f"{item.positive_rate * 100:>7.1f}% "
            f"{item.accuracy * 100:>7.1f}% "
            f"{item.precision * 100:>8.1f}% "
            f"{item.recall * 100:>5.1f}% "
            f"{item.top_k_hit_rate * 100:>8.1f}% "
            f"{item.top_k_avg_close_gain_pct:>8.2f}% "
            f"{item.top_k_avg_high_gain_pct:>7.2f}% "
            f"{item.top_k_avg_max_drawdown_pct:>11.2f}%"
        )


def avg_or_zero(values: list[float]) -> float:
    return mean(values) if values else 0.0


def print_trend_summary(rows: list, window: int) -> None:
    if len(rows) < window * 2:
        print(f"Need at least {window * 2} reviewed runs for rolling trend comparison; current={len(rows)}.")
        return

    recent = rows[:window]
    previous = rows[window : window * 2]

    def metric(items: list, key: str) -> float:
        values = [float(row[key]) for row in items if row[key] is not None]
        return avg_or_zero(values)

    hit_recent = metric(recent, "hit_rate")
    hit_prev = metric(previous, "hit_rate")
    close_recent = metric(recent, "avg_close_gain_pct")
    close_prev = metric(previous, "avg_close_gain_pct")
    drawdown_recent = metric(recent, "avg_max_drawdown_pct")
    drawdown_prev = metric(previous, "avg_max_drawdown_pct")

    print(
        "rolling "
        f"{window}-run: "
        f"hit={hit_recent * 100:.1f}% ({(hit_recent - hit_prev) * 100:+.1f}ppt), "
        f"avg_close={close_recent:.2f}% ({close_recent - close_prev:+.2f}%), "
        f"avg_drawdown={drawdown_recent:.2f}% ({drawdown_recent - drawdown_prev:+.2f}%)"
    )


def print_review_trend(config_path: Path, config: dict, limit: int, window: int) -> None:
    root = project_root(config_path)
    db_path = root / config["database"]["path"]
    with db.connect(db_path) as conn:
        db.ensure_weekly_tables(conn)
        rows = db.review_trend_runs(conn, limit=limit)
    if not rows:
        print("No reviewed weekly runs found.")
        return

    print("run_id screen_date selected reviewed hit stop_loss avg_close avg_high avg_drawdown ml_pred avg_prob")
    for row in rows:
        hit = float(row["hit_rate"] or 0) * 100
        stop_loss = float(row["stop_loss_rate"] or 0) * 100
        avg_close = float(row["avg_close_gain_pct"] or 0)
        avg_high = float(row["avg_high_gain_pct"] or 0)
        avg_drawdown = float(row["avg_max_drawdown_pct"] or 0)
        avg_prob = float(row["avg_probability_up"] or 0) * 100
        print(
            f"{int(row['run_id']):>6} "
            f"{str(row['screen_date'] or '-'):<11} "
            f"{int(row['selected_count'] or 0):>8} "
            f"{int(row['reviewed_count'] or 0):>8} "
            f"{hit:>5.1f}% "
            f"{stop_loss:>9.1f}% "
            f"{avg_close:>8.2f}% "
            f"{avg_high:>7.2f}% "
            f"{avg_drawdown:>11.2f}% "
            f"{int(row['ml_prediction_count'] or 0):>7} "
            f"{avg_prob:>7.1f}%"
        )
    print_trend_summary(rows, window)


def main() -> None:
    parser = argparse.ArgumentParser(description="Weekly stock screening and review jobs.")
    parser.add_argument("--config", default="config/weekly_strategy.yaml", help="YAML strategy config path")
    sub = parser.add_subparsers(dest="command", required=True)

    screen = sub.add_parser("screen", help="Run stock_screen_job")
    screen.add_argument("--date", default=None, help="Screen date, YYYY-MM-DD")
    screen.add_argument("--xuangu-batch-id", default=None, help="Use a specific xuangu batch id")
    screen.add_argument("--run-xuangu", action="store_true", help="Run xuangu download before scoring")
    screen.add_argument("--replace-existing", action="store_true", help="Replace existing screen run for the same date/batch")

    review = sub.add_parser("review", help="Run weekly_review_job")
    review.add_argument("--date", default=None, help="Review date, YYYY-MM-DD")
    review.add_argument("--run-id", type=int, default=None, help="Review a specific weekly screen run")

    predict = sub.add_parser("predict", help="Run ML prediction/re-ranking for selected stocks")
    predict.add_argument("--run-id", type=int, default=None, help="Predict a specific weekly screen run")

    runs = sub.add_parser("runs", help="List historical weekly screen runs and run_id values")
    runs.add_argument("--limit", type=int, default=20, help="Max runs to list")

    sub.add_parser("backtest", help="Run time-split ML backtest for baseline and main model")
    trend = sub.add_parser("trend", help="Show reviewed run performance trend and rolling comparison")
    trend.add_argument("--limit", type=int, default=20, help="How many reviewed runs to show")
    trend.add_argument("--window", type=int, default=4, help="Rolling window size for trend delta")

    args = parser.parse_args()
    config_path = Path(args.config)
    config = load_config(config_path)

    if args.command == "screen":
        if args.run_xuangu:
            config["screening"]["run_xuangu"] = True
        run_id = stock_screen_job(
            config_path,
            config,
            screen_date=args.date,
            xuangu_batch_id=args.xuangu_batch_id,
            replace_existing=args.replace_existing,
        )
        print(f"stock_screen_job completed: run_id={run_id}")
    elif args.command == "review":
        review_id = weekly_review_job(config_path, config, review_date=args.date, run_id=args.run_id)
        print(f"weekly_review_job completed: review_id={review_id}")
    elif args.command == "predict":
        model_run_id = ml_predict_job(config_path, config, run_id=args.run_id)
        print(f"ml_predict_job completed: model_run_id={model_run_id}")
        print_ml_predictions(config_path, config, model_run_id)
    elif args.command == "runs":
        print_screen_runs(config_path, config, args.limit)
    elif args.command == "backtest":
        metrics = ml_backtest_job(config_path, config)
        print_backtest_metrics(metrics)
    elif args.command == "trend":
        print_review_trend(config_path, config, args.limit, max(1, args.window))


if __name__ == "__main__":
    main()
