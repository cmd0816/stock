from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .models import CandidateStock, Kline, ReviewResult, ScoredStock


NON_STOCK_NAME_PATTERNS = ("指数", "上证", "中证", "沪深", "基金", "国债", "企债", "等权")


def stock_kline_filter_sql(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    code_col = f"{prefix}code"
    name_col = f"{prefix}name"
    code_sql = " OR ".join(
        f"{code_col} GLOB '{pattern}'"
        for pattern in (
            "00[0-9][0-9][0-9][0-9]",
            "30[0-9][0-9][0-9][0-9]",
            "60[0-9][0-9][0-9][0-9]",
            "68[0-9][0-9][0-9][0-9]",
            "4[0-9][0-9][0-9][0-9][0-9]",
            "8[0-9][0-9][0-9][0-9][0-9]",
            "9[0-9][0-9][0-9][0-9][0-9]",
        )
    )
    name_sql = " ".join(
        f"AND COALESCE({name_col}, '') NOT LIKE '%{pattern}%'"
        for pattern in NON_STOCK_NAME_PATTERNS
    )
    return f"({code_sql}) {name_sql}"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def ensure_weekly_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS weekly_screen_runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            screen_date TEXT NOT NULL,
            xuangu_batch_id TEXT,
            strategy_config_json TEXT NOT NULL,
            screening_text TEXT,
            candidate_count INTEGER NOT NULL,
            selected_count INTEGER NOT NULL,
            created_at_utc TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS weekly_screen_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            code TEXT NOT NULL,
            name TEXT,
            rank_no INTEGER NOT NULL,
            total_score REAL NOT NULL,
            trend_score REAL NOT NULL,
            volume_turnover_score REAL NOT NULL,
            breakout_score REAL NOT NULL,
            fundamentals_score REAL NOT NULL,
            risk_score REAL NOT NULL,
            selected INTEGER NOT NULL,
            selected_reason TEXT,
            row_json TEXT NOT NULL,
            score_json TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES weekly_screen_runs(run_id)
        );

        CREATE TABLE IF NOT EXISTS weekly_selected_stocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            screen_date TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT,
            rank_no INTEGER NOT NULL,
            total_score REAL NOT NULL,
            selected_reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'selected',
            created_at_utc TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES weekly_screen_runs(run_id)
        );

        CREATE TABLE IF NOT EXISTS weekly_review_runs (
            review_id INTEGER PRIMARY KEY AUTOINCREMENT,
            reviewed_run_id INTEGER NOT NULL,
            review_date TEXT NOT NULL,
            config_json TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS weekly_review_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            review_id INTEGER NOT NULL,
            selected_id INTEGER NOT NULL,
            code TEXT NOT NULL,
            name TEXT,
            base_trade_date TEXT,
            review_start_date TEXT,
            review_end_date TEXT,
            highest_gain_pct REAL,
            close_gain_pct REAL,
            max_drawdown_pct REAL,
            stop_loss_triggered INTEGER NOT NULL,
            meets_expectation INTEGER NOT NULL,
            best_exit_meets_expectation INTEGER NOT NULL DEFAULT 0,
            notes TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            FOREIGN KEY (review_id) REFERENCES weekly_review_runs(review_id),
            FOREIGN KEY (selected_id) REFERENCES weekly_selected_stocks(id)
        );

        CREATE TABLE IF NOT EXISTS weekly_ml_model_runs (
            model_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_run_id INTEGER NOT NULL,
            model_name TEXT NOT NULL,
            config_json TEXT NOT NULL,
            train_sample_count INTEGER NOT NULL,
            positive_sample_count INTEGER NOT NULL,
            model_json TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            FOREIGN KEY (source_run_id) REFERENCES weekly_screen_runs(run_id)
        );

        CREATE TABLE IF NOT EXISTS weekly_ml_training_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model_run_id INTEGER NOT NULL,
            source_run_id INTEGER NOT NULL,
            code TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            label INTEGER NOT NULL,
            future_high_gain_pct REAL NOT NULL,
            future_close_gain_pct REAL NOT NULL,
            future_max_drawdown_pct REAL NOT NULL,
            feature_json TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            FOREIGN KEY (model_run_id) REFERENCES weekly_ml_model_runs(model_run_id),
            FOREIGN KEY (source_run_id) REFERENCES weekly_screen_runs(run_id)
        );

        CREATE TABLE IF NOT EXISTS weekly_ml_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model_run_id INTEGER NOT NULL,
            source_run_id INTEGER NOT NULL,
            code TEXT NOT NULL,
            name TEXT,
            probability_up REAL NOT NULL,
            predicted_score REAL NOT NULL,
            feature_json TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            UNIQUE(source_run_id, code),
            FOREIGN KEY (model_run_id) REFERENCES weekly_ml_model_runs(model_run_id),
            FOREIGN KEY (source_run_id) REFERENCES weekly_screen_runs(run_id)
        );

        CREATE TABLE IF NOT EXISTS weekly_ml_prediction_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model_run_id INTEGER NOT NULL,
            source_run_id INTEGER NOT NULL,
            code TEXT NOT NULL,
            name TEXT,
            probability_up REAL NOT NULL,
            predicted_score REAL NOT NULL,
            feature_json TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            UNIQUE(model_run_id, code),
            FOREIGN KEY (model_run_id) REFERENCES weekly_ml_model_runs(model_run_id),
            FOREIGN KEY (source_run_id) REFERENCES weekly_screen_runs(run_id)
        );

        CREATE INDEX IF NOT EXISTS idx_weekly_candidates_run ON weekly_screen_candidates(run_id);
        CREATE INDEX IF NOT EXISTS idx_weekly_selected_run ON weekly_selected_stocks(run_id);
        CREATE INDEX IF NOT EXISTS idx_weekly_review_run ON weekly_review_results(review_id);
        CREATE INDEX IF NOT EXISTS idx_weekly_ml_samples_model ON weekly_ml_training_samples(model_run_id);
        CREATE INDEX IF NOT EXISTS idx_weekly_ml_samples_code_date ON weekly_ml_training_samples(code, trade_date);
        CREATE INDEX IF NOT EXISTS idx_weekly_ml_predictions_run ON weekly_ml_predictions(source_run_id);
        CREATE INDEX IF NOT EXISTS idx_weekly_ml_prediction_history_run
            ON weekly_ml_prediction_history(source_run_id, model_run_id);
        """
    )
    _migrate_weekly_review_runs_to_append_mode(conn)
    _add_column_if_missing(conn, "weekly_review_results", "best_exit_meets_expectation", "INTEGER NOT NULL DEFAULT 0")
    ensure_market_context_tables(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_weekly_review_runs_run_id ON weekly_review_runs(reviewed_run_id)")
    conn.execute(
        """
        INSERT OR IGNORE INTO weekly_ml_prediction_history (
            model_run_id, source_run_id, code, name, probability_up,
            predicted_score, feature_json, reason, created_at_utc
        )
        SELECT
            model_run_id, source_run_id, code, name, probability_up,
            predicted_score, feature_json, reason, created_at_utc
        FROM weekly_ml_predictions
        """
    )
    conn.commit()



def _migrate_weekly_review_runs_to_append_mode(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='weekly_review_runs'"
    ).fetchone()
    if row is None:
        return
    sql = str(row[0] or "").upper()
    if "UNIQUE(REVIEWED_RUN_ID)" not in sql:
        return

    conn.execute("DROP INDEX IF EXISTS idx_weekly_review_runs_run_id")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS weekly_review_runs_new ("
        "review_id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "reviewed_run_id INTEGER NOT NULL,"
        "review_date TEXT NOT NULL,"
        "config_json TEXT NOT NULL,"
        "created_at_utc TEXT NOT NULL"
        ")"
    )
    conn.execute(
        "INSERT INTO weekly_review_runs_new (review_id, reviewed_run_id, review_date, config_json, created_at_utc) "
        "SELECT review_id, reviewed_run_id, review_date, config_json, created_at_utc "
        "FROM weekly_review_runs ORDER BY review_id"
    )
    conn.execute("DROP TABLE weekly_review_runs")
    conn.execute("ALTER TABLE weekly_review_runs_new RENAME TO weekly_review_runs")


def _add_column_if_missing(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    col_def: str,
) -> None:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if any(str(r["name"]) == column for r in rows):
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")


def ensure_market_context_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS stock_fund_flow_daily (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            main_net_inflow REAL,
            main_net_inflow_ratio REAL,
            source_url TEXT NOT NULL,
            raw_line TEXT NOT NULL,
            fetched_at_utc TEXT NOT NULL,
            UNIQUE(code, trade_date)
        );

        CREATE TABLE IF NOT EXISTS stock_sector_map (
            code TEXT PRIMARY KEY,
            sector_name TEXT NOT NULL,
            source_url TEXT NOT NULL,
            raw_line TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_fund_flow_code_date
            ON stock_fund_flow_daily(code, trade_date);
        CREATE INDEX IF NOT EXISTS idx_sector_name
            ON stock_sector_map(sector_name);
        """
    )
    conn.commit()


