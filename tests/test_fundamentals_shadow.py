import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from weekly_stock.fundamentals import coverage_report, field_assessment, growth_field
from weekly_stock.models import CandidateStock, ScoreBreakdown, ScoredStock
from weekly_stock.scoring import score_fundamentals
from weekly_stock.shadow import half_volume_score, save_shadow_ranking
from xuangu_to_sqlite import import_xlsx_to_sqlite


class FundamentalShadowTests(unittest.TestCase):
    def test_report_suffix_never_becomes_a_growth_value(self):
        key = '最新净利润同比增长率(%)(截至2026.06.30最新)'
        self.assertEqual(growth_field({key: '--|2026半年报'}, 'profit')['status'], 'missing_value')
        self.assertEqual(growth_field({key: '0|2026半年报'}, 'profit')['value'], 0)
        self.assertEqual(growth_field({key: '-12.3%|2026半年报'}, 'profit')['value'], -12.3)
        self.assertEqual(growth_field({key: 'NaN'}, 'profit')['status'], 'invalid_value')

    def test_missing_is_different_from_below_threshold(self):
        self.assertEqual(field_assessment({}, 'profit', 10)['assessment'], 'unknown')
        self.assertEqual(field_assessment({'净利润同比': 0}, 'profit', 10)['assessment'], 'below_threshold')
        self.assertEqual(field_assessment({'净利润同比': 10}, 'profit', 10)['assessment'], 'pass')
        self.assertEqual(growth_field({'扣非净利润同比': 50}, 'profit')['status'], 'missing_column')

    def test_conflicting_periods_not_arbitrarily_selected(self):
        rows = {'净利润同比(2026半年报)': 20, '净利润同比(2026一季报)': -10}
        self.assertEqual(growth_field(rows, 'profit')['status'], 'ambiguous_columns')
        rows['最新净利润同比'] = 20
        self.assertEqual(growth_field(rows, 'profit')['value'], 20)

    def test_legacy_missing_profit_score_is_preserved_but_explained(self):
        candidate = CandidateStock('600108', 'test', 'b', {'最新营业收入同比增长率': 20})
        reasons = []
        score = score_fundamentals(candidate, {'fundamentals': {'revenue_growth_min': 10, 'profit_growth_min': 10}}, 15, reasons)
        self.assertEqual(score, 7.5)
        self.assertTrue(any('数据未知' in r and '净利润' in r for r in reasons))
        self.assertEqual(coverage_report([candidate.row_json])['fields']['profit'], {'missing_column': 1})

    def test_import_preserves_fields_and_captures_observation_not_announcement(self):
        raw = {'代码': '600108', '名称': 'test', '营业收入同比': '20|2026半年报', '净利润同比': '--|2026半年报'}
        parsed = [{'headers': list(raw), 'row_map': raw, 'sheet_name': 'sheet1', 'row_no': 2}]
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / 'test.db'
            with patch('xuangu_to_sqlite.parse_xlsx_rows', return_value=(parsed, 1)), \
                 contextlib.redirect_stdout(io.StringIO()):
                import_xlsx_to_sqlite(database, Path(tmp) / 'source.xlsx', 'test', 'cond', 'b1')
            with contextlib.closing(sqlite3.connect(database)) as conn:
                payload, observed = conn.execute('SELECT row_json, imported_at_utc FROM xuangu_results').fetchone()
            data = json.loads(payload)
            self.assertEqual({key: data[key] for key in raw}, raw)
            self.assertNotIn('_fundamental_audit_v1', raw)
            audit = data['_fundamental_audit_v1']
            self.assertEqual(audit['snapshot_observed_at_utc'], observed)
            self.assertEqual(audit['availability_basis'], 'export_snapshot_not_announcement_date')
            self.assertIsNone(audit['profit']['value'])

    def test_shadow_is_separate_and_preserves_original_membership(self):
        selected = [ScoredStock(CandidateStock('000001', 'a', 'b', {}), ScoreBreakdown(trend=30, volume_turnover=20), ''),
                    ScoredStock(CandidateStock('000002', 'b', 'b', {}), ScoreBreakdown(trend=45), '')]
        with contextlib.closing(sqlite3.connect(':memory:')) as conn:
            save_shadow_ranking(conn, 1, selected)
            rows = conn.execute('SELECT code,original_rank,shadow_rank,original_score,shadow_score FROM weekly_shadow_rankings ORDER BY shadow_rank').fetchall()
            self.assertEqual(rows, [('000002', 2, 1, 45, 45), ('000001', 1, 2, 50, 40)])
            self.assertEqual([s.candidate.code for s in selected], ['000001', '000002'])
            self.assertEqual(selected[0].score.total, 50)
            # Immutable snapshots cannot be replaced by re-running the same run.
            with self.assertRaises(sqlite3.IntegrityError):
                save_shadow_ranking(conn, 1, selected)
        self.assertEqual(half_volume_score(80, 20), 70)


if __name__ == '__main__':
    unittest.main()
