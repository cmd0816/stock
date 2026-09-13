"""Fixed, forward-recorded experiment; never updates production selection."""
from datetime import datetime, timezone
import json

VARIANT = 'volume_half_v1'


def half_volume_score(total, volume_score):
    return float(total) - .5 * float(volume_score)


def save_shadow_ranking(conn, run_id, selected, config=None):
    conn.execute('''CREATE TABLE IF NOT EXISTS weekly_shadow_rankings (
        run_id INTEGER NOT NULL, variant TEXT NOT NULL, code TEXT NOT NULL,
        name TEXT, original_rank INTEGER NOT NULL, shadow_rank INTEGER NOT NULL,
        original_score REAL NOT NULL, volume_score REAL NOT NULL, shadow_score REAL NOT NULL,
        recorded_at_utc TEXT NOT NULL, PRIMARY KEY(run_id, variant, code)
    )''')
    ranked = sorted(enumerate(selected, 1),
                    key=lambda pair: (-half_volume_score(pair[1].score.total, pair[1].score.volume_turnover),
                                      pair[1].candidate.code))
    recorded = datetime.now(timezone.utc).isoformat()
    if config is not None:
        conn.execute('''CREATE TABLE IF NOT EXISTS weekly_shadow_experiments (
            run_id INTEGER NOT NULL, variant TEXT NOT NULL, config_json TEXT NOT NULL,
            expected_count INTEGER NOT NULL, recorded_at_utc TEXT NOT NULL,
            PRIMARY KEY(run_id,variant))''')
        frozen = dict(review=config['review'], execution=config.get('execution', {}),
                      top_ks=config.get('ml', {}).get('backtest_top_ks', [3,5,10]))
        conn.execute('INSERT INTO weekly_shadow_experiments VALUES (?,?,?,?,?)',
                     (run_id, VARIANT, json.dumps(frozen, sort_keys=True), len(selected), recorded))
    for shadow_rank, (original_rank, item) in enumerate(ranked, 1):
        conn.execute('''INSERT INTO weekly_shadow_rankings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                     (run_id, VARIANT, item.candidate.code, item.candidate.name, original_rank,
                      shadow_rank, item.score.total, item.score.volume_turnover,
                      half_volume_score(item.score.total, item.score.volume_turnover), recorded))
    conn.commit()
