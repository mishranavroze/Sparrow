"""Tests for the preparation workflow: Prepare → Upload → Publish.

IMPORTANT: All test dates use 2099 to avoid any collision with real data.
"""

import struct
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src import database


def _make_mp3_bytes() -> bytes:
    """Create minimal valid MP3 bytes (MPEG sync word + padding)."""
    # MP3 frame header: sync=0xFFE0, MPEG1, Layer3, 128kbps, 44100Hz, stereo
    header = b"\xff\xfb\x90\x00"
    return header + b"\x00" * 1000


def _make_compiled_digest(date="2099-03-15", text="Test digest", article_count=10,
                          total_words=1000, topics_summary="Topics",
                          rss_summary="", segment_counts=None, segment_sources=None):
    """Create a mock CompiledDigest for tests."""
    from src.models import CompiledDigest
    return CompiledDigest(
        date=date,
        text=text,
        article_count=article_count,
        total_words=total_words,
        topics_summary=topics_summary,
        rss_summary=rss_summary,
        segment_counts=segment_counts or {},
        segment_sources=segment_sources or {},
    )


@pytest.fixture
def client(tmp_path):
    """Create a test client with patched paths and reset preparation state."""
    import main

    episodes_dir = tmp_path / "episodes"
    episodes_dir.mkdir()
    exports_dir = tmp_path / "exports"
    exports_dir.mkdir()
    db_path = tmp_path / "test.db"

    # Reset preparation state before each test
    main._preparation_active = False
    main._preparation_date = None
    main._preparation_cancelled = False
    main._generation_running = False
    main._preparation_digest = None
    main._preparation_error = None

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
            yield c, episodes_dir, tmp_path


# --- database.delete_digest ---

def test_database_delete_digest(tmp_path):
    """Unit test for the new delete_digest function."""
    db_path = tmp_path / "test.db"
    with patch.object(database, "DB_PATH", db_path):
        # No digest to delete
        assert database.delete_digest("2099-01-01") is False

        # Save and delete
        database.save_digest("2099-01-01", "Test content", 5, 500, "Topics")
        assert database.get_digest("2099-01-01") is not None
        assert database.delete_digest("2099-01-01") is True
        assert database.get_digest("2099-01-01") is None

        # Already deleted
        assert database.delete_digest("2099-01-01") is False


def test_database_delete_episode(tmp_path):
    """Unit test for the new delete_episode function."""
    db_path = tmp_path / "test.db"
    with patch.object(database, "DB_PATH", db_path):
        assert database.delete_episode("2099-01-01") is False

        database.save_episode("2099-01-01", 5000000, 1200, "00:20:00", "Topics")
        assert database.has_episode("2099-01-01") is True
        assert database.delete_episode("2099-01-01") is True
        assert database.has_episode("2099-01-01") is False


def test_database_save_digest_force(tmp_path):
    """save_digest with force=True bypasses episode lock."""
    db_path = tmp_path / "test.db"
    with patch.object(database, "DB_PATH", db_path):
        database.save_digest("2099-01-01", "Original", 5, 500, "Topics A")
        database.save_episode("2099-01-01", 5000000, 1200, "00:20:00", "Topics A")

        # Without force — silently refused
        database.save_digest("2099-01-01", "New content", 10, 1000, "Topics B")
        assert database.get_digest("2099-01-01")["markdown_text"] == "Original"

        # With force — overwrites
        database.save_digest("2099-01-01", "Forced content", 15, 1500, "Topics C", force=True)
        assert database.get_digest("2099-01-01")["markdown_text"] == "Forced content"


# --- POST /api/start-preparation ---

