import sqlite3
import tempfile
import unittest
from pathlib import Path

from eastmoney_to_sqlite import init_db
from weekly_stock import db


class MlContextFeatureTests(unittest.TestCase):
    def test_stock_kline_filters_ignore_index_rows_for_training_reads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "stocks.db"
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                init_db(conn)
                rows = [
                    ("src", 0, "000001", "0.000001", "上证综合指数", "2026-05-01", 1, 1, 1, 1, 1, 1, 0, 1, 0, 1, "{}", "now"),
                    ("src", 0, "000001", "0.000001", "上证综合指数", "2026-05-02", 1, 1, 1, 1, 1, 1, 0, 1, 0, 1, "{}", "now"),
                    ("src", 0, "000002", "0.000002", "A", "2026-05-01", 2, 2, 2, 2, 1, 1, 0, 1, 0, 1, "{}", "now"),
                    ("src", 0, "000003", "0.000003", "中证测试指数", "2026-05-01", 3, 3, 3, 3, 1, 1, 0, 1, 0, 1, "{}", "now"),
                    ("src", 0, "000003", "0.000003", "B", "2026-05-02", 4, 4, 4, 4, 1, 1, 0, 1, 0, 1, "{}", "now"),
                ]
                conn.executemany(
                    """
                    INSERT INTO eastmoney_stock_daily_klines (
                        source_url, market, code, secid, name, trade_date,
                        open, close, high, low, volume, turnover,
                        amplitude_percent, change_percent, change_amount, turnover_rate,
                        raw_line, fetched_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                conn.commit()

                self.assertEqual(db.all_downloaded_codes(conn), ["000002", "000003"])
                self.assertEqual(db.load_klines(conn, "000001"), [])
                self.assertEqual([k.trade_date for k in db.load_klines(conn, "000003")], ["2026-05-02"])

    def test_load_ml_context_features_rolls_fund_and_sector(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "stocks.db"
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                init_db(conn)
                db.ensure_weekly_tables(conn)

                kline_rows = [
                    # date 1
                    ("src", 0, "000001", "0.000001", "A", "2026-05-01", 10, 10, 10, 10, 1, 1, 0, 1, 0, 1, "{}", "now"),
                    ("src", 0, "000002", "0.000002", "B", "2026-05-01", 10, 10, 10, 10, 1, 1, 0, 3, 0, 1, "{}", "now"),
                    ("src", 0, "000003", "0.000003", "C", "2026-05-01", 10, 10, 10, 10, 1, 1, 0, -1, 0, 1, "{}", "now"),
                    # date 2
                    ("src", 0, "000001", "0.000001", "A", "2026-05-02", 10, 10, 10, 10, 1, 1, 0, 2, 0, 1, "{}", "now"),
                    ("src", 0, "000002", "0.000002", "B", "2026-05-02", 10, 10, 10, 10, 1, 1, 0, 4, 0, 1, "{}", "now"),
                    ("src", 0, "000003", "0.000003", "C", "2026-05-02", 10, 10, 10, 10, 1, 1, 0, -2, 0, 1, "{}", "now"),
                    # date 3
                    ("src", 0, "000001", "0.000001", "A", "2026-05-03", 10, 10, 10, 10, 1, 1, 0, 3, 0, 1, "{}", "now"),
                    ("src", 0, "000002", "0.000002", "B", "2026-05-03", 10, 10, 10, 10, 1, 1, 0, 5, 0, 1, "{}", "now"),
                    ("src", 0, "000003", "0.000003", "C", "2026-05-03", 10, 10, 10, 10, 1, 1, 0, -3, 0, 1, "{}", "now"),
                ]
                conn.executemany(
                    """
                    INSERT INTO eastmoney_stock_daily_klines (
                        source_url, market, code, secid, name, trade_date,
                        open, close, high, low, volume, turnover,
                        amplitude_percent, change_percent, change_amount, turnover_rate,
                        raw_line, fetched_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    kline_rows,
                )

                sector_rows = [
                    {"code": "000001", "sector_name": "Tech", "source_url": "unit:test", "raw_line": {}},
                    {"code": "000002", "sector_name": "Tech", "source_url": "unit:test", "raw_line": {}},
                    {"code": "000003", "sector_name": "Finance", "source_url": "unit:test", "raw_line": {}},
                ]
                db.upsert_sector_rows(conn, sector_rows)

                fund_rows = [
                    {"code": "000001", "trade_date": "2026-05-01", "main_net_inflow": 10, "main_net_inflow_ratio": 1.0, "source_url": "unit:test", "raw_line": {}},
                    {"code": "000001", "trade_date": "2026-05-02", "main_net_inflow": 20, "main_net_inflow_ratio": 2.0, "source_url": "unit:test", "raw_line": {}},
                    {"code": "000001", "trade_date": "2026-05-03", "main_net_inflow": 30, "main_net_inflow_ratio": 3.0, "source_url": "unit:test", "raw_line": {}},
                ]
                db.upsert_fund_flow_rows(conn, fund_rows)
                conn.commit()

                out = db.load_ml_context_features(conn, [("000001", "2026-05-03")])

            features = out[("000001", "2026-05-03")]
            self.assertAlmostEqual(float(features["fund_main_net_ratio"]), 3.0, places=8)
            self.assertAlmostEqual(float(features["fund_main_net_ratio_5"]), 2.0, places=8)
            self.assertAlmostEqual(float(features["fund_main_net_ratio_20"]), 2.0, places=8)
            self.assertEqual(float(features["fund_flow_available"]), 1.0)
            self.assertAlmostEqual(float(features["sector_ret_5"]), 3.0, places=8)
            self.assertAlmostEqual(float(features["sector_ret_20"]), 3.0, places=8)
            self.assertAlmostEqual(float(features["sector_momentum_5_20"]), 0.0, places=8)
            self.assertEqual(float(features["sector_available"]), 1.0)

    def test_load_ml_context_features_market_breadth_and_relative_strength(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "stocks.db"
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                init_db(conn)
                db.ensure_weekly_tables(conn)

                rows = []
                for day in range(1, 26):
                    trade_date = f"2026-05-{day:02d}"
                    specs = [
                        ("000001", "A", 100 + day, 1.0),
                        ("000002", "B", 200 + day * 2, 3.0),
                        ("000003", "C", 100 - day, -1.0),
                        # BaoStock query_all_stock can include index rows whose
                        # 6-digit code collides with real stocks. They should
                        # not affect market breadth / relative strength.
                        ("000004", "上证工业类指数", 9999 + day, 99.0),
                    ]
                    for code, name, close, change_pct in specs:
                        rows.append(
                            (
                                "src",
                                0,
                                code,
                                f"0.{code}",
                                name,
                                trade_date,
                                close - 0.5,
                                close,
                                close + 1.0,
                                close - 1.0,
                                1000,
                                1,
                                0,
                                change_pct,
                                0,
                                1,
                                "{}",
                                "now",
                            )
                        )
                conn.executemany(
                    """
                    INSERT INTO eastmoney_stock_daily_klines (
                        source_url, market, code, secid, name, trade_date,
                        open, close, high, low, volume, turnover,
                        amplitude_percent, change_percent, change_amount, turnover_rate,
                        raw_line, fetched_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                db.upsert_sector_rows(
                    conn,
                    [
                        {"code": "000001", "sector_name": "Tech", "source_url": "unit:test", "raw_line": {}},
                        {"code": "000002", "sector_name": "Tech", "source_url": "unit:test", "raw_line": {}},
                        {"code": "000003", "sector_name": "Finance", "source_url": "unit:test", "raw_line": {}},
                    ],
                )
                conn.commit()

                out = db.load_ml_context_features(conn, [("000001", "2026-05-25")])

            features = out[("000001", "2026-05-25")]
            a_ret_5 = (125 / 120 - 1) * 100
            c_ret_5 = (75 / 80 - 1) * 100
            market_ret_5 = (a_ret_5 + a_ret_5 + c_ret_5) / 3
            a_ret_20 = (125 / 105 - 1) * 100
            c_ret_20 = (75 / 95 - 1) * 100
            market_ret_20 = (a_ret_20 + a_ret_20 + c_ret_20) / 3

            self.assertAlmostEqual(float(features["market_breadth_adv_5"]), 2 / 3, places=8)
            self.assertAlmostEqual(float(features["market_breadth_above_ma20"]), 2 / 3, places=8)
            self.assertAlmostEqual(float(features["market_breadth_new_high_20"]), 2 / 3, places=8)
            self.assertEqual(float(features["market_universe_size"]), 3.0)
            self.assertAlmostEqual(float(features["market_member_ret_5"]), market_ret_5, places=8)
            self.assertAlmostEqual(float(features["market_member_ret_20"]), market_ret_20, places=8)
            self.assertAlmostEqual(float(features["sector_member_ret_5"]), a_ret_5, places=8)
            self.assertAlmostEqual(float(features["sector_member_ret_20"]), a_ret_20, places=8)
            self.assertAlmostEqual(float(features["excess_ret_5_vs_market"]), a_ret_5 - market_ret_5, places=8)
            self.assertAlmostEqual(float(features["excess_ret_20_vs_market"]), a_ret_20 - market_ret_20, places=8)
            self.assertAlmostEqual(float(features["excess_ret_5_vs_sector"]), 0.0, places=8)
            self.assertAlmostEqual(float(features["excess_ret_20_vs_sector"]), 0.0, places=8)


if __name__ == "__main__":
    unittest.main()
