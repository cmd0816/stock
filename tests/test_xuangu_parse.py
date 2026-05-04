import tempfile
import unittest
import zipfile
from pathlib import Path
import sqlite3

from openpyxl import Workbook

from xuangu_to_sqlite import (
    clean_stock_code,
    detect_code_name_keys,
    import_xlsx_to_sqlite,
    parse_xlsx_rows,
)


def make_bad_dimension_xlsx(path: Path) -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "sheet1"
    worksheet.append(["序号", "代码", "名称"])
    worksheet.append([1, "000967", "盈峰环境"])
    worksheet.append([2, "600110", "诺德股份"])
    workbook.save(path)

    with zipfile.ZipFile(path, "r") as zin:
        files = {name: zin.read(name) for name in zin.namelist()}

    sheet_name = "xl/worksheets/sheet1.xml"
    files[sheet_name] = files[sheet_name].replace(
        b'<dimension ref="A1:C3"/>',
        b'<dimension ref="A1:A3"/>',
    )

    with zipfile.ZipFile(path, "w") as zout:
        for name, content in files.items():
            zout.writestr(name, content)


class XuanguParseTests(unittest.TestCase):
    def test_parse_xlsx_rows_handles_bad_sheet_dimension(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            xlsx_path = Path(tmp) / "xuangu.xlsx"
            make_bad_dimension_xlsx(xlsx_path)

            rows, sheet_count = parse_xlsx_rows(xlsx_path)

            self.assertEqual(sheet_count, 1)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["headers"], ["序号", "代码", "名称"])

            code_key, name_key = detect_code_name_keys(rows[0]["headers"])
            self.assertEqual(code_key, "代码")
            self.assertEqual(name_key, "名称")
            self.assertEqual(clean_stock_code(rows[0]["row_map"][code_key]), "000967")
            self.assertEqual(rows[0]["row_map"][name_key], "盈峰环境")

    def test_import_xlsx_can_replace_existing_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            xlsx_path = root / "xuangu.xlsx"
            db_path = root / "stocks.db"
            make_bad_dimension_xlsx(xlsx_path)

            first = import_xlsx_to_sqlite(
                db_path=db_path,
                xlsx_path=xlsx_path,
                source_url="test",
                condition_text="cond",
                batch_id="20260503",
            )
            second = import_xlsx_to_sqlite(
                db_path=db_path,
                xlsx_path=xlsx_path,
                source_url="test",
                condition_text="cond",
                batch_id="20260503",
                replace_existing=True,
            )

            self.assertEqual(first, ("20260503", 1, 2))
            self.assertEqual(second, ("20260503", 1, 2))
            with sqlite3.connect(db_path) as conn:
                count, recognized = conn.execute(
                    "SELECT COUNT(*), SUM(stock_code IS NOT NULL) FROM xuangu_results"
                ).fetchone()
            self.assertEqual(count, 2)
            self.assertEqual(recognized, 2)


if __name__ == "__main__":
    unittest.main()
