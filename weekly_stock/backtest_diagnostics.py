"""Read-only backtest membership audit and historical funnel diagnostics."""
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, time
from statistics import mean
import math
from zoneinfo import ZoneInfo


class BacktestReport(list):
    def __init__(self, metrics=(), *, run_audit=(), stages=()):
        super().__init__(metrics)
        self.run_audit = list(run_audit)
        self.stages = list(stages)
        self.exclusions = []
        self.factor_groups = []
        self.ablations = []


SCORE_COMPONENTS = ('trend_score', 'volume_turnover_score', 'breakout_score',
                    'fundamentals_score', 'risk_score')
TECHNICAL_FACTORS = ('ret_5', 'ret_20', 'close_ma20_pct', 'turnover_5',
                     'volume_ratio_5_20', 'dist_high_20')


def finite_number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def score_pool_for_fold(fold, rows, weekly_dates=None):
    """Require intact stored candidate membership and labels for every member."""
    by_run, accepted, audit = defaultdict(list), [], []
    samples = {(s.trade_date, s.code): s for s in fold.test_samples}
    for row in rows:
        if fold.test_start_date <= row['screen_date'] <= fold.test_end_date:
            by_run[row['run_id']].append(row)
    for run_id, candidates in sorted(by_run.items()):
        day = candidates[0]['screen_date']
        expected = int(candidates[0]['expected_candidate_count'])
        matched = [(r, samples.get((day, r['code']))) for r in candidates]
        count = sum(s is not None for _, s in matched)
        if weekly_dates is not None and day not in weekly_dates:
            status = 'outside_weekly_scope'
        elif len(candidates) != expected or len({r['code'] for r in candidates}) != expected:
            status = 'incomplete_scoring_snapshot'
        elif any(finite_number(r.get(key)) is None for r in candidates for key in ('total_score', *SCORE_COMPONENTS)):
            status = 'missing_score_component'
        elif count != expected:
            status = 'missing_features_or_execution_labels'
        else:
            status = 'complete'
            accepted.extend(matched)
        audit.append(dict(scope='score_pool', fold=fold.fold_no, run_id=run_id,
                          screen_date=day, available=count, expected=expected, status=status))
    return accepted, audit


def quantile_cuts(values, bins=3):
    """Thresholds depend only on training features, never test values/returns."""
    values = sorted(values)
    cuts = []
    for i in range(1, bins):
        position = (len(values) - 1) * i / bins
        low = int(position)
        value = values[low] + (values[min(low + 1, len(values) - 1)] - values[low]) * (position - low)
        if value > values[0] and value not in cuts:
            cuts.append(value)
    return cuts


def factor_groups_for_fold(fold, matched, scoring_rows, cfg):
    """Technical bins: purged market train set; score bins: stored train candidates.

    Each fold reports its own boundaries; do not pool different numeric bins or
    retrospectively manufacture historical fundamental scores.
    """
    minimum = int(cfg.get('diagnostic_min_train_samples', 20))
    bins = int(cfg.get('diagnostic_bins', 3))
    if minimum < 1 or bins < 2:
        raise ValueError('diagnostic_min_train_samples >= 1 and diagnostic_bins >= 2 required')
    train_keys = {(s.trade_date, s.code) for s in fold.train_samples}
    historical = [r for r in scoring_rows if (r['screen_date'], r['code']) in train_keys
                  and r['screen_date'] < fold.test_start_date]
    result = []
    for factor in (*TECHNICAL_FACTORS, *SCORE_COMPONENTS):
        is_score = factor in SCORE_COMPONENTS
        source = 'stored_train_candidates' if is_score else 'purged_market_train'
        raw = ([r.get(factor) for r in historical] if is_score else
               [s.features.get(factor) for s in fold.train_samples if s.trade_date < fold.test_start_date])
        train_values = [v for value in raw if (v := finite_number(value)) is not None]
        base = dict(fold=fold.fold_no, test_start=fold.test_start_date, test_end=fold.test_end_date,
                    factor=factor, train_source=source, train_count=len(train_values))
        if len(train_values) < minimum:
            result.append(dict(base, status='insufficient_train_values', bin=None, count=0,
                               hit_rate=None, avg_exit_pct=None))
            continue
        cuts = quantile_cuts(train_values, bins)
        grouped = defaultdict(list)
        for row, sample in matched:
            value = finite_number(row.get(factor) if is_score else sample.features.get(factor))
            grouped[None if value is None else bisect_right(cuts, value)].append(sample)
        for index in [*range(len(cuts) + 1), None]:
            observations = grouped[index]
            if index is None and not observations:
                continue
            status = 'missing_test_feature' if index is None else ('complete' if observations else 'empty_test_bin')
            result.append(dict(base, status=status, bin=index, cuts=cuts,
                               lower=None if index is None or index == 0 else cuts[index - 1],
                               upper=None if index is None or index == len(cuts) else cuts[index],
                               count=len(observations), dates=len({s.trade_date for s in observations}),
                               hit_rate=mean(s.label for s in observations) if observations and index is not None else None,
                               avg_exit_pct=mean(s.future_close_gain_pct for s in observations) if observations and index is not None else None))
    return result


