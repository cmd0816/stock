import contextlib
import copy
import io
import math
import unittest
from unittest.mock import patch

from weekly_stock.cli import print_backtest_metrics
from weekly_stock.ml import (
    BacktestPeriod, TrainingSample, WalkForwardFold, attach_rule_comparison,
    backtest_models, evaluate_predictions, weekly_bootstrap_intervals,
)


def sample(code, day, label, gain, main=.2, baseline=.8):
    return TrainingSample(code, day, {'main': main, 'baseline': baseline},
                          label, gain + 3, gain, -4)


class FakeModel:
    def __init__(self, key):
        self.key = key

    def predict_probability(self, features):
        return features[self.key]

    def predict_probabilities(self, features):
        return [self.predict_probability(row) for row in features]


class BacktestReportingTests(unittest.TestCase):
    def test_period_counts_weighting_and_negative_baseline(self):
        rows = [sample('a', '2026-08-21', 1, 5),
                sample('b', '2026-08-21', 0, -3),
                sample('a', '2026-08-28', 0, -1)]
        result = evaluate_predictions('test', rows, [.8, .7, .9], 10, 2)
        self.assertEqual(result.selected_count, 3)
        self.assertEqual([p.selected_count for p in result.periods], [2, 1])
        self.assertEqual([p.candidate_count for p in result.periods], [2, 1])
        self.assertEqual([p.hit_count for p in result.periods], [1, 0])
        self.assertAlmostEqual(result.always_negative_accuracy, 2 / 3)
        self.assertAlmostEqual(result.top_k_avg_close_gain_pct, 1 / 3)
        self.assertEqual(result.bootstrap_week_count, 2)
        again = evaluate_predictions('test', rows, [.8, .7, .9], 10, 2)
        self.assertEqual(result.hit_rate_ci, again.hit_rate_ci)
        self.assertEqual(result.avg_exit_ci, again.avg_exit_ci)

    def test_bootstrap_clusters_runs_and_dates_within_iso_week(self):
        periods = [BacktestPeriod('2026-08-27#1', 10, 2, 1, 3),
                   BacktestPeriod('2026-08-28#2', 10, 2, 1, -3)]
        self.assertEqual(weekly_bootstrap_intervals(periods), (1, None, None))
        self.assertEqual(weekly_bootstrap_intervals([]), (0, None, None))
        periods.append(BacktestPeriod('2026-09-04#3', 10, 2, 1, 0))
        # Within-week gains cancel; treating runs as independent would widen CI.
        self.assertEqual(weekly_bootstrap_intervals(periods), (2, (.5, .5), (0, 0)))

    def test_paired_bootstrap_uses_synchronized_week_differences(self):
        rows = [sample('a', '2026-08-21', 1, 20),
                sample('b', '2026-08-21', 1, 21),
                sample('a', '2026-08-28', 0, -20),
                sample('b', '2026-08-28', 0, -19)]
        rule = evaluate_predictions('rule', rows, [.9, .1, .9, .1], 10, 1)
        model = evaluate_predictions('model', rows, [.1, .9, .1, .9], 10, 1)
        attach_rule_comparison(model, rule)
        self.assertEqual(model.exit_delta_vs_rule_pct, 1)
        self.assertEqual(model.exit_delta_ci, (1, 1))
        self.assertEqual([p.exit_delta_vs_rule_pct for p in model.periods], [1, 1])
        invalid = copy.deepcopy(rule)
        invalid.periods[0].selected_count = 2
        with self.assertRaises(ValueError):
            attach_rule_comparison(model, invalid)
        invalid.periods.pop()
        with self.assertRaises(ValueError):
            attach_rule_comparison(model, invalid)

    def run_backtest(self, baseline='logistic_regression', partial=False):
        rows, candidates = [], []
        for run_id, day in enumerate(['2026-08-21', '2026-08-28'], 1):
            rows.extend([sample('a', day, 0, -3, .9, .1),
                         sample('b', day, 1, 5, .1, .9)])
            for rank, code in enumerate(['a', 'b'], 1):
                candidates.append(dict(run_id=run_id, screen_date=day, code=code,
                                       total_score=100 - rank, rank_no=rank))
        if partial:
            candidates.append(dict(run_id=1, screen_date='2026-08-21', code='missing',
                                   total_score=99, rank_no=3))
        fold = WalkForwardFold(1, rows, rows, '2026-08-21', '2026-08-28', 0)
        cfg = dict(model_name='lightgbm', baseline_model_name=baseline,
                   backtest_top_k=1, rule_score_weight=.25)
        with patch('weekly_stock.ml.purged_walk_forward_splits', return_value=[fold]), \
             patch('weekly_stock.ml.train_model', return_value=FakeModel('main')), \
             patch('weekly_stock.ml.train_logistic_regression_model', return_value=FakeModel('baseline')):
            return backtest_models(rows, cfg, rule_candidates=candidates)

    def test_baseline_paired_and_blend_share_exact_contest(self):
        metrics = {m.model_name: m for m in self.run_backtest()}
        names = ['rule_paired', 'ml_paired', 'lightgbm+rule_e2e',
                 'logistic_regression_paired', 'logistic_regression+rule_e2e']
        self.assertEqual(len(metrics), 7)
        for name in names:
            self.assertEqual(metrics[name].test_count, 4)
            self.assertEqual(metrics[name].selected_count, 2)
            self.assertEqual([p.period for p in metrics[name].periods],
                             ['2026-08-21#1', '2026-08-28#2'])
        self.assertEqual(metrics['ml_paired'].top_k_hit_rate, 0)
        self.assertEqual(metrics['logistic_regression_paired'].top_k_hit_rate, 1)
        self.assertEqual(metrics['logistic_regression+rule_e2e'].exit_delta_ci, (8, 8))
        self.assertTrue(math.isnan(metrics['logistic_regression+rule_e2e'].brier_score))
        self.assertFalse(math.isnan(metrics['logistic_regression_paired'].brier_score))

    def test_partial_run_excluded_from_every_paired_model(self):
        with self.assertWarnsRegex(UserWarning, 'executable labels 2/3'):
            metrics = self.run_backtest(partial=True)
        for metric in metrics[2:]:
            self.assertEqual(metric.test_count, 2)
            self.assertEqual([p.period for p in metric.periods], ['2026-08-28#2'])
            self.assertIsNone(metric.avg_exit_ci)

    def test_baseline_disabled_and_empty_evaluation(self):
        metrics = self.run_backtest(baseline='none')
        self.assertEqual(len([m for m in metrics if m.scope != 'rerank']), 4)
        self.assertFalse(any('logistic' in m.model_name for m in metrics))
        result = evaluate_predictions('empty', [], [], 0, 10)
        self.assertEqual(result.selected_count, 0)
        self.assertIsNone(result.avg_exit_ci)
        with self.assertRaises(ValueError):
            evaluate_predictions('bad', [], [.5], 0, 10)

    def test_ties_are_deterministic_by_stock_code(self):
        rows = [sample('b', '2026-08-28', 0, -1), sample('a', '2026-08-28', 1, 5)]
        result = evaluate_predictions('test', rows, [.5, .5], 0, 1)
        self.assertEqual(result.top_k_hit_rate, 1)

    def test_cli_explains_metric_scope_and_shows_details(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            print_backtest_metrics(self.run_backtest())
        report = output.getvalue()
        for token in ['all_neg', 'top_n', 'avg_adverse', '不是账户净值最大回撤',
                      '配对 CI', '2026-08-21#1', 'logistic_regression_paired', '2,000']:
            self.assertIn(token, report)
        self.assertNotIn('avg_drawdown', report)


if __name__ == '__main__':
    unittest.main()
