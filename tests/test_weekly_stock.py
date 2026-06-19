import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from weekly_stock.config import DEFAULT_CONFIG
from weekly_stock.db import connect, ensure_weekly_tables, review_feedback_labels
from weekly_stock.jobs import (
    apply_review_feedback_labels,
    download_review_history_for_selected_stocks,
    ml_backtest_job,
    ml_predict_job,
    review_selected_stock,
    stock_screen_job,
)
from weekly_stock.ml import TrainingSample, build_training_samples, label_future
from weekly_stock.models import Kline
from weekly_stock.trading_calendar import align_to_last_trading_day, weekly_last_trading_days


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

    def test_stock_screen_job_uses_screen_date_klines_cutoff(self) -> None:
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
                insert_candidate(conn, "000001", "回放A", {"营业收入同比增长率": "20%", "净利润同比增长率": "20%"})
                insert_candidate(conn, "000002", "回放B", {"营业收入同比增长率": "20%", "净利润同比增长率": "20%"})

                # Screen date will be 2026-05-02. Before that date, both stocks have similar history.
                for i in range(1, 32):
                    insert_kline(conn, "000001", i, 10 + i * 0.1, volume=1000)
                    insert_kline(conn, "000002", i, 10 + i * 0.1, volume=1000)
                # Add large post-date jump only for 000002. It should not affect 2026-05-02 screening.
                insert_kline(conn, "000002", 33, 30, volume=5000)
                insert_kline(conn, "000002", 34, 35, volume=5000)
                conn.commit()

            run_id = stock_screen_job(
                config_path,
                DEFAULT_CONFIG | {"database": {"path": "stocks.db"}},
                screen_date="2026-05-02",
                xuangu_batch_id="b1",
            )
            with connect(root / "stocks.db") as conn:
                rows = conn.execute(
                    "SELECT code, selected_reason FROM weekly_selected_stocks WHERE run_id=? ORDER BY rank_no",
                    (run_id,),
                ).fetchall()
            self.assertTrue(rows)
            self.assertIn("最近交易日 2026-05-01", rows[0]["selected_reason"])

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
        self.assertIn("成功原因", result.notes)
        self.assertIn("最高涨幅", result.notes)

    def test_review_selected_stock_failure_reasons(self) -> None:
        klines = [
            Kline("2026-05-01", 10, 10, 10.2, 9.8, 1000, 5, 0),
            Kline("2026-05-04", 10, 9.8, 10.2, 9.3, 1200, 6, -2),
            Kline("2026-05-05", 9.8, 9.6, 10.1, 9.2, 1300, 7, -2),
        ]
        selected = {"code": "000001", "name": "测试", "screen_date": "2026-05-02"}
        result = review_selected_stock(klines, selected, DEFAULT_CONFIG)
        self.assertFalse(result.meets_expectation)
        self.assertTrue(result.stop_loss_triggered)
        self.assertIn("失败原因", result.notes)
        self.assertIn("触发止损线", result.notes)

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
                    "weekly_last_trading_day_only": False,
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
                samples = conn.execute(
                    "SELECT code, label, feature_json FROM weekly_ml_training_samples WHERE model_run_id=?",
                    (model_run_id,),
                ).fetchall()
            self.assertEqual(len(models), 1)
            self.assertGreaterEqual(len(predictions), 1)
            self.assertGreaterEqual(len(samples), 1)
            self.assertTrue(all(0 <= row["probability_up"] <= 1 for row in predictions))

    def test_apply_review_feedback_labels_relabels_and_weights(self) -> None:
        samples = [
            TrainingSample(
                code="000001",
                trade_date="2026-05-01",
                features={"ret_5": 1.0},
                label=0,
                future_high_gain_pct=1.0,
                future_close_gain_pct=1.0,
                future_max_drawdown_pct=-1.0,
            ),
            TrainingSample(
                code="000002",
                trade_date="2026-05-01",
                features={"ret_5": -1.0},
                label=1,
                future_high_gain_pct=2.0,
                future_close_gain_pct=2.0,
                future_max_drawdown_pct=-2.0,
            ),
        ]
        merged, weights, stats = apply_review_feedback_labels(
            samples,
            {("000001", "2026-05-01"): 1},
            weight=3,
        )
        self.assertEqual(stats["matched"], 1)
        self.assertEqual(stats["relabeled"], 1)
        self.assertEqual(stats["extra_weighted"], 2)
        relabeled = [s for s in merged if s.code == "000001" and s.trade_date == "2026-05-01"]
        self.assertEqual(len(relabeled), 1)
        self.assertTrue(all(s.label == 1 for s in relabeled))
        self.assertEqual(len(weights), len(merged))
        self.assertEqual(weights, [3.0, 1.0])

    def test_review_history_download_uses_all_selected_stocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "stocks.db"
            with connect(db_path) as conn:
                create_source_tables(conn)
                ensure_weekly_tables(conn)
                run_id = conn.execute(
                    """
                    INSERT INTO weekly_screen_runs (
                        screen_date, xuangu_batch_id, strategy_config_json, screening_text,
                        candidate_count, selected_count, created_at_utc
                    ) VALUES ('2026-05-01', 'b1', '{}', '', 6, 6, 'now')
                    """
                ).lastrowid
                for idx in range(1, 7):
                    conn.execute(
                        """
                        INSERT INTO weekly_selected_stocks (
                            run_id, screen_date, code, name, rank_no, total_score, selected_reason, created_at_utc
                        ) VALUES (?, '2026-05-01', ?, ?, ?, 80, 'test', 'now')
                        """,
                        (run_id, f"00000{idx}", f"测试{idx}", idx),
                    )
                conn.commit()
                selected_rows = conn.execute(
                    "SELECT * FROM weekly_selected_stocks WHERE run_id=? ORDER BY rank_no",
                    (run_id,),
                ).fetchall()

            import download_top_history_akshare as downloader

            called_codes = []
            original_fetch = downloader.fetch_kline_with_akshare
            original_save = downloader.save_akshare_kline_rows

            class FakeFrame:
                def to_dict(self, orient: str) -> list[dict]:
                    if orient != "records":
                        raise AssertionError(orient)
                    return [{"date": "2026-05-08", "open": 10, "close": 11, "high": 11, "low": 10}]

            def fake_fetch(symbol: str, start: str, end: str, adjust: str, source: str = "auto"):
                called_codes.append(symbol)
                return FakeFrame(), "fake"

            def fake_save(db_path_arg: Path, market: int, code: str, name: str, rows: list[dict], source_name: str = "fake") -> int:
                return len(rows)

            try:
                downloader.fetch_kline_with_akshare = fake_fetch
                downloader.save_akshare_kline_rows = fake_save
                stats = download_review_history_for_selected_stocks(
                    db_path,
                    selected_rows,
                    "2026-05-08",
                    {
                        **DEFAULT_CONFIG,
                        "review": {
                            **DEFAULT_CONFIG["review"],
                            "download_min_existing_days": 200,
                        },
                    },
                )
            finally:
                downloader.fetch_kline_with_akshare = original_fetch
                downloader.save_akshare_kline_rows = original_save

            self.assertEqual(stats["total"], 6)
            self.assertEqual(stats["updated"], 6)
            self.assertEqual(called_codes, [f"00000{idx}" for idx in range(1, 7)])

    def test_review_feedback_labels_skip_incomplete_reviews(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "stocks.db"
            with connect(db_path) as conn:
                create_source_tables(conn)
                ensure_weekly_tables(conn)
                run_id = conn.execute(
                    """
                    INSERT INTO weekly_screen_runs (
                        screen_date, xuangu_batch_id, strategy_config_json, screening_text,
                        candidate_count, selected_count, created_at_utc
                    ) VALUES ('2026-05-01', 'b1', '{}', '', 2, 2, 'now')
                    """
                ).lastrowid
                selected_ids = []
                for idx, code in enumerate(("000001", "000002"), start=1):
                    selected_ids.append(
                        conn.execute(
                            """
                            INSERT INTO weekly_selected_stocks (
                                run_id, screen_date, code, name, rank_no, total_score, selected_reason, created_at_utc
                            ) VALUES (?, '2026-05-01', ?, '测试', ?, 80, 'test', 'now')
                            """,
                            (run_id, code, idx),
                        ).lastrowid
                    )
                review_id = conn.execute(
                    """
                    INSERT INTO weekly_review_runs (reviewed_run_id, review_date, config_json, created_at_utc)
                    VALUES (?, '2026-05-08', '{}', 'now')
                    """,
                    (run_id,),
                ).lastrowid
                conn.execute(
                    """
                    INSERT INTO weekly_review_results (
                        review_id, selected_id, code, name, base_trade_date, review_start_date, review_end_date,
                        highest_gain_pct, close_gain_pct, max_drawdown_pct, stop_loss_triggered,
                        meets_expectation, best_exit_meets_expectation, notes, created_at_utc
                    ) VALUES (?, ?, '000001', '完整', '2026-05-01', '2026-05-04', '2026-05-08',
                        6, 3, -2, 0, 1, 1, 'ok', 'now')
                    """,
                    (review_id, selected_ids[0]),
                )
                conn.execute(
                    """
                    INSERT INTO weekly_review_results (
                        review_id, selected_id, code, name, base_trade_date, review_start_date, review_end_date,
                        highest_gain_pct, close_gain_pct, max_drawdown_pct, stop_loss_triggered,
                        meets_expectation, best_exit_meets_expectation, notes, created_at_utc
                    ) VALUES (?, ?, '000002', '缺K', '2026-05-01', NULL, NULL,
                        NULL, NULL, NULL, 0, 0, 0, 'K线不足', 'now')
                    """,
                    (review_id, selected_ids[1]),
                )
                conn.commit()
                labels = review_feedback_labels(conn)

            self.assertEqual(labels, {("000001", "2026-05-01"): 1})

    def test_ml_predict_job_can_apply_review_feedback_labels(self) -> None:
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

            base_ml_cfg = {
                **DEFAULT_CONFIG["ml"],
                "model_name": "centroid_v1",
                "min_train_samples": 5,
                "lookback_trading_days": 60,
                "sample_stride": 5,
                "history_limit": 120,
                "weekly_last_trading_day_only": False,
            }
            base_config = DEFAULT_CONFIG | {
                "database": {"path": "stocks.db"},
                "screening": {"top_n": 2, "min_score": 0, "run_xuangu": False},
                "ml": base_ml_cfg,
            }
            run_id = stock_screen_job(config_path, base_config, screen_date="2026-05-02", xuangu_batch_id="b1")

            with connect(root / "stocks.db") as conn:
                selected_row = conn.execute(
                    "SELECT id, code FROM weekly_selected_stocks WHERE run_id=? ORDER BY rank_no LIMIT 1",
                    (run_id,),
                ).fetchone()
                self.assertIsNotNone(selected_row)
                code = str(selected_row["code"])
                klines = conn.execute(
                    """
                    SELECT trade_date, open, close, high, low, volume, turnover_rate, change_percent
                    FROM eastmoney_stock_daily_klines
                    WHERE code = ?
                    ORDER BY trade_date
                    """,
                    (code,),
                ).fetchall()
                klines_obj = [
                    Kline(
                        trade_date=str(r["trade_date"]),
                        open=r["open"],
                        close=r["close"],
                        high=r["high"],
                        low=r["low"],
                        volume=r["volume"],
                        turnover_rate=r["turnover_rate"],
                        change_percent=r["change_percent"],
                    )
                    for r in klines
                ]
                samples = build_training_samples({code: klines_obj}, base_ml_cfg)
                self.assertGreater(len(samples), 0)
                target_sample = samples[0]
                flipped_label = 0 if int(target_sample.label) == 1 else 1
                review_id = conn.execute(
                    """
                    INSERT INTO weekly_review_runs (reviewed_run_id, review_date, config_json, created_at_utc)
                    VALUES (?, '2026-05-20', '{}', '2026-05-20T00:00:00Z')
                    """,
                    (run_id,),
                ).lastrowid
                conn.execute(
                    """
                    INSERT INTO weekly_review_results (
                        review_id, selected_id, code, name, base_trade_date, review_start_date, review_end_date,
                        highest_gain_pct, close_gain_pct, max_drawdown_pct, stop_loss_triggered, meets_expectation,
                        notes, created_at_utc
                    ) VALUES (?, ?, ?, '测试', ?, '2026-05-21', '2026-05-25', 0, 0, 0, 0, ?, 'test', '2026-05-20T00:00:00Z')
                    """,
                    (review_id, int(selected_row["id"]), code, target_sample.trade_date, flipped_label),
                )
                conn.commit()

            config_with_feedback = DEFAULT_CONFIG | {
                "database": {"path": "stocks.db"},
                "screening": {"top_n": 2, "min_score": 0, "run_xuangu": False},
                "ml": {
                    **base_ml_cfg,
                    "use_review_feedback_labels": True,
                    "review_feedback_weight": 1,
                    "review_feedback_recent_runs": 0,
                },
            }
            model_run_id = ml_predict_job(config_path, config_with_feedback, run_id=run_id)

            with connect(root / "stocks.db") as conn:
                row = conn.execute(
                    """
                    SELECT label
                    FROM weekly_ml_training_samples
                    WHERE model_run_id = ? AND code = ? AND trade_date = ?
                    LIMIT 1
                    """,
                    (model_run_id, code, target_sample.trade_date),
                ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(int(row["label"]), int(flipped_label))

    def test_ml_backtest_job_returns_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            (root / "screening.txt").write_text("测试条件", encoding="utf-8")
            with connect(root / "stocks.db") as conn:
                create_source_tables(conn)
                ensure_weekly_tables(conn)
                for i in range(1, 110):
                    insert_kline(conn, "000001", i, 10 + i * 0.08, volume=1000 + i * 10)
                    insert_kline(conn, "000002", i, 12 + ((i % 8) - 4) * 0.03, volume=900 + (i % 5) * 15)
                conn.commit()

            config = DEFAULT_CONFIG | {
                "database": {"path": "stocks.db"},
                "ml": {
                    **DEFAULT_CONFIG["ml"],
                    "model_name": "centroid_v1",
                    "baseline_model_name": "none",
                    "min_train_samples": 5,
                    "lookback_trading_days": 60,
                    "sample_stride": 5,
                    "history_limit": 130,
                    "backtest_top_k": 3,
                    "weekly_last_trading_day_only": False,
                },
            }
            metrics = ml_backtest_job(config_path, config)
            self.assertEqual(len(metrics), 1)
            self.assertEqual(metrics[0].model_name, "centroid_v1")
            self.assertGreater(metrics[0].test_count, 0)

    def test_trading_calendar_aligns_to_latest_trade_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with connect(root / "stocks.db") as conn:
                create_source_tables(conn)
                insert_kline(conn, "000001", 28, 10)
                insert_kline(conn, "000001", 29, 10)
                insert_kline(conn, "000001", 30, 10)
                conn.commit()
                aligned = align_to_last_trading_day("2026-05-09", conn=conn, prefer_akshare=False)
            self.assertEqual(aligned, "2026-04-30")

    def test_ml_samples_can_use_weekly_last_trading_days_only(self) -> None:
        klines = [
            Kline(f"2026-01-{day:02d}", 10, 10 + day * 0.1, 10 + day * 0.2, 9 + day * 0.1, 1000, 5, 0)
            for day in range(1, 32)
        ] + [
            Kline(f"2026-02-{day:02d}", 13, 13 + day * 0.1, 13 + day * 0.2, 12 + day * 0.1, 1000, 5, 0)
            for day in range(1, 29)
        ] + [
            Kline(f"2026-03-{day:02d}", 16, 16 + day * 0.1, 16 + day * 0.2, 15 + day * 0.1, 1000, 5, 0)
            for day in range(1, 32)
        ]
        allowed = weekly_last_trading_days(k.trade_date for k in klines)
        samples = build_training_samples(
            {"000001": klines},
            {
                **DEFAULT_CONFIG["ml"],
                "weekly_last_trading_day_only": True,
                "lookback_trading_days": 20,
                "horizon_trading_days": 5,
                "sample_stride": 1,
            },
        )
        self.assertGreater(len(samples), 0)
        self.assertTrue(all(sample.trade_date in allowed for sample in samples))

    def test_label_future_exit_rules_stop_loss(self) -> None:
        klines = []
        for day in range(1, 23):
            close = 10.0
            high = 10.2
            low = 9.8
            if day == 22:
                high = 10.0
                low = 8.9
                close = 9.2
            klines.append(Kline(f"2026-05-{day:02d}", 10.0, close, high, low, 1000, 5, 0))

        result = label_future(
            klines,
            end_idx=20,
            horizon=1,
            cfg={
                "use_trade_exit_rules": True,
                "exit_stop_loss_pct": 0.10,
                "exit_on_break_ma20": True,
                "positive_high_gain_pct": 0.0,
                "positive_close_gain_pct": 0.0,
            },
        )
        self.assertIsNotNone(result)
        label, _high_gain, close_gain, _max_drawdown = result or (0, 0, 0, 0)
        self.assertEqual(label, 0)
        self.assertAlmostEqual(close_gain, -10.0, places=4)

    def test_label_future_exit_rules_break_ma20(self) -> None:
        klines = []
        for day in range(1, 23):
            close = 10.0
            high = 10.2
            low = 9.8
            if day == 22:
                close = 9.5
                high = 9.7
                low = 9.3
            klines.append(Kline(f"2026-05-{day:02d}", 10.0, close, high, low, 1000, 5, 0))

        result = label_future(
            klines,
            end_idx=20,
            horizon=1,
            cfg={
                "use_trade_exit_rules": True,
                "exit_stop_loss_pct": 0.10,
                "exit_on_break_ma20": True,
                "positive_high_gain_pct": 0.0,
                "positive_close_gain_pct": -0.02,
            },
        )
        self.assertIsNotNone(result)
        label, _high_gain, close_gain, _max_drawdown = result or (0, 0, 0, 0)
        self.assertEqual(label, 0)
        self.assertAlmostEqual(close_gain, -5.0, places=4)

    def test_label_future_any_target_logic_matches_review_goal(self) -> None:
        klines = [Kline("2026-05-01", 10.0, 10.0, 10.0, 10.0, 1000, 5, 0)]
        klines.append(Kline("2026-05-02", 10.0, 10.1, 10.6, 9.7, 1000, 5, 0))

        result = label_future(
            klines,
            end_idx=0,
            horizon=1,
            cfg={
                "use_trade_exit_rules": False,
                "positive_high_gain_pct": 0.05,
                "positive_close_gain_pct": 0.02,
                "positive_target_logic": "any",
                "negative_drawdown_pct": 0.06,
            },
        )
        self.assertIsNotNone(result)
        label, high_gain, close_gain, max_drawdown = result or (0, 0, 0, 0)
        self.assertEqual(label, 1)
        self.assertAlmostEqual(high_gain, 6.0, places=4)
        self.assertAlmostEqual(close_gain, 1.0, places=4)
        self.assertAlmostEqual(max_drawdown, -3.0, places=4)


if __name__ == "__main__":
    unittest.main()
