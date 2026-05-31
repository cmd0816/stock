from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .models import CandidateStock, Kline, ReviewResult, ScoredStock


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

        CREATE INDEX IF NOT EXISTS idx_weekly_candidates_run ON weekly_screen_candidates(run_id);
        CREATE INDEX IF NOT EXISTS idx_weekly_selected_run ON weekly_selected_stocks(run_id);
        CREATE INDEX IF NOT EXISTS idx_weekly_review_run ON weekly_review_results(review_id);
        CREATE INDEX IF NOT EXISTS idx_weekly_ml_samples_model ON weekly_ml_training_samples(model_run_id);
        CREATE INDEX IF NOT EXISTS idx_weekly_ml_samples_code_date ON weekly_ml_training_samples(code, trade_date);
        CREATE INDEX IF NOT EXISTS idx_weekly_ml_predictions_run ON weekly_ml_predictions(source_run_id);
        """
    )
    _migrate_weekly_review_runs_to_append_mode(conn)
    _add_column_if_missing(conn, "weekly_review_results", "best_exit_meets_expectation", "INTEGER NOT NULL DEFAULT 0")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_weekly_review_runs_run_id ON weekly_review_runs(reviewed_run_id)")
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
    """
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


def review_feedback_labels(conn: sqlite3.Connection, recent_runs: int = 0) -> Dict[Tuple[str, str], int]:
    if recent_runs and int(recent_runs) > 0:
        rows = conn.execute(
            """
            WITH chosen_reviews AS (
                SELECT review_id
                FROM weekly_review_runs
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
            """,
            (int(recent_runs),),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            WITH latest_per_run AS (
                SELECT reviewed_run_id, MAX(review_id) AS review_id
                FROM weekly_review_runs
                GROUP BY reviewed_run_id
            )
            SELECT rr.code, rr.base_trade_date, rr.meets_expectation
            FROM weekly_review_results rr
            JOIN latest_per_run lr
              ON lr.review_id = rr.review_id
            WHERE rr.base_trade_date IS NOT NULL
              AND rr.base_trade_date <> ''
            """
        ).fetchall()
    return {
        (str(row["code"]), str(row["base_trade_date"])): int(row["meets_expectation"])
        for row in rows
        if row["code"] and row["base_trade_date"]
    }


def all_downloaded_codes(conn: sqlite3.Connection) -> List[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT code
        FROM eastmoney_stock_daily_klines
        ORDER BY code
        """
    ).fetchall()
    return [str(row["code"]) for row in rows]


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
                json.dumps(pred.get("features") or {}, ensure_ascii=False, default=str),
                pred.get("reason") or "",
                utc_now(),
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