def latest_xuangu_batch_id(conn: sqlite3.Connection) -> Optional[str]:
    row = conn.execute(
        "SELECT batch_id FROM xuangu_batches ORDER BY imported_at_utc DESC LIMIT 1"
    ).fetchone()
    return str(row["batch_id"]) if row else None


def load_xuangu_candidates(conn: sqlite3.Connection, batch_id: Optional[str] = None) -> List[CandidateStock]:
    if batch_id is None:
        batch_id = latest_xuangu_batch_id(conn)
    if not batch_id:
        return []
    rows = conn.execute(
        """
        SELECT batch_id, stock_code, stock_name, row_json
        FROM xuangu_results
        WHERE batch_id = ? AND stock_code IS NOT NULL
        """,
        (batch_id,),
    ).fetchall()
    candidates: List[CandidateStock] = []
    seen = set()
    for row in rows:
        code = str(row["stock_code"])
        if code in seen:
            continue
        seen.add(code)
        try:
            row_json = json.loads(row["row_json"])
        except Exception:
            row_json = {}
        candidates.append(CandidateStock(code=code, name=row["stock_name"], batch_id=row["batch_id"], row_json=row_json))
    return candidates


def load_klines(
    conn: sqlite3.Connection,
    code: str,
    limit: int = 260,
    as_of_date: Optional[str] = None,
) -> List[Kline]:
    params: List[Any] = [code]
    sql = """
        SELECT trade_date, open, close, high, low, volume, turnover_rate, change_percent
        FROM eastmoney_stock_daily_klines
        WHERE code = ?
          AND {stock_filter}
    """
    sql = sql.format(stock_filter=stock_kline_filter_sql())
    if as_of_date:
        sql += " AND trade_date <= ?"
        params.append(as_of_date)
    sql += " ORDER BY trade_date DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, tuple(params)).fetchall()
    return [
        Kline(
            trade_date=row["trade_date"],
            open=row["open"],
            close=row["close"],
            high=row["high"],
            low=row["low"],
            volume=row["volume"],
            turnover_rate=row["turnover_rate"],
            change_percent=row["change_percent"],
        )
        for row in reversed(rows)
    ]


