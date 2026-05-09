import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from weekly_stock.config import DEFAULT_CONFIG
from weekly_stock.db import connect, ensure_weekly_tables
from weekly_stock.jobs import ml_predict_job, review_selected_stock, stock_screen_job
from weekly_stock.models import Kline


def write_config(root: Path) -> Path:
    config_dir = root / "config"
    config_dir.mkdir()
    config_path = config_dir / "weekly_strategy.yaml"
    config_path.write_text(
        """
database:
  path: stocks.db
paths:
  screening_file: screening.txt
  download_dir: downloads
screening:
  top_n: 3
  min_score: 0
  run_xuangu: false
""",
        encoding="utf-8",
    )
    return config_path


def create_source_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE xuangu_batches (
            batch_id TEXT PRIMARY KEY,
            imported_at_utc TEXT NOT NULL,
            source_url TEXT NOT NULL,
            condition_text TEXT,
            xlsx_path TEXT NOT NULL,
            sheet_count INTEGER NOT NULL,
            row_count INTEGER NOT NULL
        );
        CREATE TABLE xuangu_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT NOT NULL,
            imported_at_utc TEXT NOT NULL,
            condition_text TEXT,
            source_url TEXT NOT NULL,
            source_file TEXT NOT NULL,
            sheet_name TEXT NOT NULL,
            row_no INTEGER NOT NULL,
            stock_code TEXT,
            stock_name TEXT,
            row_json TEXT NOT NULL
        );
        CREATE TABLE eastmoney_stock_daily_klines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_url TEXT NOT NULL,
            market INTEGER NOT NULL,
            code TEXT NOT NULL,
            secid TEXT NOT NULL,
            name TEXT,
            trade_date TEXT NOT NULL,
            open REAL,
            close REAL,
            high REAL,
            low REAL,
            volume REAL,
            turnover REAL,
            amplitude_percent REAL,
            change_percent REAL,
            change_amount REAL,
            turnover_rate REAL,
            raw_line TEXT NOT NULL,
            fetched_at_utc TEXT NOT NULL,
            UNIQUE(code, trade_date)
        );
        """
    )


def insert_candidate(conn: sqlite3.Connection, code: str, name: str, row_json: dict) -> None:
    conn.execute(
        """
        INSERT INTO xuangu_results (
            batch_id, imported_at_utc, condition_text, source_url, source_file,
            sheet_name, row_no, stock_code, stock_name, row_json
        ) VALUES ('b1', '2026-05-01T00:00:00Z', 'cond', 'url', 'file', 'Sheet1', 1, ?, ?, ?)
        """,
        (code, name, json.dumps(row_json, ensure_ascii=False)),
    )


def insert_kline(conn: sqlite3.Connection, code: str, day: int, close: float, volume: float = 1000, turnover_rate: float = 5) -> None:
    trade_date = f"2026-04-{day:02d}" if day <= 30 else f"2026-05-{day-30:02d}"
    conn.execute(
        """
        INSERT INTO eastmoney_stock_daily_klines (
            source_url, market, code, secid, name, trade_date, open, close, high, low,
            volume, turnover, amplitude_percent, change_percent, change_amount,
            turnover_rate, raw_line, fetched_at_utc
        ) VALUES ('url', 0, ?, ?, '测试', ?, ?, ?, ?, ?, ?, 0, 0, 1, 0, ?, 'raw', 'now')
        """,
        (code, f"0.{code}", trade_date, close - 0.2, close, close + 0.5, close - 0.5, volume, turnover_rate),
    )


class WeeklyStockTests(unittest.TestCase):
    def test_stock_screen_job_saves_top_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            (root / "screening.txt").write_text("测试条件", encoding="utf-8")
            with connect(root / "stocks.db") as conn:
                create_source_tables(conn)
                ensure_weekly_tables(conn)
                conn.execute(
                    "INSERT INTO xuangu_batches VALUES ('b1', '2026-05-01T00:00:00Z', 'url', 'cond', 'file', 1, 2)"
                )
                insert_candidate(conn, "000001", "强势股份", {"营业收入同比增长率": "20%", "净利润同比增长率": "15%"})
                insert_candidate(conn, "000002", "普通股份", {"营业收入同比增长率": "5%", "净利润同比增长率": "3%"})
                for i in range(1, 36):
                    insert_kline(conn, "000001", i, 10 + i * 0.2, volume=1000 + i * 20)
                    insert_kline(conn, "000002", i, 10 + i * 0.05, volume=1000)
                conn.commit()

            run_id = stock_screen_job(config_path, DEFAULT_CONFIG | {"database": {"path": "stocks.db"}}, screen_date="2026-05-02", xuangu_batch_id="b1")

            with connect(root / "stocks.db") as conn:
                selected = conn.execute(
                    "SELECT code, selected_reason FROM weekly_selected_stocks WHERE run_id=? ORDER BY rank_no",
                    (run_id,),
                ).fetchall()
                self.assertGreaterEqual(len(selected), 2)
                self.assertEqual(selected[0]["code"], "000001")
                self.assertIn("风险过滤", selected[0]["selected_reason"])

    def test_stock_screen_job_replace_existing_keeps_latest_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            (root / "screening.txt").write_text("测试条件", encoding="utf-8")
            with connect(root / "stocks.db") as conn:
                create_source_tables(conn)
                ensure_weekly_tables(conn)
                conn.execute(
                    "INSERT INTO xuangu_batches VALUES ('b1', '2026-05-01T00:00:00Z', 'url', 'cond', 'file', 1, 2)"
                )
                insert_candidate(conn, "000001", "强势股份", {"营业收入同比增长率": "20%"})
                insert_candidate(conn, "000002", "普通股份", {"营业收入同比增长率": "5%"})
                for i in range(1, 36):
                    insert_kline(conn, "000001", i, 10 + i * 0.2)
                    insert_kline(conn, "000002", i, 10 + i * 0.05)
                conn.commit()

            first_run_id = stock_screen_job(
                config_path,
                DEFAULT_CONFIG | {"database": {"path": "stocks.db"}},
                screen_date="2026-05-02",
                xuangu_batch_id="b1",
                replace_existing=True,
            )
            second_run_id = stock_screen_job(
                config_path,
                DEFAULT_CONFIG | {"database": {"path": "stocks.db"}},
                screen_date="2026-05-02",
                xuangu_batch_id="b1",
                replace_existing=True,
            )

            with connect(root / "stocks.db") as conn:
                runs = conn.execute("SELECT run_id FROM weekly_screen_runs ORDER BY run_id").fetchall()
                selected = conn.execute("SELECT DISTINCT run_id FROM weekly_selected_stocks").fetchall()
            self.assertNotEqual(first_run_id, second_run_id)
            self.assertEqual([row["run_id"] for row in runs], [second_run_id])
            self.assertEqual([row["run_id"] for row in selected], [second_run_id])

    def test_review_selected_stock_metrics(self) -> None:
        klines = [
            Kline("2026-05-01", 10, 10, 10.2, 9.8, 1000, 5, 0),
            Kline("2026-05-04", 10, 10.5, 11.0, 9.7, 1200, 6, 5),
            Kline("2026-05-05", 10.5, 10.8, 11.2, 10.1, 1300, 7, 3),
        ]
        selected = {"code": "000001", "name": "测试", "screen_date": "2026-05-02"}
        result = review_selected_stock(klines, selected, DEFAULT_CONFIG)
        self.assertAlmostEqual(result.highest_gain_pct or 0, 12.0)
        self.assertFalse(result.stop_loss_triggered)
        self.assertTrue(result.meets_expectation)

    def test_ml_predict_job_saves_predictions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            (root / "screening.txt").write_text("测试条件", encoding="utf-8")
            with connect(root / "stocks.db") as conn:
                create_source_tables(conn)
                ensure_weekly_tables(conn)
                conn.execute(
                    "INSERT INTO xuangu_batches VALUES ('b1', '2026-05-01T00:00:00Z', 'url', 'cond', 'file', 1, 2)"
                )
                insert_candidate(conn, "000001", "强势股份", {"营业收入同比增长率": "20%", "净利润同比增长率": "15%"})
                insert_candidate(conn, "000002", "普通股份", {"营业收入同比增长率": "5%", "净利润同比增长率": "3%"})
                for i in range(1, 96):
                    insert_kline(conn, "000001", i, 10 + i * 0.08, volume=1000 + i * 10)
                    insert_kline(conn, "000002", i, 12 + ((i % 8) - 4) * 0.03, volume=900 + (i % 5) * 15)
                conn.commit()

            config = DEFAULT_CONFIG | {
                "database": {"path": "stocks.db"},
                "screening": {"top_n": 2, "min_score": 0, "run_xuangu": False},
                "ml": {
                    **DEFAULT_CONFIG["ml"],
                    "model_name": "centroid_v1",
                    "min_train_samples": 5,
                    "lookback_trading_days": 60,
                    "sample_stride": 5,
                    "history_limit": 120,
                },
            }
            run_id = stock_screen_job(config_path, config, screen_date="2026-05-02", xuangu_batch_id="b1")
            model_run_id = ml_predict_job(config_path, config, run_id=run_id)

            with connect(root / "stocks.db") as conn:
                models = conn.execute("SELECT * FROM weekly_ml_model_runs WHERE model_run_id=?", (model_run_id,)).fetchall()
                predictions = conn.execute(
                    "SELECT code, probability_up, predicted_score FROM weekly_ml_predictions WHERE source_run_id=?",
                    (run_id,),
                ).fetchall()
            self.assertEqual(len(models), 1)
            self.assertGreaterEqual(len(predictions), 1)
            self.assertTrue(all(0 <= row["probability_up"] <= 1 for row in predictions))


if __name__ == "__main__":
    unittest.main()
