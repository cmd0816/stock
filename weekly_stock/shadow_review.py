"""Forward-only evaluation of frozen rankings and frozen execution settings."""
import json
from dataclasses import asdict
from datetime import datetime, time, timedelta
from statistics import mean
from zoneinfo import ZoneInfo

from . import db
from .backtest_diagnostics import parse_timestamp
from .trade_simulator import simulate_trade, execution_options, simulation_version
from .shadow import VARIANT


def summarize_trades(codes, outcomes, stop):
    trades = [outcomes[code] for code in codes]
    executed = [o for o in trades if o.exit_reason != 'no_entry']
    wins = [o.realized_gain_pct for o in executed if o.realized_gain_pct > 0]
    losses = [o.realized_gain_pct for o in executed if o.realized_gain_pct < 0]
    categories = {}
    for outcome in trades:
        category = outcome.exit_reason
        if category == 'stop_loss' and outcome.exit_at_open and outcome.realized_gain_pct < -stop * 100 - 1e-8:
            category = 'gap_stop'
        categories.setdefault(category, []).append(outcome.realized_gain_pct)
    return dict(codes=codes, slots=len(trades), executed=len(executed),
                hit_rate=sum(o.label for o in trades)/len(trades) if trades else None,
                avg_exit_pct=mean(o.realized_gain_pct for o in trades) if trades else None,
                avg_win_pct=mean(wins) if wins else None, avg_loss_pct=mean(losses) if losses else None,
                win_rate=len(wins)/len(executed) if executed else None,
                categories={key: dict(count=len(values), avg_exit_pct=mean(values),
                                      contribution_pct=sum(values)/len(trades)) for key, values in categories.items()})


def evaluate_snapshot(run, rows, experiment, klines, trading_dates, as_of_date):
    result = dict(run_id=run['run_id'], variant=experiment['variant'], as_of_date=as_of_date,
                  status='pending', reason='', comparisons=[])
    def stop(reason, status='pending'):
        result.update(status=status, reason=reason)
        return result
    cfg = json.loads(experiment['config_json'])
    review, execution = cfg['review'], cfg.get('execution', {})
    if execution_options(execution)['entry_mode'] != 'next_open':
        return stop('unsupported_entry_mode', 'ineligible')
    day = run['screen_date']
    dates = sorted({d for d in trading_dates if day < d <= as_of_date})
    horizon = int(review['horizon_trading_days'])
    if horizon < 1:
        return stop('invalid_horizon', 'ineligible')
    if not dates:
        return stop('waiting_for_next_session')
    cutoff = datetime.combine(datetime.fromisoformat(dates[0]).date(), time(9,30), ZoneInfo('Asia/Shanghai'))
    close = datetime.combine(datetime.fromisoformat(day).date(), time(15), ZoneInfo('Asia/Shanghai'))
    stamps = [parse_timestamp(run['created_at_utc']), parse_timestamp(experiment['recorded_at_utc'])]
    stamps.extend(parse_timestamp(row['recorded_at_utc']) for row in rows)
    if any(stamp is None or not close <= stamp < cutoff for stamp in stamps):
        return stop('snapshot_not_verified_before_entry', 'ineligible')
    if any(stamp < stamps[0] for stamp in stamps[1:]):
        return stop('snapshot_precedes_screen_run', 'ineligible')
    count = experiment['expected_count']
    if (count < 1 or len(rows) != count or len({r['code'] for r in rows}) != count
            or any(sorted(r[key] for r in rows) != list(range(1,count+1)) for key in ('original_rank','shadow_rank'))):
        return stop('incomplete_or_invalid_frozen_rankings', 'ineligible')
    if len(dates) < horizon:
        return stop(f'waiting_for_horizon:{len(dates)}/{horizon}')
    dates = dates[:horizon]
    outcomes, missing = {}, []
    for row in rows:
        code = row['code']
        bars = [bar for bar in klines.get(code, []) if bar.trade_date <= dates[-1]]
        positions = {bar.trade_date: i for i, bar in enumerate(bars)}
        if day not in positions or any(d not in positions for d in dates):
            missing.append(code)
            continue
        outcome = simulate_trade(bars, positions[day], horizon,
                                 high_target_pct=float(review['expected_high_gain_pct']),
                                 close_target_pct=float(review['expected_close_gain_pct']),
                                 stop_loss_pct=float(review['stop_loss_pct']),
                                 target_logic=review.get('positive_target_logic','any'),
                                 use_exit_rules=review.get('use_trade_exit_rules',True),
                                 exit_on_break_ma20=review.get('exit_on_break_ma20',False),
                                 **execution_options(execution))
        if outcome is None:
            missing.append(code)
        else:
            outcomes[code] = outcome
    if missing:
        result['missing_codes'] = missing
        return stop('incomplete_bars_or_unverifiable_exit')
    top_ks = sorted({int(k) for k in cfg['top_ks']})
    if not top_ks:
        return stop('missing_top_k', 'ineligible')
    for k in top_ks:
        if k < 1:
            return stop('invalid_top_k', 'ineligible')
        original = [r['code'] for r in sorted(rows, key=lambda r:r['original_rank'])[:k]]
        shadow = [r['code'] for r in sorted(rows, key=lambda r:r['shadow_rank'])[:k]]
        a = summarize_trades(original, outcomes, float(review['stop_loss_pct']))
        b = summarize_trades(shadow, outcomes, float(review['stop_loss_pct']))
        result['comparisons'].append(dict(k=k, original=a, shadow=b,
                                          replaced_count=len(set(shadow)-set(original)),
                                          exit_delta_pct=b['avg_exit_pct']-a['avg_exit_pct']))
    result.update(status='complete', reason='', review_end_date=dates[-1],
                  simulation_version=simulation_version({**review, **execution}, review=True),
                  outcomes={code:asdict(outcome) for code,outcome in outcomes.items()})
    return result