def test_start_preparation_no_digest(client):
    """Starting preparation without existing digest triggers generation."""
    c, episodes_dir, tmp_path = client

    async def noop():
        pass

    with patch("main.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2099, 3, 15, 12, 0, 0, tzinfo=UTC)
        mock_dt.strptime = datetime.strptime
        # Mock _run_generation to avoid actually running
        with patch("main._run_generation", return_value=noop()):
            res = c.post("/api/start-preparation")

    assert res.status_code == 200
    data = res.json()
    assert data["state"] == "generating"
    assert data["date"] == "2099-03-15"

    import main
    assert main._preparation_active is True


def test_start_preparation_existing_digest(client):
    """Starting preparation with existing digest still triggers new generation.

    Existing digest in DB should be untouched (not deleted).
    """
    c, episodes_dir, tmp_path = client

    database.save_digest("2099-03-15", "Old digest", 10, 1000, "Topics A")

    async def noop():
        pass

    with patch("main.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2099, 3, 15, 12, 0, 0, tzinfo=UTC)
        mock_dt.strptime = datetime.strptime
        with patch("main._run_generation", return_value=noop()):
            res = c.post("/api/start-preparation")

    assert res.status_code == 200
    data = res.json()
    assert data["state"] == "generating"
    # Old digest should still exist in DB (untouched until Publish)
    assert database.get_digest("2099-03-15") is not None
    assert database.get_digest("2099-03-15")["markdown_text"] == "Old digest"


def test_start_preparation_with_existing_episode(client):
    """Starting preparation when episode exists should NOT clear old data.

    Existing episode + digest in DB and RSS stay untouched until Publish.
    """
    c, episodes_dir, tmp_path = client
    import main

    database.save_digest("2099-03-15", "Old digest", 5, 500, "Topics")
    database.save_episode("2099-03-15", 5000000, 1200, "00:20:00", "Topics")
    old_mp3 = episodes_dir / "noctua-2099-03-15.mp3"
    old_mp3.write_bytes(b"old audio")

    async def noop():
        pass

    with patch("main.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2099, 3, 15, 12, 0, 0, tzinfo=UTC)
        mock_dt.strptime = datetime.strptime
        with patch("main._run_generation", return_value=noop()):
            res = c.post("/api/start-preparation")

    assert res.status_code == 200
    data = res.json()
    assert data["state"] == "generating"

    # Old digest and episode record should still exist (untouched)
    assert database.get_digest("2099-03-15") is not None
    assert database.has_episode("2099-03-15") is True
    # MP3 should still exist
    assert old_mp3.exists()
    assert main._preparation_active is True


# --- POST /api/upload-episode (now preview mode) ---

def test_upload_returns_metadata_without_publishing(client):
    """Upload should return metadata but NOT create episode in DB or RSS.

    Audio is saved as .prep.mp3 to avoid overwriting existing episode.
    """
    c, episodes_dir, tmp_path = client

    database.save_digest("2099-03-15", "Test digest", 10, 1000, "Topics")

    # Create a valid MP3 file to upload
    mp3_data = _make_mp3_bytes()

    with (
        patch("src.episode_manager._ensure_mp3", side_effect=lambda p: p),
        patch("mutagen.mp3.MP3") as mock_mp3,
    ):
        mock_mp3.return_value.info.length = 120.5
        res = c.post(
            "/api/upload-episode",
            data={"date": "2099-03-15"},
            files={"file": ("test.mp3", mp3_data, "audio/mpeg")},
        )

    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert "Preview ready" in data["message"]
    assert data["episode"]["date"] == "2099-03-15"
    assert data["episode"]["duration_formatted"] == "00:02:00"
    # Audio saved as .prep.mp3
    assert data["episode"]["audio_url"] == "/episodes/noctua-2099-03-15.prep.mp3"

    # Prep MP3 should exist on disk
    assert (episodes_dir / "noctua-2099-03-15.prep.mp3").exists()

    # Episode should NOT be in the database
    assert database.has_episode("2099-03-15") is False

    # episodes.json should NOT exist or be empty
    episodes_json = tmp_path / "episodes.json"
    assert not episodes_json.exists()


# --- POST /api/publish-episode ---

def test_publish_adds_to_rss_and_db(client):
    """Publishing should add episode to DB and RSS, and clear preparation state."""
    c, episodes_dir, tmp_path = client
    import main

    # Set up in-memory preparation digest (not in DB)
    digest = _make_compiled_digest(
        date="2099-03-15", text="New digest", article_count=10,
        total_words=1000, topics_summary="Topics A", rss_summary="Test summary",
    )
    main._preparation_active = True
    main._preparation_date = "2099-03-15"
    main._preparation_digest = digest

    # Create prep MP3 on disk (not canonical)
    prep_mp3 = episodes_dir / "noctua-2099-03-15.prep.mp3"
    prep_mp3.write_bytes(_make_mp3_bytes())

    with (
        patch("src.episode_manager.process") as mock_process,
        patch("src.feed_builder.add_episode") as mock_add,
    ):
        from src.models import EpisodeMetadata
        mp3_path = episodes_dir / "noctua-2099-03-15.mp3"
        mock_process.return_value = EpisodeMetadata(
            date="2099-03-15",
            file_path=mp3_path,
            file_size_bytes=1004,
            duration_seconds=120,
            duration_formatted="00:02:00",
            topics_summary="Topics A",
            rss_summary="Test summary",
            gcs_url="",
        )
        res = c.post("/api/publish-episode", data={"date": "2099-03-15"})

    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert "published" in data["message"].lower()

    # Preparation state should be cleared
    assert main._preparation_active is False
    assert main._preparation_digest is None

    # Prep MP3 should be renamed to canonical
    assert not prep_mp3.exists()
    assert (episodes_dir / "noctua-2099-03-15.mp3").exists()

    # Digest should now be in DB (saved with force)
    saved = database.get_digest("2099-03-15")
    assert saved is not None
    assert saved["markdown_text"] == "New digest"

    # episode_manager.process and feed_builder.add_episode should have been called
    mock_process.assert_called_once()
    mock_add.assert_called_once()


def test_publish_no_digest(client):
    """Publishing without an in-memory digest should return 404."""
    c, episodes_dir, tmp_path = client
    res = c.post("/api/publish-episode", data={"date": "2099-03-15"})
    assert res.status_code == 404


def test_publish_no_mp3(client):
    """Publishing without a prep MP3 on disk should return 404."""
    c, episodes_dir, tmp_path = client
    import main

    # Set in-memory digest but no prep MP3
    main._preparation_digest = _make_compiled_digest()
    res = c.post("/api/publish-episode", data={"date": "2099-03-15"})
    assert res.status_code == 404


# --- POST /api/cancel-preparation ---

def test_cancel_cleans_up_prep_only(client):
    """Cancel should only delete prep MP3 and clear in-memory state.

    Existing digest and episode in DB should be untouched.
    """
    c, episodes_dir, tmp_path = client
    import main

    # Existing data in DB (should be preserved)
    database.save_digest("2099-03-15", "Test digest", 10, 1000, "Topics")

    # Prep MP3 (should be deleted)
    prep_mp3 = episodes_dir / "noctua-2099-03-15.prep.mp3"
    prep_mp3.write_bytes(_make_mp3_bytes())

    # Canonical MP3 (should be preserved)
    canonical_mp3 = episodes_dir / "noctua-2099-03-15.mp3"
    canonical_mp3.write_bytes(b"existing episode audio")

    main._preparation_active = True
    main._preparation_date = "2099-03-15"
    main._preparation_digest = _make_compiled_digest()

    res = c.post("/api/cancel-preparation")

    assert res.status_code == 200
    assert main._preparation_active is False
    assert main._preparation_date is None
    assert main._preparation_digest is None

    # Digest should still exist in DB
    assert database.get_digest("2099-03-15") is not None

    # Prep MP3 should be deleted
    assert not prep_mp3.exists()

    # Canonical MP3 should be preserved
    assert canonical_mp3.exists()


def test_cancel_during_generation(client):
    """Cancel during generation should set the cancelled flag."""
    c, episodes_dir, tmp_path = client
    import main

    main._preparation_active = True
    main._preparation_date = "2099-03-15"
    main._generation_running = True

    res = c.post("/api/cancel-preparation")

    assert res.status_code == 200
    assert main._preparation_cancelled is True
    assert main._preparation_active is False


def test_cancel_preserves_published_episode(client):
    """Cancel should not touch a digest or episode that exists in DB."""
    c, episodes_dir, tmp_path = client
    import main

    database.save_digest("2099-03-15", "Locked digest", 10, 1000, "Topics")
    database.save_episode("2099-03-15", 5000000, 1200, "00:20:00", "Topics")

    main._preparation_active = True
    main._preparation_date = "2099-03-15"

    res = c.post("/api/cancel-preparation")

    assert res.status_code == 200
    # Digest and episode should still exist
    assert database.get_digest("2099-03-15") is not None
    assert database.has_episode("2099-03-15") is True


# --- GET /api/latest-episode ---

def test_latest_includes_preparation_state(client):
    """Latest episode response should include preparation field when active."""
    c, episodes_dir, tmp_path = client
    import main

    # Set in-memory preparation digest (not DB)
    main._preparation_active = True
    main._preparation_date = "2099-03-15"
    main._preparation_digest = _make_compiled_digest()

    res = c.get("/api/latest-episode")
    data = res.json()

    assert data["preparation"] is not None
    assert data["preparation"]["active"] is True
    assert data["preparation"]["state"] == "digest_ready"
    assert data["preparation"]["date"] == "2099-03-15"
    assert data["preparation"]["digest"]["article_count"] == 10


def test_latest_preparation_null_when_inactive(client):
    """Latest episode response should have preparation=null when not active."""
    c, episodes_dir, tmp_path = client
    import main

    main._preparation_active = False

    res = c.get("/api/latest-episode")
    data = res.json()

    assert data["preparation"] is None


def test_latest_preparation_audio_uploaded(client):
    """Latest with prep active + prep MP3 on disk should show audio_uploaded state."""
    c, episodes_dir, tmp_path = client
    import main

    main._preparation_active = True
    main._preparation_date = "2099-03-15"
    main._preparation_digest = _make_compiled_digest()

    # Create prep MP3 (not canonical)
    prep_mp3 = episodes_dir / "noctua-2099-03-15.prep.mp3"
    prep_mp3.write_bytes(_make_mp3_bytes())

    res = c.get("/api/latest-episode")
    data = res.json()

    assert data["preparation"]["state"] == "audio_uploaded"
    assert data["preparation"]["audio"] is not None
    assert data["preparation"]["audio"]["audio_url"] == "/episodes/noctua-2099-03-15.prep.mp3"


def test_latest_preparation_generating(client):
    """Latest with prep active + generation running should show generating state."""
    c, episodes_dir, tmp_path = client
    import main

    main._preparation_active = True
    main._preparation_date = "2099-03-15"
    main._generation_running = True

    res = c.get("/api/latest-episode")
    data = res.json()

    assert data["preparation"]["state"] == "generating"
    assert data["preparation"]["generating"] is True


def test_latest_preparation_existing_episode_flag(client):
    """Preparation response should include existing_episode flag when applicable."""
    c, episodes_dir, tmp_path = client
    import main

    # Published episode exists in DB
    database.save_episode("2099-03-15", 5000000, 1200, "00:20:00", "Topics")

    main._preparation_active = True
    main._preparation_date = "2099-03-15"
    main._preparation_digest = _make_compiled_digest()

    res = c.get("/api/latest-episode")
    data = res.json()

    assert data["preparation"]["existing_episode"] is True


# --- GET /api/preparation-digest ---

def test_preparation_digest_download(client):
    """Preparation digest endpoint should serve the in-memory digest."""
    c, episodes_dir, tmp_path = client
    import main

    main._preparation_digest = _make_compiled_digest(text="# My Digest\nContent here")

    res = c.get("/api/preparation-digest")
    assert res.status_code == 200
    assert "# My Digest" in res.text
    assert "text/markdown" in res.headers["content-type"]


def test_preparation_digest_404_when_none(client):
    """Preparation digest endpoint should return 404 when no digest in memory."""
    c, episodes_dir, tmp_path = client
    res = c.get("/api/preparation-digest")
    assert res.status_code == 404
