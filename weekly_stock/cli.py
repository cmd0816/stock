from __future__ import annotations

import argparse
from pathlib import Path

from .config import load_config
from .jobs import stock_screen_job, weekly_review_job


def main() -> None:
    parser = argparse.ArgumentParser(description="Weekly stock screening and review jobs.")
    parser.add_argument("--config", default="config/weekly_strategy.yaml", help="YAML strategy config path")
    sub = parser.add_subparsers(dest="command", required=True)

    screen = sub.add_parser("screen", help="Run stock_screen_job")
    screen.add_argument("--date", default=None, help="Screen date, YYYY-MM-DD")
    screen.add_argument("--xuangu-batch-id", default=None, help="Use a specific xuangu batch id")
    screen.add_argument("--run-xuangu", action="store_true", help="Run xuangu download before scoring")

    review = sub.add_parser("review", help="Run weekly_review_job")
    review.add_argument("--date", default=None, help="Review date, YYYY-MM-DD")
    review.add_argument("--run-id", type=int, default=None, help="Review a specific weekly screen run")

    args = parser.parse_args()
    config_path = Path(args.config)
    config = load_config(config_path)

    if args.command == "screen":
        if args.run_xuangu:
            config["screening"]["run_xuangu"] = True
        run_id = stock_screen_job(config_path, config, screen_date=args.date, xuangu_batch_id=args.xuangu_batch_id)
        print(f"stock_screen_job completed: run_id={run_id}")
    elif args.command == "review":
        review_id = weekly_review_job(config_path, config, review_date=args.date, run_id=args.run_id)
        print(f"weekly_review_job completed: review_id={review_id}")


if __name__ == "__main__":
    main()
