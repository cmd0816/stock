import sqlite3
import tempfile
import unittest
from pathlib import Path

from eastmoney_to_sqlite import init_db
from weekly_stock import db


class MlContextFeatureTests(unittest.TestCase):
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
            self.assertAlmostEqual(float(features["sector_ret_5"]), 3.0, places=8)
            self.assertAlmostEqual(float(features["sector_ret_20"]), 3.0, places=8)
            self.assertAlmostEqual(float(features["sector_momentum_5_20"]), 0.0, places=8)


if __name__ == "__main__":
    unittest.main()
