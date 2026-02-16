"""Tests for database module."""

from unittest.mock import patch

from src import database


def test_save_and_get_digest(tmp_path):
    db_path = tmp_path / "test.db"
    with patch.object(database, "DB_PATH", db_path):
        database.save_digest("2026-02-16", "# Digest\n\nContent", 5, 1200, "Topic A; Topic B")

        result = database.get_digest("2026-02-16")
        assert result is not None
        assert result["date"] == "2026-02-16"
        assert result["markdown_text"] == "# Digest\n\nContent"
        assert result["article_count"] == 5
        assert result["total_words"] == 1200
        assert result["topics_summary"] == "Topic A; Topic B"


def test_save_digest_upserts(tmp_path):
    db_path = tmp_path / "test.db"
    with patch.object(database, "DB_PATH", db_path):
        database.save_digest("2026-02-16", "Old content", 3, 500, "Old topics")
        database.save_digest("2026-02-16", "New content", 7, 2000, "New topics")

        result = database.get_digest("2026-02-16")
        assert result["markdown_text"] == "New content"
        assert result["article_count"] == 7


def test_list_digests(tmp_path):
    db_path = tmp_path / "test.db"
    with patch.object(database, "DB_PATH", db_path):
        database.save_digest("2026-02-14", "Day 1", 2, 400, "A")
        database.save_digest("2026-02-15", "Day 2", 3, 600, "B")
        database.save_digest("2026-02-16", "Day 3", 5, 1000, "C")

        digests = database.list_digests()
        assert len(digests) == 3
        # Most recent first
        assert digests[0]["date"] == "2026-02-16"
        assert digests[2]["date"] == "2026-02-14"
        # List view should not include full markdown
        assert "markdown_text" not in digests[0]


def test_get_digest_not_found(tmp_path):
    db_path = tmp_path / "test.db"
    with patch.object(database, "DB_PATH", db_path):
        assert database.get_digest("2099-01-01") is None


def test_pipeline_run_logging(tmp_path):
    db_path = tmp_path / "test.db"
    with patch.object(database, "DB_PATH", db_path):
        database.start_run("run-123")
        database.log_step("run-123", "1. Fetch emails", "success", "Fetched 5 emails")
        database.log_step("run-123", "2. Parse content", "success", "Parsed 3 articles")
        database.log_step("run-123", "3. Compile digest", "failed", "Error: something broke")
        database.finish_run("run-123", "failed", "Error: something broke")

        run = database.get_run("run-123")
        assert run is not None
        assert run["status"] == "failed"
        assert run["error_message"] == "Error: something broke"
        assert len(run["steps_log"]) == 3
        assert run["steps_log"][0]["step"] == "1. Fetch emails"
        assert run["steps_log"][0]["status"] == "success"
        assert run["steps_log"][2]["status"] == "failed"


def test_list_runs(tmp_path):
    db_path = tmp_path / "test.db"
    with patch.object(database, "DB_PATH", db_path):
        database.start_run("run-a")
        database.finish_run("run-a", "success")
        database.start_run("run-b")
        database.finish_run("run-b", "failed", "Some error")

        runs = database.list_runs()
        assert len(runs) == 2
        # Most recent first
        assert runs[0]["run_id"] == "run-b"
