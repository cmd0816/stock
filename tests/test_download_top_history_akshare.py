import sqlite3
import tempfile
import unittest
from pathlib import Path

from download_top_history_akshare import save_akshare_kline_rows


class DownloadTopHistoryAkshareTests(unittest.TestCase):
    def test_save_akshare_kline_rows_turnover_rate_by_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "stocks.db"
            rows = [
                {
                    "date": "2026-05-20",
                    "open": 10.0,
                    "close": 10.5,
                    "high": 10.8,
                    "low": 9.9,
                    "volume": 10000,
                    "amount": 102000,
                    "turnover": 0.1234,
                }
            ]

            save_akshare_kline_rows(db_path, 0, "000001", "TEST", rows, "stock_zh_a_daily")
            with sqlite3.connect(db_path) as conn:
                value = conn.execute(
                    "SELECT turnover_rate FROM eastmoney_stock_daily_klines WHERE code='000001' AND trade_date='2026-05-20'"
                ).fetchone()[0]
            self.assertAlmostEqual(value, 0.1234, places=8)

            save_akshare_kline_rows(db_path, 0, "000001", "TEST", rows, "stock_zh_a_hist")
            with sqlite3.connect(db_path) as conn:
                value = conn.execute(
                    "SELECT turnover_rate FROM eastmoney_stock_daily_klines WHERE code='000001' AND trade_date='2026-05-20'"
                ).fetchone()[0]
            self.assertAlmostEqual(value, 12.34, places=8)


if __name__ == "__main__":
    unittest.main()
