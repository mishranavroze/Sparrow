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
            rss_summary TEXT NOT NULL DEFAULT '',
            segment_counts TEXT NOT NULL DEFAULT '{}',
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

        CREATE TABLE IF NOT EXISTS episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT UNIQUE NOT NULL,
            file_size_bytes INTEGER NOT NULL,
            duration_seconds INTEGER NOT NULL,
            duration_formatted TEXT NOT NULL,
            topics_summary TEXT NOT NULL DEFAULT '',
            rss_summary TEXT NOT NULL DEFAULT '',
            gcs_url TEXT NOT NULL DEFAULT '',
            published_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_digests_date ON digests(date);
        CREATE INDEX IF NOT EXISTS idx_episodes_date ON episodes(date);
        CREATE INDEX IF NOT EXISTS idx_runs_started ON pipeline_runs(started_at);
    """)
    # Migrate: add rss_summary column if missing (existing DBs)
    try:
        conn.execute("ALTER TABLE digests ADD COLUMN rss_summary TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # Column already exists
    # Migrate: add segment_counts column to digests if missing
    try:
        conn.execute("ALTER TABLE digests ADD COLUMN segment_counts TEXT NOT NULL DEFAULT '{}'")
    except sqlite3.OperationalError:
        pass  # Column already exists
    # Migrate: add gcs_url column to episodes if missing
    try:
        conn.execute("ALTER TABLE episodes ADD COLUMN gcs_url TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # Column already exists
    conn.commit()


# --- Digest CRUD ---

def save_digest(date: str, markdown_text: str, article_count: int,
                total_words: int, topics_summary: str, rss_summary: str = "",
                segment_counts: dict[str, int] | None = None) -> None:
    """Save or update a daily digest."""
    conn = _get_connection()
    try:
        conn.execute(
            """INSERT INTO digests (date, markdown_text, article_count, total_words,
               topics_summary, rss_summary, segment_counts, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET
               markdown_text=excluded.markdown_text,
               article_count=excluded.article_count,
               total_words=excluded.total_words,
               topics_summary=excluded.topics_summary,
               rss_summary=excluded.rss_summary,
               segment_counts=excluded.segment_counts,
               created_at=excluded.created_at""",
            (date, markdown_text, article_count, total_words, topics_summary,
             rss_summary, json.dumps(segment_counts or {}),
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


def get_topic_coverage(limit: int = 30) -> list[dict]:
    """Get segment_counts from recent digests for topic coverage analysis."""
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT date, segment_counts FROM digests ORDER BY date DESC LIMIT ?",
            (limit,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["segment_counts"] = json.loads(d["segment_counts"])
            result.append(d)
        return result
    finally:
        conn.close()


# --- Episode Archive ---

def save_episode(date: str, file_size_bytes: int, duration_seconds: int,
                 duration_formatted: str, topics_summary: str,
                 rss_summary: str = "", gcs_url: str = "") -> None:
    """Permanently archive an episode. This record is never deleted."""
    conn = _get_connection()
    try:
        conn.execute(
            """INSERT INTO episodes (date, file_size_bytes, duration_seconds,
               duration_formatted, topics_summary, rss_summary, gcs_url, published_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET
               file_size_bytes=excluded.file_size_bytes,
               duration_seconds=excluded.duration_seconds,
               duration_formatted=excluded.duration_formatted,
               topics_summary=excluded.topics_summary,
               rss_summary=excluded.rss_summary,
               gcs_url=excluded.gcs_url,
               published_at=excluded.published_at""",
            (date, file_size_bytes, duration_seconds, duration_formatted,
             topics_summary, rss_summary, gcs_url, datetime.now(UTC).isoformat()),
        )
        conn.commit()
        logger.info("Archived episode for %s", date)
    finally:
        conn.close()


def list_episodes(limit: int = 0) -> list[dict]:
    """List all archived episodes (most recent first). No limit by default."""
    conn = _get_connection()
    try:
        query = "SELECT * FROM episodes ORDER BY date DESC"
        if limit > 0:
            query += f" LIMIT {limit}"
        rows = conn.execute(query).fetchall()
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