def review_shadow_runs(conn, as_of_date, run_id=None):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'weekly_shadow_rankings' not in tables:
        return []
    ids = [r[0] for r in conn.execute('SELECT DISTINCT run_id FROM weekly_shadow_rankings WHERE variant=? ORDER BY run_id', (VARIANT,))
           if run_id is None or r[0] == run_id]
    conn.execute('''CREATE TABLE IF NOT EXISTS weekly_shadow_reviews (
        run_id INTEGER NOT NULL, variant TEXT NOT NULL, as_of_date TEXT NOT NULL,
        status TEXT NOT NULL, result_json TEXT NOT NULL, PRIMARY KEY(run_id,variant))''')
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    latest_complete_day = (now.date() if now.time() >= time(15) else now.date()-timedelta(days=1)).isoformat()
    as_of_date = min(as_of_date, latest_complete_day)
    dates = [r[0] for r in conn.execute(f'SELECT DISTINCT trade_date FROM eastmoney_stock_daily_klines WHERE {db.stock_kline_filter_sql()} AND trade_date<=? ORDER BY trade_date', (as_of_date,))]
    reports = []
    for identifier in ids:
        saved = conn.execute('SELECT * FROM weekly_shadow_reviews WHERE run_id=? AND variant=?', (identifier,VARIANT)).fetchone()
        if saved and saved['status'] == 'complete':
            if saved['as_of_date'] <= as_of_date:
                reports.append(json.loads(saved['result_json']))
            else:
                reports.append(dict(run_id=identifier,status='pending',reason='requested_date_precedes_saved_review',comparisons=[]))
            continue
        rows = [dict(r) for r in conn.execute('SELECT * FROM weekly_shadow_rankings WHERE run_id=? AND variant=?', (identifier,VARIANT))]
        experiment = (conn.execute('SELECT * FROM weekly_shadow_experiments WHERE run_id=? AND variant=?',(identifier,VARIANT)).fetchone()
                      if 'weekly_shadow_experiments' in tables else None)
        run = conn.execute('SELECT * FROM weekly_screen_runs WHERE run_id=?', (identifier,)).fetchone()
        if experiment is None or run is None:
            reports.append(dict(run_id=identifier,status='ineligible',reason='missing_frozen_config_or_run',comparisons=[]))
            continue
        bars = {r['code']: db.load_klines(conn,r['code'],limit=-1,as_of_date=as_of_date) for r in rows}
        result = evaluate_snapshot(dict(run), rows, dict(experiment), bars, dates, as_of_date)
        conn.execute('''INSERT INTO weekly_shadow_reviews VALUES (?,?,?,?,?)
            ON CONFLICT(run_id,variant) DO UPDATE SET as_of_date=excluded.as_of_date,status=excluded.status,result_json=excluded.result_json''',
                     (identifier,result['variant'],as_of_date,result['status'],json.dumps(result,ensure_ascii=False)))
        reports.append(result)
    conn.commit()
    return reports


def print_shadow_reviews(reports):
    if not reports:
        print('No forward shadow snapshots to review.')
    for report in reports:
        print(f"Shadow review run={report['run_id']} status={report['status']} reason={report['reason']}")
        for comparison in report['comparisons']:
            print(f"  K={comparison['k']} replaced={comparison['replaced_count']} delta={comparison['exit_delta_pct']:+.2f}pp")
            for name in ('original','shadow'):
                value=comparison[name]
                print(f"    {name}: slots={value['slots']} executed={value['executed']} hit={value['hit_rate']:.1%} avg_exit={value['avg_exit_pct']:+.2f}% avg_win={value['avg_win_pct']} avg_loss={value['avg_loss_pct']}")
                print(f"      exits={json.dumps(value['categories'],ensure_ascii=False)}")
    print('分类互斥：take_profit止盈、stop_loss普通止损、gap_stop开盘穿越止损、horizon到期、break_ma20、no_entry未建仓。')
    print('收益按固定选中槽位等权，未建仓计现金0收益；各类别contribution_pct相加等于平均收益，不是账户净值。')
