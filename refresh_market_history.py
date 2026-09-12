#!/usr/bin/env python3
"""Refresh the whole A-share pool; --check reports coverage without network/writes.

Refresh the full modeling window for stale symbols to avoid splicing differently
adjusted price series. Successful symbols are skipped on resume.
"""
import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from weekly_stock.config import load_config
from weekly_stock.data_quality import market_coverage, require_market_coverage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='stocks.db')
    parser.add_argument('--config', default='config/weekly_strategy.yaml')
    parser.add_argument('--end-date', required=True, help='China trading date YYYY-MM-DD')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    path = Path(args.db).resolve()
    cfg = load_config(args.config)['ml']
    if not args.check:
        root = Path(__file__).resolve().parent
        subprocess.run([
            sys.executable, '-u', str(root / 'baostock_to_sqlite.py'),
            '--db', str(path), '--all-a-shares', '--end-date', args.end_date,
            '--days', str(max(540, int(cfg.get('history_limit', 320)) * 2)),
            '--adjust', 'qfq', '--delay', '0', '--skip-existing-days',
            str(int(cfg.get('history_limit', 320))),
        ], check=True)
    with sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True) as conn:
        report = market_coverage(conn, args.end_date, int(cfg.get('lookback_trading_days', 60)))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        require_market_coverage(conn, args.end_date, cfg)


if __name__ == '__main__':
    main()
