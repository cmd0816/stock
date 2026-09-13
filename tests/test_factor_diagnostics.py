import contextlib
import copy
import io
import unittest
import warnings
from dataclasses import replace
from unittest.mock import patch

from weekly_stock.backtest_diagnostics import (
    SCORE_COMPONENTS, factor_groups_for_fold, quantile_cuts, score_pool_for_fold,
)
from weekly_stock.cli import print_backtest_metrics
from weekly_stock.ml import TrainingSample, WalkForwardFold, backtest_models


def sample(code, day, value=1, gain=2):
    return TrainingSample(code, day, {'ret_5': value}, int(gain > 0), gain + 2, gain, -4)


def row(code, day, run_id=1, total=50, trend=10, expected=2):
    result = dict(code=code, screen_date=day, run_id=run_id, total_score=total,
                  trend_score=trend, expected_candidate_count=expected, rank_no=int(code))
    result.update({key: 5 for key in SCORE_COMPONENTS if key != 'trend_score'})
    return result


class Model:
    def predict_probability(self, features):
        return .5

    def predict_probabilities(self, features):
        return [.5] * len(features)


class FactorDiagnosticsTests(unittest.TestCase):
    def fixture(self):
        train = [sample(str(i), '2026-07-03', i, 1) for i in range(1, 7)]
        test = [sample('1', '2026-07-10', 2, -4), sample('2', '2026-07-10', 5, 5)]
        fold = WalkForwardFold(1, train, test, '2026-07-10', '2026-07-10', 0)
        test_rows = [row('1', '2026-07-10', total=60, trend=40),
                     row('2', '2026-07-10', total=50, trend=10)]
        return fold, test_rows

    def test_boundaries_ignore_future_features_labels_and_returns(self):
        fold, test_rows = self.fixture()
        cfg = dict(diagnostic_min_train_samples=2, diagnostic_bins=3)
        matched = list(zip(test_rows, fold.test_samples))
        first = factor_groups_for_fold(fold, matched, test_rows, cfg)
        # Adversarial future values, including an erroneously supplied future train row.
        changed = replace(fold, train_samples=fold.train_samples + [sample('99', '2026-07-17', 99999)])
        future = [(r, replace(s, features={'ret_5': 999999}, label=1-s.label,
                              future_close_gain_pct=10000)) for r, s in matched]
        second = factor_groups_for_fold(changed, future, test_rows, cfg)
        a = [r for r in first if r['factor']=='ret_5']
        b = [r for r in second if r['factor']=='ret_5']
        self.assertEqual([r['cuts'] for r in a], [r['cuts'] for r in b])
        self.assertEqual(a[0]['train_count'], 6)
        self.assertEqual(b[0]['train_count'], 6)
        self.assertNotEqual([r['count'] for r in a], [r['count'] for r in b])

    def test_score_bins_only_use_available_historical_train_candidates(self):
        fold, test_rows = self.fixture()
        train_rows = [row(str(i), '2026-07-03', run_id=0, trend=i*5, expected=6) for i in range(1, 7)]
        output = factor_groups_for_fold(fold, list(zip(test_rows, fold.test_samples)),
                                        train_rows + test_rows, {'diagnostic_min_train_samples': 2})
        trend = [r for r in output if r['factor']=='trend_score']
        self.assertEqual(trend[0]['train_count'], 6)
        self.assertEqual(trend[0]['train_source'], 'stored_train_candidates')
        self.assertEqual(trend[0]['cuts'], quantile_cuts([5, 10, 15, 20, 25, 30]))
        no_history = factor_groups_for_fold(fold, list(zip(test_rows, fold.test_samples)), test_rows, {})
        self.assertEqual(next(r for r in no_history if r['factor']=='trend_score')['status'],
                         'insufficient_train_values')

    def test_discrete_constant_and_missing_test_values(self):
        self.assertEqual(quantile_cuts([3, 3, 3]), [])
        self.assertEqual(quantile_cuts([0, 30, 30]), [20, 30])
        fold, test_rows = self.fixture()
        matched = [(test_rows[0], replace(fold.test_samples[0], features={})),
                   (test_rows[1], fold.test_samples[1])]
        output = factor_groups_for_fold(fold, matched, [], {'diagnostic_min_train_samples': 2})
        missing = next(r for r in output if r['factor']=='ret_5' and r['status']=='missing_test_feature')
        self.assertEqual(missing['count'], 1)
        self.assertIsNone(missing['avg_exit_pct'])

    def test_incomplete_membership_or_components_excludes_entire_contest(self):
        fold, rows = self.fixture()
        accepted, audit = score_pool_for_fold(fold, rows[:1])
        self.assertFalse(accepted)
        self.assertEqual(audit[0]['status'], 'incomplete_scoring_snapshot')
        rows[0]['risk_score'] = None
        accepted, audit = score_pool_for_fold(fold, rows)
        self.assertFalse(accepted)
        self.assertEqual(audit[0]['status'], 'missing_score_component')
        rows[0]['risk_score'] = 5
        accepted, audit = score_pool_for_fold(replace(fold, test_samples=fold.test_samples[:1]), rows)
        self.assertFalse(accepted)
        self.assertEqual(audit[0]['status'], 'missing_features_or_execution_labels')

    def run_report(self, context, fold):
        cfg = dict(model_name='test', baseline_model_name='none', backtest_top_k=1,
                   weekly_last_trading_day_only=True, diagnostic_min_train_samples=2)
        with patch('weekly_stock.ml.purged_walk_forward_splits', return_value=[fold]), \
             patch('weekly_stock.ml.train_model', return_value=Model()):
            return backtest_models(fold.train_samples + fold.test_samples, cfg,
                                   rule_candidates=context.get('rule_candidates', []), screen_context=context)

    def test_ablation_changes_only_component_and_keeps_contests_paired(self):
        fold, rows = self.fixture()
        original = copy.deepcopy(rows)
        report = self.run_report({'scoring_candidates': rows}, fold)
        ablations = {m.model_name: m for m in report.ablations if m.scope=='score_pool'}
        self.assertEqual(len(ablations), 7)
        self.assertEqual(ablations['score_original'].top_k_avg_close_gain_pct, -4)
        self.assertEqual(ablations['without_trend_score'].top_k_avg_close_gain_pct, 5)
        self.assertEqual(ablations['without_trend_score'].exit_delta_vs_rule_pct, 9)
        self.assertEqual(ablations['without_risk_score'].exit_delta_vs_rule_pct, 0)
        for metric in report.ablations:
            self.assertEqual(metric.test_count, 2)
            self.assertEqual(metric.selected_count, 1)
        self.assertEqual(rows, original)  # Historical scores were not mutated.

    def test_midweek_exclusion_is_not_reported_as_missing_market_data(self):
        fold, rows = self.fixture()
        midweek = row('1', '2026-07-08', run_id=2, expected=1)
        fold = replace(fold, test_start_date='2026-07-06')
        context = dict(rule_candidates=[midweek], scoring_candidates=[midweek],
                       trading_dates=['2026-07-06', '2026-07-07', '2026-07-08', '2026-07-09', '2026-07-10'])
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter('always')
            report = self.run_report(context, fold)
        self.assertTrue(any('outside weekly evaluation' in str(w.message) for w in captured))
        self.assertFalse(any('executable labels 0/' in str(w.message) for w in captured))
        self.assertTrue(all(r['status']=='outside_weekly_scope' for r in report.exclusions))
        self.assertFalse(report.ablations)

    def test_expected_week_end_with_missing_labels_keeps_real_warning(self):
        fold, rows = self.fixture()
        missing = row('3', '2026-07-10', expected=1)
        with self.assertWarnsRegex(UserWarning, 'executable labels 0/1'):
            report = self.run_report(dict(rule_candidates=[missing], trading_dates=['2026-07-09', '2026-07-10']), fold)
        self.assertEqual(report.exclusions[0]['status'], 'missing_features_or_execution_labels')

    def test_cli_shows_boundaries_sources_and_ablation_reference(self):
        fold, rows = self.fixture()
        report = self.run_report({'scoring_candidates': rows}, fold)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            print_backtest_metrics(report)
        for token in ['purged_market_train', 'insufficient_train_values', 'score_original',
                      'without_trend_score', 'delta_vs_original_pp', '不重新运行生产的门槛/双通道']:
            self.assertIn(token, output.getvalue())


if __name__ == '__main__':
    unittest.main()