def parse_timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed if parsed.utcoffset() is not None else None
    except ValueError:
        return None


def choose_canonical_runs(runs, trading_dates, cutoff_time='09:30'):
    """Choose before reading any future labels; never fall back after label loss."""
    clock = time.fromisoformat(cutoff_time)
    if clock.tzinfo is not None or clock > time(9, 30):
        raise ValueError('backtest_entry_cutoff_time must be a local time no later than 09:30')
    dates = sorted(set(trading_dates))
    audit, eligible = [], defaultdict(list)
    for run in sorted(runs, key=lambda r: (r['screen_date'], r['run_id'])):
        day = run['screen_date']
        idx = bisect_right(dates, day)
        row = dict(run_id=int(run['run_id']), screen_date=day,
                   created_at_utc=run.get('created_at_utc'), cutoff=None, status='')
        created = parse_timestamp(run.get('created_at_utc'))
        if day not in dates or idx >= len(dates):
            row['status'] = 'missing_trading_date_or_next_session'
        else:
            cutoff = datetime.combine(datetime.fromisoformat(dates[idx]).date(), clock,
                                      tzinfo=ZoneInfo('Asia/Shanghai'))
            close = datetime.combine(datetime.fromisoformat(day).date(), time(15),
                                     tzinfo=ZoneInfo('Asia/Shanghai'))
            row['cutoff'] = cutoff.isoformat()
            if created is None:
                row['status'] = 'unverifiable_timestamp'
            elif created < close:
                row['status'] = 'before_signal_close'
            elif created >= cutoff:
                row['status'] = 'at_or_after_entry_cutoff'
            else:
                row['status'] = 'alternate_same_date'
                eligible[day].append((created, int(run['run_id']), row))
        audit.append(row)
    chosen = set()
    for options in eligible.values():
        _, run_id, row = max(options, key=lambda item: (item[0], item[1]))
        row['status'] = 'canonical'
        chosen.add(run_id)
    return chosen, audit


def stage_diagnostics(samples, pools):
    """All-member descriptive returns, not a Top-K strategy or causal estimate.

    Partial labels get coverage only, never a survivor-only return average.
    Market comparisons are also supplied on dates where all three pools are
    complete, so summaries cannot silently compare different date ranges.
    """
    by_date = defaultdict(dict)
    for sample in samples:
        by_date[sample.trade_date][sample.code] = sample
    rows = []
    for pool in pools:
        available = by_date.get(pool['screen_date'])
        if not available:
            continue  # Outside the out-of-sample dates, not a training diagnostic.
        local = []
        for stage in ('market', 'upstream', 'selected'):
            codes = set(available) if stage == 'market' else pool.get(stage)
            expected = len(codes) if codes is not None else pool.get(stage + '_expected', 0)
            matched = [] if codes is None else [available[c] for c in sorted(codes) if c in available]
            status = pool.get(stage + '_status', 'complete')
            if codes is None:
                status = pool.get(stage + '_status', 'missing_snapshot')
            elif not expected:
                status = 'empty_pool'
            elif len(matched) != expected:
                status = 'incomplete_labels'
            complete = status == 'complete' and expected > 0 and len(matched) == expected
            local.append(dict(screen_date=pool['screen_date'], run_id=pool['run_id'],
                              stage=stage, expected=expected, available=len(matched), status=status,
                              hit_rate=mean(s.label for s in matched) if complete else None,
                              avg_exit_pct=mean(s.future_close_gain_pct for s in matched) if complete else None))
        comparable = all(row['status'] == 'complete' for row in local)
        for row in local:
            row['comparable'] = comparable
        rows.extend(local)
    return rows
