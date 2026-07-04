import sqlite3
import tempfile
import unittest
from pathlib import Path

from eastmoney_to_sqlite import init_db
from baostock_to_sqlite import (
    apply_offset_and_limit,
    convert_baostock_row,
    filter_targets_with_existing_history,
    save_baostock_kline_rows,
)


class BaoStockToSqliteTests(unittest.TestCase):
    def test_apply_offset_and_limit(self) -> None:
        targets = [
            ("000003", "C"),
            ("000001", "A"),
            ("000004", "D"),
            ("000002", "B"),
        ]
        self.assertEqual(
            apply_offset_and_limit(targets, offset=2, limit=1),
            [("000003", "C")],
        )
        self.assertEqual(
            apply_offset_and_limit(targets, offset=2, limit=0),
            [("000003", "C"), ("000004", "D")],
        )
        self.assertEqual(apply_offset_and_limit(targets, offset=99, limit=0), [])

    def test_filter_targets_with_existing_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "stocks.db"
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                init_db(conn)
                for code, dates in {
                    "000001": ["2026-07-01", "2026-07-02", "2026-07-03"],
                    "000002": ["2026-07-01", "2026-07-02"],
                    "000003": ["2026-07-01", "2026-07-02", "2026-07-03"],
                }.items():
                    for trade_date in dates:
                        conn.execute(
                            """
                            INSERT INTO eastmoney_stock_daily_klines (
                                source_url, market, code, secid, name, trade_date,
                                open, close, high, low, volume, turnover,
                                amplitude_percent, change_percent, change_amount, turnover_rate,
                                raw_line, fetched_at_utc
                            ) VALUES ('unit', 0, ?, ?, 'T', ?, 1, 1, 1, 1, 1, 1, 0, 0, 0, 1, '{}', 'now')
                            """,
                            (code, f"0.{code}", trade_date),
                        )
                conn.commit()

                remaining, skipped = filter_targets_with_existing_history(
                    conn,
                    [("000001", "A"), ("000002", "B"), ("000003", "C")],
                    "2026-07-01",
                    "2026-07-03",
                    3,
                )

        self.assertEqual(skipped, 2)
        self.assertEqual(remaining, [("000002", "B")])

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
