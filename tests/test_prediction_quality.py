import copy
import sqlite3
import unittest
from unittest.mock import patch

from weekly_stock.config import DEFAULT_CONFIG, load_config
from weekly_stock.data_quality import require_market_coverage
from weekly_stock.db import ensure_weekly_tables, review_feedback_labels
from weekly_stock.jobs import review_selected_stock, select_screen_candidates
from weekly_stock.ml import build_training_samples, label_future
from weekly_stock.models import CandidateStock, Kline, ScoreBreakdown, ScoredStock
from weekly_stock.trade_simulator import simulate_trade, simulation_version


def bar(day, opening, high, low, close):
    return Kline(day, opening, close, high, low, 1000, 5, 0)


class PredictionQualityTests(unittest.TestCase):
    def trade(self, rows, **kwargs):
        options = dict(high_target_pct=.05, close_target_pct=.02, stop_loss_pct=.06,
                       entry_mode='next_open', enforce_t_plus_one=True)
        options.update(kwargs)
        return simulate_trade(rows, 0, len(rows) - 1, **options)

    def test_next_open_gap_and_t_plus_one(self):
        rows = [bar('2026-08-28', 10, 10, 10, 10),
                bar('2026-08-31', 11, 12, 10.8, 11),
                bar('2026-09-01', 9.5, 10, 9.4, 9.8)]
        result = self.trade(rows)
        self.assertEqual(result.entry_price, 11)
        self.assertEqual(result.exit_reason, 'stop_loss')
        self.assertEqual(result.exit_trade_date, '2026-09-01')
        self.assertAlmostEqual(result.realized_gain_pct, (9.5 / 11 - 1) * 100)

    def test_opening_target_precedes_intraday_stop(self):
        rows = [bar('2026-08-28', 10, 10, 10, 10),
                bar('2026-08-31', 10, 10.1, 9.9, 10),
                bar('2026-09-01', 11, 11.1, 9, 9.5)]
        result = self.trade(rows)
        self.assertEqual(result.exit_reason, 'take_profit')
        self.assertAlmostEqual(result.realized_gain_pct, 10)

    def test_costs_are_applied_to_horizon_return(self):
        rows = [bar('2026-08-28', 10, 10, 10, 10),
                bar('2026-08-31', 10, 10.1, 9.9, 10),
                bar('2026-09-01', 10, 10.1, 9.9, 10)]
        result = self.trade(rows, fee_bps_per_side=5, slippage_bps_per_side=5)
        self.assertAlmostEqual(result.realized_gain_pct,
                               ((.9995 ** 2) / (1.0005 ** 2) - 1) * 100)

    def test_untradeable_entry_has_no_training_label(self):
        rows = [bar('2026-08-28', 10, 10, 10, 10),
                bar('2026-08-31', 11, 11, 11, 11),
                bar('2026-09-01', 12, 12.5, 11, 12)]
        self.assertEqual(self.trade(rows).exit_reason, 'no_entry')
        self.assertIsNone(label_future(rows, 0, 2, {'entry_mode': 'next_open'}))

    def test_new_review_and_training_use_identical_costs_and_version(self):
        cfg = load_config('config/weekly_strategy.yaml')
        cfg['review']['horizon_trading_days'] = 2
        cfg['ml']['horizon_trading_days'] = 2
        rows = [bar('2026-08-28', 10, 10, 10, 10),
                bar('2026-08-31', 11, 11.1, 10.9, 11),
                bar('2026-09-01', 10, 10.5, 9.8, 10.2)]
        review = review_selected_stock(rows, {'code': '600108', 'name': '测试', 'screen_date': rows[0].trade_date}, cfg)
        ml_cfg = {**cfg['ml'], **cfg['execution']}
        label = label_future(rows, 0, 2, ml_cfg)
        self.assertEqual(int(review.meets_expectation), label[0])
        self.assertAlmostEqual(review.close_gain_pct, label[2])
        self.assertEqual(review.simulation_version, simulation_version(ml_cfg))
        self.assertNotEqual(review.simulation_version, 'daily_exit_v1')
        changed = {**ml_cfg, 'fee_bps_per_side': 8}
        self.assertNotEqual(simulation_version(changed), review.simulation_version)

    def test_insufficient_qualified_stocks_stay_empty(self):
        cfg = load_config('config/weekly_strategy.yaml')
        rows = [ScoredStock(CandidateStock('600001', 'ST测试', 'b', {}),
                            ScoreBreakdown(trend=30, volume_turnover=20, breakout=20, risk=15), ''),
                ScoredStock(CandidateStock('600002', '低分', 'b', {}), ScoreBreakdown(trend=10), '')]
        selected, exceptions = select_screen_candidates(rows, cfg)
        self.assertEqual(selected, [])
        self.assertEqual(exceptions, [])

    def test_low_revenue_can_win_core_slot_without_exception(self):
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg['screening'].update(top_n=2, core_top_n=1, momentum_exception_n=1)
        stock = ScoredStock(CandidateStock('600108', '测试', 'b', {'营业收入同比增长率': -2.63}),
                            ScoreBreakdown(trend=30, volume_turnover=20, breakout=20, risk=15), '')
        selected, exceptions = select_screen_candidates([stock], cfg)
        self.assertEqual(selected, [stock])
        self.assertEqual(exceptions, [])

    def test_coverage_rejects_stale_market_but_respects_historical_cutoff(self):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.execute('CREATE TABLE eastmoney_stock_daily_klines(code TEXT,name TEXT,trade_date TEXT,close REAL)')
        conn.executemany('INSERT INTO eastmoney_stock_daily_klines VALUES (?, ?, ?, ?)',
                         [('600001', 'A', '2026-07-03', 10), ('600002', 'B', '2026-07-03', 10),
                          ('600001', 'A', '2026-09-11', 10), ('600003', 'NEW', '2026-09-11', 10)])
        require_market_coverage(conn, '2026-07-03', {'min_market_coverage': .9})
        with self.assertRaisesRegex(RuntimeError, '行情覆盖不足'):
            require_market_coverage(conn, '2026-09-11', {'min_market_coverage': .9})

    def test_cross_section_does_not_depend_on_future_tradeability(self):
        rows = [bar(f'2026-01-{i:02}', 10, 10.1, 9.9, 10) for i in range(1, 4)]
        cfg = {'lookback_trading_days': 0, 'horizon_trading_days': 1,
               'weekly_last_trading_day_only': False, 'sample_stride': 1}
        # B cannot be traded tomorrow, but must still participate in today's ranks.
        def label(series, idx, horizon, cfg):
            return (1, 5, 5, -1) if series is rows else None
        other = list(rows)
        with patch('weekly_stock.ml.features_at', side_effect=lambda series, idx: {'ret_5': 1 if series is rows else 2}), \
             patch('weekly_stock.ml.label_future', side_effect=label):
            samples = build_training_samples({'A': rows, 'B': other}, cfg)
        self.assertEqual(len(samples), 2)
        self.assertTrue(all(s.features['ret_5_rank'] == 0 for s in samples))

    def test_feedback_filters_execution_versions(self):
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        ensure_weekly_tables(conn)
        conn.execute("INSERT INTO weekly_review_runs(reviewed_run_id,review_date,config_json,created_at_utc) VALUES (1,'2026-09-04','{}','2026-09-04')")
        for code, version in [('600001', 'daily_exit_v1'), ('600002', 'next_open_v2:test')]:
            conn.execute("""INSERT INTO weekly_review_results (
                review_id,selected_id,code,base_trade_date,review_start_date,review_end_date,
                highest_gain_pct,close_gain_pct,max_drawdown_pct,is_complete,
                stop_loss_triggered,meets_expectation,notes,created_at_utc,simulation_version
                ) VALUES (1,1,?,'2026-08-28','2026-08-31','2026-09-04',5,3,-1,1,0,1,'','2026-09-04',?)""", (code,version))
        labels = review_feedback_labels(conn, as_of_date='2026-09-05', simulation_version='next_open_v2:test')
        self.assertEqual(labels, {('600002', '2026-08-28'): 1})
        recent = review_feedback_labels(conn, recent_runs=1, as_of_date='2026-09-05', simulation_version='next_open_v2:test')
        self.assertEqual(labels, recent)


if __name__ == '__main__':
    unittest.main()
