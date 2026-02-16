"""SQLite database for storing daily digests and pipeline run logs."""

import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path("output/noctua.db")


def _get_connection() -> sqlite3.Connection:
    """Get a database connection, creating the DB and tables if needed."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _create_tables(conn)
    return conn


def _create_tables(conn: sqlite3.Connection) -> None:
    """Create tables if they don't exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS digests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT UNIQUE NOT NULL,
            markdown_text TEXT NOT NULL,
            article_count INTEGER NOT NULL DEFAULT 0,
            total_words INTEGER NOT NULL DEFAULT 0,
            topics_summary TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pipeline_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            current_step TEXT,
            error_message TEXT,
            steps_log TEXT NOT NULL DEFAULT '[]'
        );

        CREATE INDEX IF NOT EXISTS idx_digests_date ON digests(date);
        CREATE INDEX IF NOT EXISTS idx_runs_started ON pipeline_runs(started_at);
    """)
    conn.commit()


# --- Digest CRUD ---

def save_digest(date: str, markdown_text: str, article_count: int,
                total_words: int, topics_summary: str) -> None:
    """Save or update a daily digest."""
    conn = _get_connection()
    try:
        conn.execute(
            """INSERT INTO digests (date, markdown_text, article_count, total_words,
               topics_summary, created_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET
               markdown_text=excluded.markdown_text,
               article_count=excluded.article_count,
               total_words=excluded.total_words,
               topics_summary=excluded.topics_summary,
               created_at=excluded.created_at""",
            (date, markdown_text, article_count, total_words, topics_summary,
             datetime.now(UTC).isoformat()),
        )
        conn.commit()
        logger.info("Saved digest for %s to database", date)
    finally:
        conn.close()


def get_digest(date: str) -> dict | None:
    """Get a single digest by date."""
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM digests WHERE date = ?", (date,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_digests(limit: int = 50) -> list[dict]:
    """List recent digests (most recent first)."""
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT id, date, article_count, total_words, topics_summary, created_at "
            "FROM digests ORDER BY date DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --- Pipeline Run Logging ---

def start_run(run_id: str) -> None:
    """Record the start of a pipeline run."""
    conn = _get_connection()
    try:
        conn.execute(
            "INSERT INTO pipeline_runs (run_id, started_at, status, steps_log) "
            "VALUES (?, ?, 'running', '[]')",
            (run_id, datetime.now(UTC).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def log_step(run_id: str, step: str, status: str, message: str = "") -> None:
    """Log a pipeline step to the current run."""
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT steps_log FROM pipeline_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if not row:
            return

        steps = json.loads(row["steps_log"])
        steps.append({
            "step": step,
            "status": status,
            "message": message,
            "timestamp": datetime.now(UTC).isoformat(),
        })

        conn.execute(
            "UPDATE pipeline_runs SET steps_log = ?, current_step = ? WHERE run_id = ?",
            (json.dumps(steps), step, run_id),
        )
        conn.commit()
    finally:
        conn.close()


def finish_run(run_id: str, status: str, error_message: str = "") -> None:
    """Mark a pipeline run as finished."""
    conn = _get_connection()
    try:
        conn.execute(
            "UPDATE pipeline_runs SET status = ?, finished_at = ?, error_message = ? "
            "WHERE run_id = ?",
            (status, datetime.now(UTC).isoformat(), error_message, run_id),
        )
        conn.commit()
    finally:
        conn.close()


def list_runs(limit: int = 20) -> list[dict]:
    """List recent pipeline runs (most recent first)."""
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM pipeline_runs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["steps_log"] = json.loads(d["steps_log"])
            result.append(d)
        return result
    finally:
        conn.close()


def get_run(run_id: str) -> dict | None:
    """Get a single pipeline run by run_id."""
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM pipeline_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["steps_log"] = json.loads(d["steps_log"])
        return d
    finally:
        conn.close()
