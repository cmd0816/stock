import contextlib
import io
import sqlite3
import unittest
from unittest.mock import patch

from weekly_stock.backtest_diagnostics import BacktestReport, choose_canonical_runs, stage_diagnostics
from weekly_stock.cli import print_backtest_metrics
from weekly_stock.db import backtest_screen_context, ensure_weekly_tables
from weekly_stock.ml import TrainingSample, WalkForwardFold, backtest_models


def run(run_id, created, day='2026-07-03'):
    return dict(run_id=run_id, screen_date=day, created_at_utc=created)


def sample(code, day, gain=2):
    return TrainingSample(code, day, {'score': int(code) / 10}, int(gain > 0), gain + 1, gain, -3)


class FakeModel:
    def predict_probability(self, features):
        return features['score']

    def predict_probabilities(self, features):
        return [self.predict_probability(row) for row in features]


class BacktestDiagnosticsTests(unittest.TestCase):
    def test_latest_before_cutoff_and_timezone_boundary(self):
        runs = [run(99, '2026-07-03T23:49:29+00:00'),
                run(100, '2026-07-05T05:21:44+00:00'),
                run(101, '2026-07-06T01:30:00+00:00'),
                run(102, '2026-07-06T09:31:00+08:00'),
                run(103, '2026-07-03T06:59:59+00:00'),
                run(104, '2026-07-03T23:00:00'), run(105, 'now')]
        chosen, audit = choose_canonical_runs(runs, ['2026-07-03', '2026-07-06'])
        self.assertEqual(chosen, {100})
        status = {r['run_id']: r['status'] for r in audit}
        self.assertEqual(status[99], 'alternate_same_date')
        self.assertEqual(status[101], 'at_or_after_entry_cutoff')
        self.assertEqual(status[102], 'at_or_after_entry_cutoff')
        self.assertEqual(status[103], 'before_signal_close')
        self.assertEqual(status[104], 'unverifiable_timestamp')
        self.assertEqual(status[105], 'unverifiable_timestamp')

    def test_session_calendar_holiday_ties_and_missing_next_day(self):
        runs = [run(1, '2026-06-21T12:00:00Z', '2026-06-18'),
                run(2, '2026-06-21T12:00:00Z', '2026-06-18')]
        chosen, _ = choose_canonical_runs(runs, ['2026-06-18', '2026-06-22'])
        self.assertEqual(chosen, {2})
        self.assertEqual(choose_canonical_runs(runs[::-1], ['2026-06-18', '2026-06-22'])[0], {2})
        chosen, audit = choose_canonical_runs(runs, ['2026-06-18'])
        self.assertFalse(chosen)
        self.assertEqual(audit[0]['status'], 'missing_trading_date_or_next_session')
        with self.assertRaises(ValueError):
            choose_canonical_runs(runs, [], '09:31')

    def test_stage_coverage_missing_snapshots_and_common_dates(self):
        rows = [sample('1', '2026-07-03', 2), sample('2', '2026-07-03', -4),
                sample('1', '2026-07-10', 5)]
        pools = [dict(run_id=1, screen_date='2026-07-03', upstream={'1', '2'}, selected={'1'}),
                 dict(run_id=2, screen_date='2026-07-10', upstream={'1', 'missing'}, selected={'1'}),
                 dict(run_id=3, screen_date='2026-07-17', upstream={'1'}, selected={'1'})]
        result = stage_diagnostics(rows, pools)
        self.assertEqual(len(result), 6)
        self.assertTrue(all(row['comparable'] for row in result[:3]))
        self.assertAlmostEqual(result[0]['avg_exit_pct'], -1)
        self.assertAlmostEqual(result[2]['avg_exit_pct'], 2)
        self.assertFalse(any(row['comparable'] for row in result[3:]))
        self.assertEqual(result[4]['status'], 'incomplete_labels')
        self.assertEqual(result[4]['available'], 1)
        self.assertEqual(result[4]['expected'], 2)
        self.assertIsNone(result[4]['avg_exit_pct'])
        pools[0]['upstream'] = None
        pools[0]['upstream_expected'] = 20
        result = stage_diagnostics(rows, pools)
        self.assertEqual(result[1]['status'], 'missing_snapshot')
        self.assertEqual(result[1]['expected'], 20)
        self.assertIsNone(result[1]['hit_rate'])

    def test_multi_k_and_rerank_excludes_no_choice_periods(self):
        rows, candidates = [], []
        for run_id, day in enumerate(['2026-07-03', '2026-07-10'], 1):
            for i in range(1, 7):
                rows.append(sample(str(i), day, i - 3))
                candidates.append(dict(run_id=run_id, screen_date=day, code=str(i),
                                       total_score=100 - i, rank_no=i))
        fold = WalkForwardFold(1, rows, rows, '2026-07-03', '2026-07-10', 0)
        cfg = dict(model_name='lightgbm', baseline_model_name='logistic_regression',
                   backtest_top_k=10, backtest_top_ks=[3, 5, 10])
        with patch('weekly_stock.ml.purged_walk_forward_splits', return_value=[fold]), \
             patch('weekly_stock.ml.train_model', return_value=FakeModel()), \
             patch('weekly_stock.ml.train_logistic_regression_model', return_value=FakeModel()):
            metrics = backtest_models(rows, cfg, rule_candidates=candidates)
        self.assertEqual(len(metrics), 36)
        self.assertEqual({m.top_k for m in metrics}, {3, 5, 10})
        for metric in metrics:
            if metric.scope == 'rerank' and metric.top_k == 10:
                self.assertEqual(metric.test_count, 0)
                self.assertEqual(metric.selected_count, 0)
                self.assertIsNone(metric.exit_delta_ci)
                self.assertIsNone(metric.exit_delta_vs_rule_pct)
            else:
                self.assertEqual(metric.test_count, 12)
                self.assertEqual(metric.selected_count, min(metric.top_k, 6) * 2)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            print_backtest_metrics(metrics)
        self.assertIn('N/A（没有符合本口径的测试批次）', output.getvalue())

    def make_db(self):
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        ensure_weekly_tables(conn)
        conn.executescript('''
            CREATE TABLE eastmoney_stock_daily_klines (code TEXT, name TEXT, trade_date TEXT);
            INSERT INTO eastmoney_stock_daily_klines VALUES ('000001','测试','2026-07-03'),('000001','测试','2026-07-06');
            CREATE TABLE xuangu_batches (batch_id TEXT, imported_at_utc TEXT);
            CREATE TABLE xuangu_results (batch_id TEXT, imported_at_utc TEXT, stock_code TEXT);
            INSERT INTO xuangu_batches VALUES ('b1','2026-07-03T08:00:00+00:00');
            INSERT INTO xuangu_results VALUES ('b1','2026-07-03T08:00:00+00:00','000001');
        ''')
        for run_id, timestamp in [(99, '2026-07-03T23:49:29+00:00'), (100, '2026-07-05T05:21:44+00:00')]:
            conn.execute('''INSERT INTO weekly_screen_runs
                (run_id,screen_date,xuangu_batch_id,strategy_config_json,candidate_count,selected_count,created_at_utc)
                VALUES (?, '2026-07-03','b1','{}',1,1,?)''', (run_id, timestamp))
            conn.execute('''INSERT INTO weekly_screen_candidates
                (run_id,code,total_score,rank_no,selected,row_json,score_json,
                 trend_score,volume_turnover_score,breakout_score,fundamentals_score,risk_score)
                VALUES (?,'000001',80,1,1,'{}','{}',20,20,20,10,10)''', (run_id,))
            conn.execute('''INSERT INTO weekly_selected_stocks
                (run_id,screen_date,code,rank_no,total_score,selected_reason,created_at_utc)
                VALUES (?,'2026-07-03','000001',1,80,'test',?)''', (run_id, timestamp))
        return conn

    def test_sql_context_deduplicates_without_deleting_and_verifies_snapshot(self):
        conn = self.make_db()
        try:
            context = backtest_screen_context(conn, {})
            self.assertEqual([r['run_id'] for r in context['rule_candidates']], [100])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM weekly_screen_runs').fetchone()[0], 2)
            self.assertEqual(context['pools'][0]['upstream'], {'000001'})
            conn.execute("UPDATE xuangu_results SET imported_at_utc='2026-07-07T00:00:00Z'")
            context = backtest_screen_context(conn, {})
            self.assertIsNone(context['pools'][0]['upstream'])
            self.assertEqual(context['pools'][0]['upstream_status'], 'snapshot_imported_after_run')
        finally:
            conn.close()

    def test_canonical_snapshot_failure_does_not_fall_back_to_older_run(self):
        conn = self.make_db()
        try:
            conn.execute('DELETE FROM weekly_selected_stocks WHERE run_id=100')
            context = backtest_screen_context(conn, {})
            self.assertEqual(context['rule_candidates'], [])
            self.assertEqual(context['pools'][0]['selected_status'], 'incomplete_selected_snapshot')
            self.assertEqual([r['run_id'] for r in context['run_audit'] if r['status']=='canonical'], [100])
        finally:
            conn.close()

    def test_cli_reports_missing_stages_without_fabricating_gains(self):
        report = BacktestReport(stages=stage_diagnostics(
            [sample('1', '2026-07-03')],
            [dict(run_id=1, screen_date='2026-07-03', upstream=None, selected={'1'})]))
        from weekly_stock.cli import print_backtest_diagnostics
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            print_backtest_diagnostics(report)
        self.assertIn('missing_snapshot', output.getvalue())
        self.assertIn('没有三阶段均完整的共同日期', output.getvalue())

    def test_missing_canonical_labels_never_resurrect_alternate_run(self):
        conn = self.make_db()
        try:
            conn.execute("UPDATE weekly_screen_candidates SET code='000002' WHERE run_id=100")
            conn.execute("UPDATE weekly_selected_stocks SET code='000002' WHERE run_id=100")
            context = backtest_screen_context(conn, {})
            rows = [sample('000001', '2026-07-03')]
            fold = WalkForwardFold(1, rows, rows, '2026-07-03', '2026-07-03', 0)
            with patch('weekly_stock.ml.purged_walk_forward_splits', return_value=[fold]), \
                 patch('weekly_stock.ml.train_model', return_value=FakeModel()), \
                 self.assertWarnsRegex(UserWarning, 'Run 100: executable labels 0/1'):
                result = backtest_models(rows, dict(model_name='test', baseline_model_name='none'),
                                         rule_candidates=context['rule_candidates'], screen_context=context)
            self.assertEqual({m.scope for m in result}, {'market'})
            self.assertEqual(result.stages[-1]['status'], 'incomplete_labels')
            self.assertEqual([r['run_id'] for r in result.run_audit if r['status']=='canonical'], [100])
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
