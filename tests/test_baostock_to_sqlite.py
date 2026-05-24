import sqlite3
import tempfile
import unittest
from pathlib import Path

from baostock_to_sqlite import convert_baostock_row, save_baostock_kline_rows


class BaoStockToSqliteTests(unittest.TestCase):
    def test_convert_row_maps_turn_to_turnover_rate(self) -> None:
        row = {
            "date": "2026-05-22",
            "open": "10.00",
            "high": "10.80",
            "low": "9.90",
            "close": "10.50",
            "preclose": "10.00",
            "volume": "123456",
            "amount": "1296000",
            "turn": "0.0345",
            "pctChg": "5.00",
        }
        out = convert_baostock_row(row)
        assert out is not None
        self.assertAlmostEqual(float(out["turnover_rate"]), 0.0345, places=8)
        self.assertAlmostEqual(float(out["change_amount"]), 0.5, places=8)
        self.assertAlmostEqual(float(out["amplitude_percent"]), 9.0, places=8)

    def test_save_rows_writes_source_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "stocks.db"
            rows = [
                {
                    "date": "2026-05-22",
                    "code": "sh.600000",
                    "open": "10.00",
                    "high": "10.80",
                    "low": "9.90",
                    "close": "10.50",
                    "preclose": "10.00",
                    "volume": "123456",
                    "amount": "1296000",
                    "adjustflag": "2",
                    "turn": "0.0345",
                    "tradestatus": "1",
                    "pctChg": "5.00",
                }
            ]

            saved = save_baostock_kline_rows(db_path, 1, "600000", "TEST", rows, "qfq")
            self.assertEqual(saved, 1)

            with sqlite3.connect(db_path) as conn:
                row = conn.execute(
                    """
                    SELECT source_url, turnover_rate
                    FROM eastmoney_stock_daily_klines
                    WHERE code='600000' AND trade_date='2026-05-22'
                    """
                ).fetchone()
            self.assertEqual(row[0], "baostock:query_history_k_data_plus:adjust=qfq")
            self.assertAlmostEqual(float(row[1]), 0.0345, places=8)


if __name__ == "__main__":
    unittest.main()
