"""Tests for feed_builder module."""

import json
from pathlib import Path
from unittest.mock import patch

from src.feed_builder import (
    _build_feed_generator,
    _load_episode_catalog,
    _save_episode_catalog,
    add_episode,
)
from src.models import EpisodeMetadata


def _make_metadata(date: str = "2026-02-16") -> EpisodeMetadata:
    return EpisodeMetadata(
        date=date,
        file_path=Path(f"output/episodes/noctua-{date}.mp3"),
        file_size_bytes=5_000_000,
        duration_seconds=1200,
        duration_formatted="00:20:00",
        topics_summary="Test topic A; Test topic B",
    )


def test_load_episode_catalog_empty(tmp_path):
    with patch("src.feed_builder.EPISODES_JSON", tmp_path / "episodes.json"):
        result = _load_episode_catalog()
        assert result == []


def test_save_and_load_catalog(tmp_path):
    json_path = tmp_path / "episodes.json"
    with patch("src.feed_builder.EPISODES_JSON", json_path):
        episodes = [{"date": "2026-02-16", "file_size_bytes": 5000000}]
        _save_episode_catalog(episodes)
        loaded = _load_episode_catalog()
        assert loaded == episodes


def test_build_feed_generator():
    episodes = [
        {
            "date": "2026-02-16",
            "file_size_bytes": 5000000,
            "duration_seconds": 1200,
            "duration_formatted": "00:20:00",
            "topics_summary": "Topic A; Topic B",
            "published": "2026-02-16T18:00:00+00:00",
        }
    ]
    fg = _build_feed_generator(episodes)
    rss = fg.rss_str(pretty=True).decode()
    assert "Noctua" in rss
    assert "audio/mpeg" in rss
    assert "February 16, 2026" in rss
    assert "itunes" in rss.lower()


def test_add_episode_and_build_feed(tmp_path):
    json_path = tmp_path / "episodes.json"
    feed_path = tmp_path / "feed.xml"
    with (
        patch("src.feed_builder.EPISODES_JSON", json_path),
        patch("src.feed_builder.FEED_PATH", feed_path),
    ):
        metadata = _make_metadata()
        add_episode(metadata)

        assert json_path.exists()
        assert feed_path.exists()

        catalog = json.loads(json_path.read_text())
        assert len(catalog) == 1
        assert catalog[0]["date"] == "2026-02-16"

        feed_content = feed_path.read_text()
        assert "Noctua" in feed_content
        assert "audio/mpeg" in feed_content


def test_add_episode_replaces_same_date(tmp_path):
    json_path = tmp_path / "episodes.json"
    feed_path = tmp_path / "feed.xml"
    with (
        patch("src.feed_builder.EPISODES_JSON", json_path),
        patch("src.feed_builder.FEED_PATH", feed_path),
    ):
        add_episode(_make_metadata("2026-02-16"))
        add_episode(_make_metadata("2026-02-16"))

        catalog = json.loads(json_path.read_text())
        assert len(catalog) == 1
