"""Tests for weekly episode lifecycle: helpers, cleanup, and new endpoints."""

import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src import database


# --- PST helper tests ---

def test_pst_now():
    from main import _pst_now, PST
    now = _pst_now()
    assert now.tzinfo == PST


def test_iso_week_label():
    from main import _iso_week_label
    # 2026-02-18 is a Wednesday in ISO week 8
    dt = datetime(2026, 2, 18, tzinfo=timezone.utc)
    assert _iso_week_label(dt) == "W08-2026"


def test_iso_week_label_week1():
    from main import _iso_week_label
    dt = datetime(2026, 1, 5, tzinfo=timezone.utc)
    assert _iso_week_label(dt) == "W02-2026"


def test_week_date_range():
    from main import _week_date_range
    # 2026-02-18 (Wed) -> Mon=2026-02-16, Sun=2026-02-22
    dt = datetime(2026, 2, 18, tzinfo=timezone.utc)
    mon, sun = _week_date_range(dt)
    assert mon == "2026-02-16"
    assert sun == "2026-02-22"


def test_week_date_range_monday():
    from main import _week_date_range
    dt = datetime(2026, 2, 16, tzinfo=timezone.utc)
    mon, sun = _week_date_range(dt)
    assert mon == "2026-02-16"
    assert sun == "2026-02-22"


def test_week_date_range_sunday():
    from main import _week_date_range
    dt = datetime(2026, 2, 22, tzinfo=timezone.utc)
    mon, sun = _week_date_range(dt)
    assert mon == "2026-02-16"
    assert sun == "2026-02-22"


# --- database.delete_digests_between ---

def test_delete_digests_between(tmp_path):
    db_path = tmp_path / "test.db"
    with patch.object(database, "DB_PATH", db_path):
        database.save_digest("2026-02-16", "Mon", 1, 100, "A")
        database.save_digest("2026-02-17", "Tue", 2, 200, "B")
        database.save_digest("2026-02-18", "Wed", 3, 300, "C")
        database.save_digest("2026-02-23", "Next Mon", 4, 400, "D")

        deleted = database.delete_digests_between("2026-02-16", "2026-02-22")
        assert deleted == 3

        # The one outside the range should survive
        assert database.get_digest("2026-02-23") is not None
        assert database.get_digest("2026-02-16") is None
        assert database.get_digest("2026-02-17") is None
        assert database.get_digest("2026-02-18") is None


def test_delete_digests_between_empty(tmp_path):
    db_path = tmp_path / "test.db"
    with patch.object(database, "DB_PATH", db_path):
        deleted = database.delete_digests_between("2026-01-01", "2026-01-07")
        assert deleted == 0


# --- feed_builder.clear_feed ---

def test_clear_feed(tmp_path):
    from src.feed_builder import _save_episode_catalog, clear_feed

    json_path = tmp_path / "episodes.json"
    feed_path = tmp_path / "feed.xml"

    with (
        patch("src.feed_builder.EPISODES_JSON", json_path),
        patch("src.feed_builder.FEED_PATH", feed_path),
    ):
        # Seed with an episode
        _save_episode_catalog([{
            "date": "2026-02-18",
            "file_size_bytes": 5000000,
            "duration_seconds": 1200,
            "duration_formatted": "00:20:00",
            "topics_summary": "Test",
            "published": "2026-02-18T00:00:00+00:00",
        }])

        clear_feed()

        catalog = json.loads(json_path.read_text())
        assert catalog == []
        assert feed_path.exists()


# --- Monday cleanup ---

def test_monday_cleanup(tmp_path):
    """Full Monday cleanup: archive, delete MP3s, clear feed, delete digests."""
    from main import _monday_cleanup

    episodes_dir = tmp_path / "episodes"
    episodes_dir.mkdir()
    exports_dir = tmp_path / "exports"

    # Create last week's MP3s (week of 2026-02-09 to 2026-02-15)
    for day in range(9, 14):
        (episodes_dir / f"noctua-2026-02-{day:02d}.mp3").write_bytes(b"fake mp3 data")

    # Also a current week MP3 that should still be deleted (cleanup deletes ALL mp3s)
    (episodes_dir / "noctua-2026-02-16.mp3").write_bytes(b"current week")

    db_path = tmp_path / "test.db"

    # Patch _pst_now to return Monday 2026-02-16
    mock_now = datetime(2026, 2, 16, 10, 0, 0, tzinfo=timezone(timedelta(hours=-8)))

    with (
        patch("main.EPISODES_DIR", episodes_dir),
        patch("main.EXPORTS_DIR", exports_dir),
        patch("main._pst_now", return_value=mock_now),
        patch("src.feed_builder.EPISODES_JSON", tmp_path / "episodes.json"),
        patch("src.feed_builder.FEED_PATH", tmp_path / "feed.xml"),
        patch.object(database, "DB_PATH", db_path),
    ):
        # Save digests for last week
        database.save_digest("2026-02-09", "Day", 1, 100, "A")
        database.save_digest("2026-02-10", "Day", 2, 200, "B")

        _monday_cleanup()

        # ZIP should exist for last week
        zip_path = exports_dir / "hootline-W07-2026.zip"
        assert zip_path.exists()
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            # 5 MP3s + 2 digest .md files
            assert len(names) == 7
            assert "noctua-2026-02-09.mp3" in names
            assert "noctua-digest-2026-02-09.md" in names
            assert "noctua-digest-2026-02-10.md" in names

        # All MP3s should be deleted
        assert list(episodes_dir.glob("noctua-*.mp3")) == []

        # Digests for last week should be deleted
        assert database.get_digest("2026-02-09") is None
        assert database.get_digest("2026-02-10") is None


