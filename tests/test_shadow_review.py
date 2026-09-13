import copy
import json
import unittest
import sqlite3
from contextlib import closing

from weekly_stock.models import Kline
from weekly_stock.shadow_review import evaluate_snapshot, review_shadow_runs


def bar(day, opening=10, close=10, high=10.1, low=9.9):
    return Kline(day, opening, close, high, low, 1000, 5, 0)


class ShadowReviewTests(unittest.TestCase):
    def fixture(self):
        run = dict(run_id=1, screen_date='2026-07-03', created_at_utc='2026-07-03T08:00:00Z')
        config = dict(review=dict(horizon_trading_days=2,expected_high_gain_pct=.05,
                                  expected_close_gain_pct=.02,stop_loss_pct=.06),
                      execution=dict(entry_mode='next_open',enforce_t_plus_one=True),top_ks=[1,3])
        stamp='2026-07-03T08:01:00Z'
        experiment=dict(variant='volume_half_v1',config_json=json.dumps(config),expected_count=2,recorded_at_utc=stamp)
        rows=[dict(code='a',original_rank=1,shadow_rank=2,recorded_at_utc=stamp),
              dict(code='b',original_rank=2,shadow_rank=1,recorded_at_utc=stamp)]
        days=['2026-07-03','2026-07-06','2026-07-07']
        bars={'a':[bar(days[0]),bar(days[1]),bar(days[2],9,9.2,9.3,8.9)],
              'b':[bar(days[0]),bar(days[1]),bar(days[2],11,11,11.1,10.9)]}
        return run, rows, experiment, bars, days

    def test_forward_snapshot_pairing_and_loss_decomposition(self):
        result=evaluate_snapshot(*self.fixture(),'2026-07-07')
        self.assertEqual(result['status'],'complete')
        pair=result['comparisons'][0]
        self.assertAlmostEqual(pair['exit_delta_pct'],20)
        self.assertEqual(pair['replaced_count'],1)
        self.assertEqual(pair['original']['categories']['gap_stop']['count'],1)
        self.assertEqual(pair['shadow']['categories']['take_profit']['count'],1)
        all_pair=result['comparisons'][1]
        self.assertEqual(all_pair['replaced_count'],0)
        self.assertEqual(all_pair['exit_delta_pct'],0)
        self.assertEqual(all_pair['original']['slots'],2)
        self.assertAlmostEqual(sum(r['contribution_pct'] for r in all_pair['original']['categories'].values()),
                               all_pair['original']['avg_exit_pct'])

    def test_pending_horizon_and_missing_stock_exclude_both_sides(self):
        args=list(self.fixture())
        result=evaluate_snapshot(*args,'2026-07-06')
        self.assertEqual(result['status'],'pending')
        self.assertEqual(result['comparisons'],[])
        args[3]['a'].pop()
        result=evaluate_snapshot(*args,'2026-07-07')
        self.assertEqual(result['status'],'pending')
        self.assertEqual(result['missing_codes'],['a'])
        self.assertFalse(result['comparisons'])

    def test_at_cutoff_or_late_snapshot_never_accepted(self):
        args=list(self.fixture())
        args[1][0]['recorded_at_utc']='2026-07-06T01:30:00Z'
        self.assertEqual(evaluate_snapshot(*args,'2026-07-07')['status'],'ineligible')
        args[1][0]['recorded_at_utc']='2026-07-03T08:01:00'
        self.assertEqual(evaluate_snapshot(*args,'2026-07-07')['status'],'ineligible')

    def test_rank_integrity_and_configuration_are_frozen(self):
        args=list(self.fixture())
        original=copy.deepcopy(args)
        first=evaluate_snapshot(*args,'2026-07-07')
        self.assertEqual(args,original)
        args[1][1]['original_rank']=1
        self.assertEqual(evaluate_snapshot(*args,'2026-07-07')['status'],'ineligible')
        self.assertTrue(first['simulation_version'].startswith('next_open_v2:'))

    def test_future_bars_do_not_affect_earlier_review(self):
        args=list(self.fixture())
        first=evaluate_snapshot(*args,'2026-07-07')
        args[3]['a'].append(bar('2026-07-08',99,99,100,98))
        self.assertEqual(first,evaluate_snapshot(*args,'2026-07-07'))

    def test_no_entry_counts_as_cash_not_executed_loss(self):
        args=list(self.fixture())
        args[3]['a'][1]=bar('2026-07-06',10,10,10,10)
        result=evaluate_snapshot(*args,'2026-07-07')
        value=result['comparisons'][0]['original']
        self.assertEqual(value['executed'],0)
        self.assertEqual(value['avg_exit_pct'],0)
        self.assertIsNone(value['avg_loss_pct'])
        self.assertEqual(value['categories']['no_entry']['count'],1)

    def test_intraday_stop_not_misclassified_as_gap(self):
        args=list(self.fixture())
        args[3]['a'][-1]=bar('2026-07-07',10,9.5,10.1,9)
        result=evaluate_snapshot(*args,'2026-07-07')
        categories=result['comparisons'][0]['original']['categories']
        self.assertEqual(set(categories),{'stop_loss'})
        self.assertAlmostEqual(categories['stop_loss']['avg_exit_pct'],-6)

    def test_database_pending_retry_and_completed_result_are_idempotent(self):
        run,rows,experiment,bars,days=self.fixture()
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.row_factory=sqlite3.Row
            conn.executescript('''
                CREATE TABLE weekly_screen_runs(run_id INTEGER,screen_date TEXT,created_at_utc TEXT);
                CREATE TABLE weekly_shadow_rankings(run_id INTEGER,variant TEXT,code TEXT,original_rank INTEGER,shadow_rank INTEGER,recorded_at_utc TEXT);
                CREATE TABLE weekly_shadow_experiments(run_id INTEGER,variant TEXT,config_json TEXT,expected_count INTEGER,recorded_at_utc TEXT);
                CREATE TABLE eastmoney_stock_daily_klines(code TEXT,name TEXT,trade_date TEXT,open REAL,close REAL,high REAL,low REAL,volume REAL,turnover_rate REAL,change_percent REAL);
            ''')
            conn.execute('INSERT INTO weekly_screen_runs VALUES (?,?,?)',(1,run['screen_date'],run['created_at_utc']))
            conn.execute('INSERT INTO weekly_shadow_experiments VALUES (?,?,?,?,?)',
                         (1,experiment['variant'],experiment['config_json'],2,experiment['recorded_at_utc']))
            for row in rows:
                code='000001' if row['code']=='a' else '000002'
                conn.execute('INSERT INTO weekly_shadow_rankings VALUES (?,?,?,?,?,?)',
                             (1,experiment['variant'],code,row['original_rank'],row['shadow_rank'],row['recorded_at_utc']))
                for b in bars[row['code']]:
                    conn.execute('INSERT INTO eastmoney_stock_daily_klines VALUES (?,?,?,?,?,?,?,?,?,?)',
                                 (code,'测试',b.trade_date,b.open,b.close,b.high,b.low,b.volume,b.turnover_rate,b.change_percent))
            self.assertEqual(review_shadow_runs(conn,'2026-07-06')[0]['status'],'pending')
            finished=review_shadow_runs(conn,'2026-07-07')
            self.assertEqual(finished[0]['status'],'complete')
            conn.execute('UPDATE eastmoney_stock_daily_klines SET open=99,close=99,high=100,low=98')
            self.assertEqual(review_shadow_runs(conn,'2026-07-07'),finished)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM weekly_shadow_reviews').fetchone()[0],1)
            earlier=review_shadow_runs(conn,'2026-07-06')[0]
            self.assertEqual(earlier['status'],'pending')
            self.assertFalse(earlier['comparisons'])


if __name__=='__main__':
    unittest.main()
