"""Audit source growth fields without manufacturing missing financial data."""
import math
import re
from collections import Counter


def growth_field(row, kind):
    matches = []
    for key, raw in row.items():
        title = re.sub(r'\s+', '', str(key))
        if title.startswith('_') or '同比' not in title:
            continue
        if kind == 'revenue':
            relevant = '营业' in title and '收入' in title
        elif kind == 'profit':
            relevant = '净利润' in title and '扣非' not in title and '扣除非' not in title
        else:
            raise ValueError('Unknown growth field')
        if relevant:
            matches.append((title, raw))
    if any('最新' in key for key, _ in matches):
        matches = [(key, raw) for key, raw in matches if '最新' in key]
    if not matches:
        return dict(status='missing_column', value=None, sources=[])
    parsed = []
    for key, raw in matches:
        # The suffix is a report-period label, never the numeric growth value.
        text = '' if raw is None else str(raw).split('|', 1)[0].strip().replace(',', '').replace('，', '')
        text = text.replace('％', '%').removesuffix('%').strip()
        if not text or text in ('-', '--', 'N/A', 'null', 'None'):
            status, value = 'missing_value', None
        elif re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)', text):
            value = float(text)
            status = 'available' if math.isfinite(value) else 'invalid_value'
            if status != 'available':
                value = None
        else:
            status, value = 'invalid_value', None
        parsed.append(dict(column=key, raw=raw, value=value, status=status))
    values = {item['value'] for item in parsed if item['status'] == 'available'}
    if len(values) > 1 or (values and any(item['status'] != 'available' for item in parsed)):
        status, value = 'ambiguous_columns', None
    elif values:
        status, value = 'available', next(iter(values))
    else:
        status = 'invalid_value' if any(item['status'] == 'invalid_value' for item in parsed) else 'missing_value'
        value = None
    return dict(status=status, value=value, sources=parsed)


def field_assessment(row, kind, threshold):
    result = growth_field(row, kind)
    result['assessment'] = ('unknown' if result['value'] is None else
                            'pass' if result['value'] >= threshold else 'below_threshold')
    return result


def coverage_report(rows):
    rows = list(rows)
    return dict(count=len(rows), fields={
        kind: dict(Counter(growth_field(row, kind)['status'] for row in rows))
        for kind in ('revenue', 'profit')
    })


def print_coverage(rows, label):
    report = coverage_report(rows)
    print(f"Fundamental coverage [{label}]: rows={report['count']}")
    for kind, counts in report['fields'].items():
        print(f"  {kind}: available={counts.get('available', 0)}/{report['count']} states={counts}")
    if report['fields']['profit'].get('available', 0) < report['count']:
        print('Warning: 利润同比数据不完整；请在源导出中增加净利润同比增长率列（仅展示指标，不新增筛选门槛）。缺失不等于不达标，不用当前财报回填历史。')
    return report