# --- API endpoint tests ---

@pytest.fixture
def client(tmp_path):
    """Create a test client with patched paths."""
    episodes_dir = tmp_path / "episodes"
    episodes_dir.mkdir()
    exports_dir = tmp_path / "exports"
    exports_dir.mkdir()
    db_path = tmp_path / "test.db"

    with (
        patch("main.EPISODES_DIR", episodes_dir),
        patch("main.EXPORTS_DIR", exports_dir),
        patch("main.EPISODES_JSON", tmp_path / "episodes.json"),
        patch("main.FEED_PATH", tmp_path / "feed.xml"),
        patch.object(database, "DB_PATH", db_path),
        patch("main._missed_todays_run", return_value=False),
        patch("main._maybe_monday_cleanup"),
    ):
        from main import app
        with TestClient(app) as c:
            yield c, episodes_dir, exports_dir


def test_export_episodes_creates_weekly_zip(client):
    c, episodes_dir, exports_dir = client

    # Patch _pst_now to Wednesday 2026-02-18
    mock_now = datetime(2026, 2, 18, 12, 0, 0, tzinfo=timezone(timedelta(hours=-8)))

    # Create MP3s for this week
    (episodes_dir / "noctua-2026-02-16.mp3").write_bytes(b"mon")
    (episodes_dir / "noctua-2026-02-17.mp3").write_bytes(b"tue")
    (episodes_dir / "noctua-2026-02-18.mp3").write_bytes(b"wed")

    # Create digests for this week
    database.save_digest("2026-02-16", "Monday digest", 3, 500, "A")
    database.save_digest("2026-02-18", "Wednesday digest", 5, 800, "B")

    with patch("main._pst_now", return_value=mock_now):
        res = c.get("/api/export-episodes")

    assert res.status_code == 200
    assert res.headers["content-type"] == "application/zip"
    assert "W08-2026" in res.headers["content-disposition"]

    # ZIP should be cached on disk with MP3s + digests
    zip_path = exports_dir / "hootline-W08-2026.zip"
    assert zip_path.exists()
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        assert "noctua-2026-02-16.mp3" in names
        assert "noctua-digest-2026-02-16.md" in names
        assert "noctua-digest-2026-02-18.md" in names


def test_export_episodes_no_episodes(client):
    c, episodes_dir, exports_dir = client
    mock_now = datetime(2026, 2, 18, 12, 0, 0, tzinfo=timezone(timedelta(hours=-8)))
    with patch("main._pst_now", return_value=mock_now):
        res = c.get("/api/export-episodes")
    assert res.status_code == 404


def test_export_weeks(client):
    c, episodes_dir, exports_dir = client

    # Create a pending export ZIP
    zip_path = exports_dir / "hootline-W07-2026.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("noctua-2026-02-09.mp3", "fake")

    res = c.get("/api/export-weeks")
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 1
    assert data[0]["week_label"] == "W07-2026"
    assert data[0]["filename"] == "hootline-W07-2026.zip"
    assert data[0]["size_bytes"] > 0


def test_download_export_and_delete(client):
    c, episodes_dir, exports_dir = client

    # Create a pending export ZIP
    zip_path = exports_dir / "hootline-W07-2026.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("noctua-2026-02-09.mp3", "fake")

    res = c.get("/api/download-export/hootline-W07-2026.zip")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/zip"

    # File should be deleted after download
    assert not zip_path.exists()


def test_download_export_not_found(client):
    c, _, _ = client
    res = c.get("/api/download-export/nonexistent.zip")
    assert res.status_code == 404


def test_download_export_path_traversal(client):
    c, _, _ = client
    res = c.get("/api/download-export/../../etc/passwd")
    # FastAPI normalizes path segments, so this either 400s or 404s (both safe)
    assert res.status_code in (400, 404)
