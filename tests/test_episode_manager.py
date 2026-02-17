"""Tests for episode_manager module."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from src.episode_manager import (
    _convert_to_mp3,
    _format_duration,
    _is_mp3,
    process,
)
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


class TestIsMp3:
    """Tests for _is_mp3 header detection."""

    def test_id3_header(self, tmp_path):
        f = tmp_path / "test.mp3"
        f.write_bytes(b"ID3\x04" + b"\x00" * 100)
        assert _is_mp3(f) is True

    def test_mp3_sync_word(self, tmp_path):
        f = tmp_path / "test.mp3"
        # 0xFF 0xFB = valid MP3 frame sync
        f.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 100)
        assert _is_mp3(f) is True

    def test_mp4_ftyp_header(self, tmp_path):
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00\x00\x00\x18ftypdash" + b"\x00" * 100)
        assert _is_mp3(f) is False

    def test_too_short(self, tmp_path):
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\xff\xfb")
        assert _is_mp3(f) is False

    def test_random_data(self, tmp_path):
        f = tmp_path / "test.mp3"
        f.write_bytes(b"\x00\x01\x02\x03" + b"\x00" * 100)
        assert _is_mp3(f) is False


class TestConvertToMp3:
    """Tests for _convert_to_mp3 ffmpeg conversion."""

    def test_successful_conversion(self, tmp_path):
        src = tmp_path / "test.mp3"
        src.write_bytes(b"fake mp4 data")

        with patch("src.episode_manager.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            # Simulate ffmpeg creating the output file
            tmp_output = src.with_suffix(".tmp.mp3")
            tmp_output.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 100)

            result = _convert_to_mp3(src)

            assert result == src
            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            assert cmd[0] == "ffmpeg"
            assert "-codec:a" in cmd
            assert "libmp3lame" in cmd

    def test_failed_conversion(self, tmp_path):
        src = tmp_path / "test.mp3"
        src.write_bytes(b"fake mp4 data")

        with patch("src.episode_manager.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="error decoding"
            )
            with pytest.raises(EpisodeProcessError, match="ffmpeg conversion failed"):
                _convert_to_mp3(src)
