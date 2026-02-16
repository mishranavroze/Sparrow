"""Tests for episode_manager module."""

from pathlib import Path

import pytest

from src.episode_manager import _format_duration, process
from src.exceptions import EpisodeProcessError


def test_format_duration():
    assert _format_duration(0) == "00:00:00"
    assert _format_duration(65) == "00:01:05"
    assert _format_duration(3661) == "01:01:01"
    assert _format_duration(1200) == "00:20:00"


def test_process_missing_file():
    with pytest.raises(EpisodeProcessError, match="not found"):
        process(Path("/nonexistent/file.mp3"), "test summary")


def test_process_empty_file(tmp_path):
    mp3_path = tmp_path / "noctua-2026-02-16.mp3"
    mp3_path.write_bytes(b"")
    with pytest.raises(EpisodeProcessError, match="empty"):
        process(mp3_path, "test summary")
