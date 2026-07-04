import sqlite3
import tempfile
import unittest
from pathlib import Path

from eastmoney_to_sqlite import init_db
from baostock_to_sqlite import (
    apply_offset_and_limit,
    convert_baostock_row,
    fetch_all_a_share_stocks,
    filter_targets_with_existing_history,
    infer_market,
    save_baostock_kline_rows,
    to_baostock_code,
)


class FakeBaoStockResult:
    fields = ["code", "code_name", "tradeStatus"]
    error_code = "0"
    error_msg = ""

    def __init__(self, rows: list[list[str]]) -> None:
        self.rows = rows
        self.index = -1

    def next(self) -> bool:
        self.index += 1
        return self.index < len(self.rows)

    def get_row_data(self) -> list[str]:
        return self.rows[self.index]


class FakeBaoStock:
    def __init__(self, rows: list[list[str]]) -> None:
        self.rows = rows

    def query_all_stock(self, day: str) -> FakeBaoStockResult:
        return FakeBaoStockResult(self.rows)


class BaoStockToSqliteTests(unittest.TestCase):
    def test_baostock_code_mapping_handles_bj_9_prefix(self) -> None:
        self.assertEqual(to_baostock_code("600000"), "sh.600000")
        self.assertEqual(to_baostock_code("000001"), "sz.000001")
        self.assertEqual(to_baostock_code("430001"), "bj.430001")
        self.assertEqual(to_baostock_code("830001"), "bj.830001")
        self.assertEqual(to_baostock_code("920001"), "bj.920001")
        self.assertEqual(to_baostock_code("bj.920001"), "bj.920001")
        self.assertEqual(infer_market("sh.600000"), 1)
        self.assertEqual(infer_market("bj.920001"), 0)
        self.assertEqual(infer_market("920001"), 0)

    def test_fetch_all_a_share_stocks_excludes_indices(self) -> None:
        fake_bs = FakeBaoStock(
            [
                ["sh.000001", "上证综合指数", "1"],
                ["sz.000001", "平安银行", "1"],
                ["sh.600000", "浦发银行", "1"],
                ["sh.688001", "华兴源创", "1"],
                ["sz.300001", "特锐德", "1"],
                ["bj.920001", "北交测试", "1"],
                ["sz.399001", "深证成指", "1"],
                ["sh.900901", "沪B测试", "1"],
            ]
        )

        targets = fetch_all_a_share_stocks(fake_bs, "2026-07-03")

        self.assertEqual(
            targets,
            [
                ("000001", "平安银行"),
                ("300001", "特锐德"),
                ("600000", "浦发银行"),
                ("688001", "华兴源创"),
                ("920001", "北交测试"),
            ],
        )

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
                    # Existing index rows with colliding stock-like codes must
                    # not cause --skip-existing-days to skip the real stock.
                    "000004": ["2026-07-01", "2026-07-02", "2026-07-03"],
                }.items():
                    for trade_date in dates:
                        name = "上证工业类指数" if code == "000004" else "T"
                        conn.execute(
                            """
                            INSERT INTO eastmoney_stock_daily_klines (
                                source_url, market, code, secid, name, trade_date,
                                open, close, high, low, volume, turnover,
                                amplitude_percent, change_percent, change_amount, turnover_rate,
                                raw_line, fetched_at_utc
                            ) VALUES ('unit', 0, ?, ?, ?, ?, 1, 1, 1, 1, 1, 1, 0, 0, 0, 1, '{}', 'now')
                            """,
                            (code, f"0.{code}", name, trade_date),
                        )
                conn.commit()

                remaining, skipped = filter_targets_with_existing_history(
                    conn,
                    [("000001", "A"), ("000002", "B"), ("000003", "C"), ("000004", "D")],
                    "2026-07-01",
                    "2026-07-03",
                    3,
                )

        self.assertEqual(skipped, 2)
        self.assertEqual(remaining, [("000002", "B"), ("000004", "D")])

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
