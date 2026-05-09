from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import db
from .config import load_config
from .jobs import ml_predict_job, project_root, stock_screen_job, weekly_review_job


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


if __name__ == "__main__":
    main()