def create_screen_run(
    conn: sqlite3.Connection,
    screen_date: str,
    xuangu_batch_id: Optional[str],
    config: Dict[str, Any],
    screening_text: str,
    candidate_count: int,
    selected_count: int,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO weekly_screen_runs (
            screen_date, xuangu_batch_id, strategy_config_json, screening_text,
            candidate_count, selected_count, created_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            screen_date,
            xuangu_batch_id,
            json.dumps(config, ensure_ascii=False, default=str),
            screening_text,
            candidate_count,
            selected_count,
            utc_now(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def delete_screen_runs(conn: sqlite3.Connection, screen_date: str, xuangu_batch_id: Optional[str]) -> int:
    rows = conn.execute(
        """
        SELECT run_id
        FROM weekly_screen_runs
        WHERE screen_date = ?
          AND COALESCE(xuangu_batch_id, '') = COALESCE(?, '')
        """,
        (screen_date, xuangu_batch_id),
    ).fetchall()
    run_ids = [int(row["run_id"]) for row in rows]
    if not run_ids:
        return 0

    placeholders = ",".join("?" for _ in run_ids)
    review_rows = conn.execute(
        f"SELECT review_id FROM weekly_review_runs WHERE reviewed_run_id IN ({placeholders})",
        run_ids,
    ).fetchall()
    review_ids = [int(row["review_id"]) for row in review_rows]
    if review_ids:
        review_placeholders = ",".join("?" for _ in review_ids)
        conn.execute(
            f"DELETE FROM weekly_review_results WHERE review_id IN ({review_placeholders})",
            review_ids,
        )
        conn.execute(
            f"DELETE FROM weekly_review_runs WHERE review_id IN ({review_placeholders})",
            review_ids,
        )

    model_rows = conn.execute(
        f"SELECT model_run_id FROM weekly_ml_model_runs WHERE source_run_id IN ({placeholders})",
        run_ids,
    ).fetchall()
    model_run_ids = [int(row["model_run_id"]) for row in model_rows]
    if model_run_ids:
        model_placeholders = ",".join("?" for _ in model_run_ids)
        conn.execute(
            f"DELETE FROM weekly_ml_training_samples WHERE model_run_id IN ({model_placeholders})",
            model_run_ids,
        )
        conn.execute(
            f"DELETE FROM weekly_ml_predictions WHERE model_run_id IN ({model_placeholders})",
            model_run_ids,
        )
        conn.execute(
            f"DELETE FROM weekly_ml_prediction_history WHERE model_run_id IN ({model_placeholders})",
            model_run_ids,
        )
        conn.execute(
            f"DELETE FROM weekly_ml_model_runs WHERE model_run_id IN ({model_placeholders})",
            model_run_ids,
        )

    conn.execute(f"DELETE FROM weekly_selected_stocks WHERE run_id IN ({placeholders})", run_ids)
    conn.execute(f"DELETE FROM weekly_screen_candidates WHERE run_id IN ({placeholders})", run_ids)
    conn.execute(f"DELETE FROM weekly_screen_runs WHERE run_id IN ({placeholders})", run_ids)
    conn.commit()
    return len(run_ids)


def delete_all_screen_runs(conn: sqlite3.Connection, *, preserve_review_results: bool = True) -> int:
    rows = conn.execute("SELECT run_id FROM weekly_screen_runs").fetchall()
    run_ids = [int(row["run_id"]) for row in rows]
    if not run_ids:
        return 0

    if not preserve_review_results:
        conn.execute("DELETE FROM weekly_review_results")
        conn.execute("DELETE FROM weekly_review_runs")
    conn.execute("DELETE FROM weekly_ml_training_samples")
    conn.execute("DELETE FROM weekly_ml_predictions")
    conn.execute("DELETE FROM weekly_ml_prediction_history")
    conn.execute("DELETE FROM weekly_ml_model_runs")
    conn.execute("DELETE FROM weekly_selected_stocks")
    conn.execute("DELETE FROM weekly_screen_candidates")
    conn.execute("DELETE FROM weekly_screen_runs")
    conn.commit()
    return len(run_ids)


def save_screen_results(conn: sqlite3.Connection, run_id: int, scored: List[ScoredStock], top_n: int) -> None:
    for rank, item in enumerate(scored, start=1):
        score = item.score
        selected = 1 if rank <= top_n else 0
        conn.execute(
            """
            INSERT INTO weekly_screen_candidates (
                run_id, code, name, rank_no, total_score, trend_score,
                volume_turnover_score, breakout_score, fundamentals_score, risk_score,
                selected, selected_reason, row_json, score_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                item.candidate.code,
                item.candidate.name,
                rank,
                score.total,
                score.trend,
                score.volume_turnover,
                score.breakout,
                score.fundamentals,
                score.risk,
                selected,
                item.selected_reason,
                json.dumps(item.candidate.row_json, ensure_ascii=False, default=str),
                json.dumps({"reasons": score.reasons}, ensure_ascii=False),
            ),
        )
        if selected:
            conn.execute(
                """
                INSERT INTO weekly_selected_stocks (
                    run_id, screen_date, code, name, rank_no, total_score,
                    selected_reason, created_at_utc
                )
                SELECT ?, screen_date, ?, ?, ?, ?, ?, ?
                FROM weekly_screen_runs WHERE run_id = ?
                """,
                (
                    run_id,
                    item.candidate.code,
                    item.candidate.name,
                    rank,
                    score.total,
                    item.selected_reason,
                    utc_now(),
                    run_id,
                ),
            )
    conn.commit()


def latest_selected_run_without_review(conn: sqlite3.Connection) -> Optional[int]:
    row = conn.execute(
        """
        SELECT r.run_id
        FROM weekly_screen_runs r
        WHERE EXISTS (SELECT 1 FROM weekly_selected_stocks s WHERE s.run_id = r.run_id)
          AND NOT EXISTS (SELECT 1 FROM weekly_review_runs rr WHERE rr.reviewed_run_id = r.run_id)
        ORDER BY r.screen_date DESC, r.run_id DESC
        LIMIT 1
        """
    ).fetchone()
    return int(row["run_id"]) if row else None


def selected_stocks_for_run(conn: sqlite3.Connection, run_id: int) -> List[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM weekly_selected_stocks
        WHERE run_id = ?
        ORDER BY rank_no
        """,
        (run_id,),
    ).fetchall()


def latest_selected_run(conn: sqlite3.Connection) -> Optional[int]:
    row = conn.execute(
        """
        SELECT r.run_id
        FROM weekly_screen_runs r
        WHERE EXISTS (SELECT 1 FROM weekly_selected_stocks s WHERE s.run_id = r.run_id)
        ORDER BY r.screen_date DESC, r.run_id DESC
        LIMIT 1
        """
    ).fetchone()
    return int(row["run_id"]) if row else None


def selected_codes_for_run(conn: sqlite3.Connection, run_id: int) -> List[str]:
    rows = conn.execute(
        """
        SELECT code FROM weekly_selected_stocks
        WHERE run_id = ?
        ORDER BY rank_no
        """,
        (run_id,),
    ).fetchall()
    return [str(row["code"]) for row in rows]


def screen_runs(conn: sqlite3.Connection, limit: int = 20) -> List[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            r.run_id,
            r.screen_date,
            r.xuangu_batch_id,
            r.candidate_count,
            r.selected_count,
            r.created_at_utc,
            COUNT(DISTINCT p.id) AS ml_prediction_count,
            MAX(m.model_name) AS latest_model_name,
            MAX(m.created_at_utc) AS latest_ml_at
        FROM weekly_screen_runs r
        LEFT JOIN weekly_ml_predictions p
            ON p.source_run_id = r.run_id
        LEFT JOIN weekly_ml_model_runs m
            ON m.source_run_id = r.run_id
        GROUP BY r.run_id
        ORDER BY r.screen_date DESC, r.run_id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def review_trend_runs(conn: sqlite3.Connection, limit: int = 20) -> List[sqlite3.Row]:
    return conn.execute(
        """
        WITH review_agg AS (
            SELECT
                wr.reviewed_run_id AS run_id,
                COUNT(rr.id) AS reviewed_count,
                AVG(CAST(rr.meets_expectation AS REAL)) AS hit_rate,
                AVG(CAST(rr.stop_loss_triggered AS REAL)) AS stop_loss_rate,
                AVG(rr.close_gain_pct) AS avg_close_gain_pct,
                AVG(rr.highest_gain_pct) AS avg_high_gain_pct,
                AVG(rr.max_drawdown_pct) AS avg_max_drawdown_pct
            FROM weekly_review_runs wr
            JOIN weekly_review_results rr
                ON rr.review_id = wr.review_id
            GROUP BY wr.reviewed_run_id
        ),
        ml_agg AS (
            SELECT
                source_run_id AS run_id,
                COUNT(id) AS ml_prediction_count,
                AVG(probability_up) AS avg_probability_up
            FROM weekly_ml_predictions
            GROUP BY source_run_id
        )
        SELECT
            r.run_id,
            r.screen_date,
            r.selected_count,
            ra.reviewed_count,
            ra.hit_rate,
            ra.stop_loss_rate,
            ra.avg_close_gain_pct,
            ra.avg_high_gain_pct,
            ra.avg_max_drawdown_pct,
            COALESCE(ma.ml_prediction_count, 0) AS ml_prediction_count,
            ma.avg_probability_up
        FROM weekly_screen_runs r
        JOIN review_agg ra
            ON ra.run_id = r.run_id
        LEFT JOIN ml_agg ma
            ON ma.run_id = r.run_id
        ORDER BY r.screen_date DESC, r.run_id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def review_feedback_labels(
    conn: sqlite3.Connection,
    recent_runs: int = 0,
    as_of_date: Optional[str] = None,
) -> Dict[Tuple[str, str], int]:
    cutoff_sql = "WHERE review_date < ?" if as_of_date else ""
    cutoff_params: List[Any] = [str(as_of_date)] if as_of_date else []
    if recent_runs and int(recent_runs) > 0:
        rows = conn.execute(
            f"""
            WITH chosen_reviews AS (
                SELECT review_id
                FROM weekly_review_runs
                {cutoff_sql}
                ORDER BY review_id DESC
                LIMIT ?
            ),
            latest_per_run AS (
                SELECT wr.reviewed_run_id, MAX(wr.review_id) AS review_id
                FROM weekly_review_runs wr
                JOIN chosen_reviews cr
                  ON cr.review_id = wr.review_id
                GROUP BY wr.reviewed_run_id
            )
            SELECT rr.code, rr.base_trade_date, rr.meets_expectation
            FROM weekly_review_results rr
            JOIN latest_per_run lr
              ON lr.review_id = rr.review_id
            WHERE rr.base_trade_date IS NOT NULL
              AND rr.base_trade_date <> ''
              AND rr.review_start_date IS NOT NULL
              AND rr.review_end_date IS NOT NULL
              AND rr.highest_gain_pct IS NOT NULL
              AND rr.close_gain_pct IS NOT NULL
              AND rr.max_drawdown_pct IS NOT NULL
            """,
            (*cutoff_params, int(recent_runs)),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            WITH latest_per_run AS (
                SELECT reviewed_run_id, MAX(review_id) AS review_id
                FROM weekly_review_runs
                {cutoff_sql}
                GROUP BY reviewed_run_id
            )
            SELECT rr.code, rr.base_trade_date, rr.meets_expectation
            FROM weekly_review_results rr
            JOIN latest_per_run lr
              ON lr.review_id = rr.review_id
            WHERE rr.base_trade_date IS NOT NULL
              AND rr.base_trade_date <> ''
              AND rr.review_start_date IS NOT NULL
              AND rr.review_end_date IS NOT NULL
              AND rr.highest_gain_pct IS NOT NULL
              AND rr.close_gain_pct IS NOT NULL
              AND rr.max_drawdown_pct IS NOT NULL
            """,
            cutoff_params,
        ).fetchall()
    return {
        (str(row["code"]), str(row["base_trade_date"])): int(row["meets_expectation"])
        for row in rows
        if row["code"] and row["base_trade_date"]
    }


def all_downloaded_codes(conn: sqlite3.Connection) -> List[str]:
    rows = conn.execute(
        f"""
        SELECT DISTINCT code
        FROM eastmoney_stock_daily_klines
        WHERE {stock_kline_filter_sql()}
        ORDER BY code
        """
    ).fetchall()
    return [str(row["code"]) for row in rows]


def upsert_fund_flow_rows(
    conn: sqlite3.Connection,
    rows: Iterable[Dict[str, Any]],
) -> int:
    ensure_market_context_tables(conn)
    values = []
    fetched_at = utc_now()
    for row in rows:
        code = str(row.get("code") or "").strip()
        trade_date = str(row.get("trade_date") or "").strip()
        source_url = str(row.get("source_url") or "").strip()
        if not code or not trade_date or not source_url:
            continue
        values.append(
            (
                code,
                trade_date,
                row.get("main_net_inflow"),
                row.get("main_net_inflow_ratio"),
                source_url,
                json.dumps(row.get("raw_line") or {}, ensure_ascii=False, default=str),
                fetched_at,
            )
        )
    if not values:
        return 0
    conn.executemany(
        """
        INSERT INTO stock_fund_flow_daily (
            code, trade_date, main_net_inflow, main_net_inflow_ratio,
            source_url, raw_line, fetched_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(code, trade_date) DO UPDATE SET
            main_net_inflow=COALESCE(excluded.main_net_inflow, stock_fund_flow_daily.main_net_inflow),
            main_net_inflow_ratio=COALESCE(excluded.main_net_inflow_ratio, stock_fund_flow_daily.main_net_inflow_ratio),
            source_url=excluded.source_url,
            raw_line=excluded.raw_line,
            fetched_at_utc=excluded.fetched_at_utc
        """,
        values,
    )
    return len(values)


def upsert_sector_rows(
    conn: sqlite3.Connection,
    rows: Iterable[Dict[str, Any]],
) -> int:
    ensure_market_context_tables(conn)
    values = []
    updated_at = utc_now()
    for row in rows:
        code = str(row.get("code") or "").strip()
        sector_name = str(row.get("sector_name") or "").strip()
        source_url = str(row.get("source_url") or "").strip()
        if not code or not sector_name or not source_url:
            continue
        values.append(
            (
                code,
                sector_name,
                source_url,
                json.dumps(row.get("raw_line") or {}, ensure_ascii=False, default=str),
                updated_at,
            )
        )
    if not values:
        return 0
    conn.executemany(
        """
        INSERT INTO stock_sector_map (
            code, sector_name, source_url, raw_line, updated_at_utc
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(code) DO UPDATE SET
            sector_name=excluded.sector_name,
            source_url=excluded.source_url,
            raw_line=excluded.raw_line,
            updated_at_utc=excluded.updated_at_utc
        """,
        values,
    )
    return len(values)


def _rolling_mean(values: List[float], end_idx: int, window: int) -> float:
    if window <= 0 or end_idx < 0:
        return 0.0
    start = max(0, end_idx - window + 1)
    segment = values[start : end_idx + 1]
    if not segment:
        return 0.0
    return sum(segment) / len(segment)


def _safe_pct(a: Any, b: Any) -> float | None:
    if a is None or b in (None, 0):
        return None
    try:
        return (float(a) / float(b) - 1.0) * 100.0
    except Exception:
        return None


def load_ml_context_features(
    conn: sqlite3.Connection,
    code_dates: Iterable[Tuple[str, str]],
) -> Dict[Tuple[str, str], Dict[str, float]]:
    """Load contextual ML features (fund flow + sector momentum) by (code, trade_date)."""
    items = [(str(code), str(trade_date)) for code, trade_date in code_dates if code and trade_date]
    if not items:
        return {}
    ensure_market_context_tables(conn)

    codes = sorted({code for code, _ in items})
    min_date = min(date for _, date in items)
    max_date = max(date for _, date in items)

    # Pull a wider window so 20-day rolling stats are available near min_date.
    try:
        start_dt = datetime.strptime(min_date, "%Y-%m-%d").date() - timedelta(days=45)
        query_start_date = start_dt.strftime("%Y-%m-%d")
    except Exception:
        query_start_date = min_date

    placeholder_codes = ",".join("?" for _ in codes)
    fund_rows = conn.execute(
        f"""
        SELECT code, trade_date, main_net_inflow_ratio
        FROM stock_fund_flow_daily
        WHERE code IN ({placeholder_codes})
          AND trade_date BETWEEN ? AND ?
        ORDER BY code, trade_date
        """,
        [*codes, query_start_date, max_date],
    ).fetchall()

    fund_ratio_by_code_date: Dict[Tuple[str, str], float] = {}
    fund_series_by_code: Dict[str, List[Tuple[str, float]]] = defaultdict(list)
    for row in fund_rows:
        code = str(row["code"])
        trade_date = str(row["trade_date"])
        ratio = float(row["main_net_inflow_ratio"] or 0.0)
        fund_ratio_by_code_date[(code, trade_date)] = ratio
        fund_series_by_code[code].append((trade_date, ratio))

    fund_roll_by_code_date: Dict[Tuple[str, str], Tuple[float, float]] = {}
    for code, series in fund_series_by_code.items():
        ratios = [value for _date, value in series]
        for idx, (trade_date, _value) in enumerate(series):
            fund_5 = _rolling_mean(ratios, idx, 5)
            fund_20 = _rolling_mean(ratios, idx, 20)
            fund_roll_by_code_date[(code, trade_date)] = (fund_5, fund_20)

    sector_rows = conn.execute(
        f"""
        SELECT code, sector_name
        FROM stock_sector_map
        WHERE code IN ({placeholder_codes})
        """,
        codes,
    ).fetchall()
    sector_by_code = {str(row["code"]): str(row["sector_name"] or "") for row in sector_rows}
    sectors = sorted({sector for sector in sector_by_code.values() if sector})

    # Market breadth and relative-strength features use every downloaded stock
    # in the local K-line table. When the user expands this table with BaoStock
    # all-A-share history, these features automatically become closer to true
    # market-wide breadth.
    market_rows = conn.execute(
        f"""
        SELECT code, trade_date, close, high, change_percent
        FROM eastmoney_stock_daily_klines
        WHERE trade_date BETWEEN ? AND ?
          AND close IS NOT NULL
          AND {stock_kline_filter_sql()}
        ORDER BY code, trade_date
        """,
        (query_start_date, max_date),
    ).fetchall()
    market_series_by_code: Dict[str, List[sqlite3.Row]] = defaultdict(list)
    for row in market_rows:
        market_series_by_code[str(row["code"])].append(row)

    stock_ret_by_code_date: Dict[Tuple[str, str], Tuple[float, float]] = {}
    market_daily: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "adv_flags": [],
        "above_ma20": [],
        "new_high_20": [],
        "ret5": [],
        "ret20": [],
    })
    sector_member_daily: Dict[Tuple[str, str], Dict[str, List[float]]] = defaultdict(lambda: {"ret5": [], "ret20": []})

    for code, series in market_series_by_code.items():
        closes = [float(row["close"]) for row in series]
        highs = [float(row["high"]) if row["high"] is not None else float(row["close"]) for row in series]
        sector = sector_by_code.get(code, "")
        for idx, row in enumerate(series):
            trade_date = str(row["trade_date"])
            ret5 = _safe_pct(closes[idx], closes[idx - 5]) if idx >= 5 else None
            ret20 = _safe_pct(closes[idx], closes[idx - 20]) if idx >= 20 else None
            if ret5 is not None or ret20 is not None:
                stock_ret_by_code_date[(code, trade_date)] = (ret5 or 0.0, ret20 or 0.0)

            day = market_daily[trade_date]
            change_percent = row["change_percent"]
            if change_percent is not None:
                day["adv_flags"].append(1.0 if float(change_percent) > 0 else 0.0)
            if idx >= 19:
                ma20 = sum(closes[idx - 19 : idx + 1]) / 20
                high20 = max(highs[idx - 19 : idx + 1])
                day["above_ma20"].append(1.0 if closes[idx] > ma20 else 0.0)
                day["new_high_20"].append(1.0 if highs[idx] >= high20 else 0.0)
            if ret5 is not None:
                day["ret5"].append(ret5)
                if sector:
                    sector_member_daily[(sector, trade_date)]["ret5"].append(ret5)
            if ret20 is not None:
                day["ret20"].append(ret20)
                if sector:
                    sector_member_daily[(sector, trade_date)]["ret20"].append(ret20)

    market_context_by_date: Dict[str, Dict[str, float]] = {}
    sorted_market_dates = sorted(market_daily)
    adv_ratios = [
        (sum(market_daily[trade_date]["adv_flags"]) / len(market_daily[trade_date]["adv_flags"]))
        if market_daily[trade_date]["adv_flags"]
        else 0.0
        for trade_date in sorted_market_dates
    ]
    for idx, trade_date in enumerate(sorted_market_dates):
        day = market_daily[trade_date]
        market_context_by_date[trade_date] = {
            "market_breadth_adv_5": _rolling_mean(adv_ratios, idx, 5),
            "market_breadth_above_ma20": (
                sum(day["above_ma20"]) / len(day["above_ma20"]) if day["above_ma20"] else 0.0
            ),
            "market_breadth_new_high_20": (
                sum(day["new_high_20"]) / len(day["new_high_20"]) if day["new_high_20"] else 0.0
            ),
            "market_universe_size": float(len(day["adv_flags"])),
            "market_member_ret_5": sum(day["ret5"]) / len(day["ret5"]) if day["ret5"] else 0.0,
            "market_member_ret_20": sum(day["ret20"]) / len(day["ret20"]) if day["ret20"] else 0.0,
        }

    sector_member_context: Dict[Tuple[str, str], Tuple[float, float]] = {}
    for key, values in sector_member_daily.items():
        ret5s = values["ret5"]
        ret20s = values["ret20"]
        sector_member_context[key] = (
            sum(ret5s) / len(ret5s) if ret5s else 0.0,
            sum(ret20s) / len(ret20s) if ret20s else 0.0,
        )

    if not sectors:
        # No sector map yet; return fund-flow-only features.
        out: Dict[Tuple[str, str], Dict[str, float]] = {}
        for code, trade_date in items:
            fund_ratio = fund_ratio_by_code_date.get((code, trade_date), 0.0)
            fund_5, fund_20 = fund_roll_by_code_date.get((code, trade_date), (0.0, 0.0))
            stock_ret5, stock_ret20 = stock_ret_by_code_date.get((code, trade_date), (0.0, 0.0))
            market_ctx = market_context_by_date.get(trade_date, {})
            market_ret5 = market_ctx.get("market_member_ret_5", 0.0)
            market_ret20 = market_ctx.get("market_member_ret_20", 0.0)
            out[(code, trade_date)] = {
                **market_ctx,
                "excess_ret_5_vs_market": stock_ret5 - market_ret5,
                "excess_ret_20_vs_market": stock_ret20 - market_ret20,
                "fund_main_net_ratio": fund_ratio,
                "fund_main_net_ratio_5": fund_5,
                "fund_main_net_ratio_20": fund_20,
                "fund_main_net_trend": fund_5 - fund_20,
                "fund_flow_available": 1.0 if (code, trade_date) in fund_ratio_by_code_date else 0.0,
                "sector_ret_5": 0.0,
                "sector_ret_20": 0.0,
                "sector_momentum_5_20": 0.0,
                "sector_member_ret_5": 0.0,
                "sector_member_ret_20": 0.0,
                "excess_ret_5_vs_sector": 0.0,
                "excess_ret_20_vs_sector": 0.0,
                "sector_available": 0.0,
            }
        return out

    placeholder_sectors = ",".join("?" for _ in sectors)
    sector_day_rows = conn.execute(
        f"""
        SELECT k.trade_date, m.sector_name, AVG(COALESCE(k.change_percent, 0.0)) AS sector_ret_1d
        FROM eastmoney_stock_daily_klines k
        JOIN stock_sector_map m
          ON m.code = k.code
        WHERE m.sector_name IN ({placeholder_sectors})
          AND k.trade_date BETWEEN ? AND ?
          AND {stock_kline_filter_sql("k")}
        GROUP BY k.trade_date, m.sector_name
        ORDER BY m.sector_name, k.trade_date
        """,
        [*sectors, query_start_date, max_date],
    ).fetchall()

    sector_series: Dict[str, List[Tuple[str, float]]] = defaultdict(list)
    for row in sector_day_rows:
        sector_series[str(row["sector_name"])].append((str(row["trade_date"]), float(row["sector_ret_1d"] or 0.0)))

    sector_roll: Dict[Tuple[str, str], Tuple[float, float]] = {}
    for sector, series in sector_series.items():
        rets = [value for _date, value in series]
        for idx, (trade_date, _value) in enumerate(series):
            ret_5 = _rolling_mean(rets, idx, 5)
            ret_20 = _rolling_mean(rets, idx, 20)
            sector_roll[(sector, trade_date)] = (ret_5, ret_20)

    out: Dict[Tuple[str, str], Dict[str, float]] = {}
    for code, trade_date in items:
        fund_ratio = fund_ratio_by_code_date.get((code, trade_date), 0.0)
        fund_5, fund_20 = fund_roll_by_code_date.get((code, trade_date), (0.0, 0.0))
        sector = sector_by_code.get(code, "")
        sec_5, sec_20 = sector_roll.get((sector, trade_date), (0.0, 0.0))
        stock_ret5, stock_ret20 = stock_ret_by_code_date.get((code, trade_date), (0.0, 0.0))
        market_ctx = market_context_by_date.get(trade_date, {})
        market_ret5 = market_ctx.get("market_member_ret_5", 0.0)
        market_ret20 = market_ctx.get("market_member_ret_20", 0.0)
        sector_member_ret5, sector_member_ret20 = sector_member_context.get((sector, trade_date), (0.0, 0.0))
        out[(code, trade_date)] = {
            **market_ctx,
            "excess_ret_5_vs_market": stock_ret5 - market_ret5,
            "excess_ret_20_vs_market": stock_ret20 - market_ret20,
            "fund_main_net_ratio": fund_ratio,
            "fund_main_net_ratio_5": fund_5,
            "fund_main_net_ratio_20": fund_20,
            "fund_main_net_trend": fund_5 - fund_20,
            "fund_flow_available": 1.0 if (code, trade_date) in fund_ratio_by_code_date else 0.0,
            "sector_ret_5": sec_5,
            "sector_ret_20": sec_20,
            "sector_momentum_5_20": sec_5 - sec_20,
            "sector_member_ret_5": sector_member_ret5,
            "sector_member_ret_20": sector_member_ret20,
            "excess_ret_5_vs_sector": stock_ret5 - sector_member_ret5,
            "excess_ret_20_vs_sector": stock_ret20 - sector_member_ret20,
            "sector_available": 1.0 if (sector, trade_date) in sector_roll else 0.0,
        }
    return out


def create_ml_model_run(
    conn: sqlite3.Connection,
    source_run_id: int,
    model_name: str,
    config: Dict[str, Any],
    train_sample_count: int,
    positive_sample_count: int,
    model_json: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO weekly_ml_model_runs (
            source_run_id, model_name, config_json, train_sample_count,
            positive_sample_count, model_json, created_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_run_id,
            model_name,
            json.dumps(config, ensure_ascii=False, default=str),
            train_sample_count,
            positive_sample_count,
            model_json,
            utc_now(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def save_ml_training_samples(
    conn: sqlite3.Connection,
    model_run_id: int,
    source_run_id: int,
    samples: Iterable[Any],
) -> None:
    conn.execute("DELETE FROM weekly_ml_training_samples WHERE model_run_id = ?", (model_run_id,))
    for sample in samples:
        conn.execute(
            """
            INSERT INTO weekly_ml_training_samples (
                model_run_id, source_run_id, code, trade_date, label,
                future_high_gain_pct, future_close_gain_pct, future_max_drawdown_pct,
                feature_json, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                model_run_id,
                source_run_id,
                sample.code,
                sample.trade_date,
                int(sample.label),
                sample.future_high_gain_pct,
                sample.future_close_gain_pct,
                sample.future_max_drawdown_pct,
                json.dumps(sample.features, ensure_ascii=False, default=str),
                utc_now(),
            ),
        )
    conn.commit()


def save_ml_predictions(
    conn: sqlite3.Connection,
    model_run_id: int,
    source_run_id: int,
    predictions: Iterable[Dict[str, Any]],
) -> None:
    conn.execute("DELETE FROM weekly_ml_predictions WHERE source_run_id = ?", (source_run_id,))
    for pred in predictions:
        created_at = utc_now()
        payload = json.dumps(pred.get("features") or {}, ensure_ascii=False, default=str)
        conn.execute(
            """
            INSERT INTO weekly_ml_predictions (
                model_run_id, source_run_id, code, name, probability_up,
                predicted_score, feature_json, reason, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                model_run_id,
                source_run_id,
                pred["code"],
                pred.get("name"),
                pred["probability_up"],
                pred["predicted_score"],
                payload,
                pred.get("reason") or "",
                created_at,
            ),
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO weekly_ml_prediction_history (
                model_run_id, source_run_id, code, name, probability_up,
                predicted_score, feature_json, reason, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                model_run_id,
                source_run_id,
                pred["code"],
                pred.get("name"),
                pred["probability_up"],
                pred["predicted_score"],
                payload,
                pred.get("reason") or "",
                created_at,
            ),
        )
    conn.commit()


def ml_predictions_for_run(conn: sqlite3.Connection, source_run_id: int) -> List[sqlite3.Row]:
    return conn.execute(
        """
        SELECT code, name, probability_up, predicted_score, feature_json, reason, created_at_utc
        FROM weekly_ml_predictions
        WHERE source_run_id = ?
        ORDER BY predicted_score DESC, probability_up DESC
        """,
        (source_run_id,),
    ).fetchall()




def delete_review_for_run(conn: sqlite3.Connection, reviewed_run_id: int) -> int:
    rows = conn.execute(
        "SELECT review_id FROM weekly_review_runs WHERE reviewed_run_id = ?",
        (reviewed_run_id,),
    ).fetchall()
    review_ids = [int(row[0]) for row in rows]
    if not review_ids:
        return 0

    placeholders = ",".join("?" * len(review_ids))
    conn.execute(
        f"DELETE FROM weekly_review_results WHERE review_id IN ({placeholders})",
        review_ids,
    )
    conn.execute(
        f"DELETE FROM weekly_review_runs WHERE review_id IN ({placeholders})",
        review_ids,
    )
    conn.commit()
    return len(review_ids)

def create_review_run(conn: sqlite3.Connection, reviewed_run_id: int, review_date: str, config: Dict[str, Any]) -> int:
    cur = conn.execute(
        """
        INSERT INTO weekly_review_runs (reviewed_run_id, review_date, config_json, created_at_utc)
        VALUES (?, ?, ?, ?)
        """,
        (reviewed_run_id, review_date, json.dumps(config, ensure_ascii=False, default=str), utc_now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def save_review_results(conn: sqlite3.Connection, review_id: int, selected_rows: Iterable[sqlite3.Row], results: List[ReviewResult]) -> None:
    by_code = {r.code: r for r in results}
    for selected in selected_rows:
        result = by_code[selected["code"]]
        conn.execute(
            """
            INSERT INTO weekly_review_results (
                review_id, selected_id, code, name, base_trade_date, review_start_date,
                review_end_date, highest_gain_pct, close_gain_pct, max_drawdown_pct,
                stop_loss_triggered, meets_expectation, best_exit_meets_expectation, notes, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                review_id,
                selected["id"],
                result.code,
                result.name,
                result.base_trade_date,
                result.review_start_date,
                result.review_end_date,
                result.highest_gain_pct,
                result.close_gain_pct,
                result.max_drawdown_pct,
                int(result.stop_loss_triggered),
                int(result.meets_expectation),
                int(result.best_exit_meets_expectation),
                result.notes,
                utc_now(),
            ),
        )
    conn.commit()
