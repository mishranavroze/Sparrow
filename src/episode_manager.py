"""Manage downloaded MP3 files and extract metadata."""

import logging
import os
import subprocess
from pathlib import Path

from mutagen.mp3 import MP3

from src.exceptions import EpisodeProcessError
from src.models import EpisodeMetadata

logger = logging.getLogger(__name__)

EPISODES_DIR = Path("output/episodes")


def _is_mp3(path: Path) -> bool:
    """Check if a file is a valid MP3 by inspecting its header bytes."""
    with open(path, "rb") as f:
        header = f.read(4)
    if len(header) < 4:
        return False
    # ID3 tag header (ID3v2)
    if header[:3] == b"ID3":
        return True
    # MP3 sync word: first 11 bits set (0xFFE0 mask)
    if (header[0] == 0xFF) and (header[1] & 0xE0) == 0xE0:
        return True
    return False


def _convert_to_mp3(path: Path) -> Path:
    """Convert a non-MP3 audio file to MP3 using ffmpeg."""
    tmp_output = path.with_suffix(".tmp.mp3")
    cmd = [
        "ffmpeg", "-i", str(path),
        "-codec:a", "libmp3lame", "-qscale:a", "2",
        "-y", str(tmp_output),
    ]
    logger.info("Converting %s to MP3 via ffmpeg", path.name)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tmp_output.unlink(missing_ok=True)
        raise EpisodeProcessError(
            f"ffmpeg conversion failed (exit {result.returncode}): {result.stderr[:500]}"
        )
    # Replace original file with converted MP3
    tmp_output.replace(path)
    logger.info("Conversion complete: %s is now a valid MP3", path.name)
    return path


def _ensure_mp3(path: Path) -> Path:
    """Ensure the file at path is a valid MP3, converting if necessary."""
    if not _is_mp3(path):
        logger.warning("%s is not a valid MP3 — converting with ffmpeg", path.name)
        return _convert_to_mp3(path)
    return path


MAX_EPISODES = 30


def _format_duration(seconds: int) -> str:
    """Format seconds as HH:MM:SS.

    Args:
        seconds: Duration in seconds.

    Returns:
        Formatted duration string.
    """
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _cleanup_old_episodes() -> None:
    """Remove old episodes beyond the retention limit."""
    episodes = sorted(EPISODES_DIR.glob("noctua-*.mp3"))
    if len(episodes) > MAX_EPISODES:
        to_remove = episodes[: len(episodes) - MAX_EPISODES]
        for ep in to_remove:
            logger.info("Removing old episode: %s", ep.name)
            ep.unlink()


def process(mp3_path: Path, topics_summary: str) -> EpisodeMetadata:
    """Validate a downloaded MP3 and extract metadata.

    Args:
        mp3_path: Path to the downloaded MP3 file.
        topics_summary: Brief summary of episode topics.

    Returns:
        EpisodeMetadata with duration, file size, etc.
    """
    try:
        if not mp3_path.exists():
            raise EpisodeProcessError(f"MP3 file not found: {mp3_path}")

        file_size = mp3_path.stat().st_size
        if file_size == 0:
            raise EpisodeProcessError(f"MP3 file is empty: {mp3_path}")

        # Ensure the file is actually MP3 (NotebookLM may serve MP4/DASH)
        mp3_path = _ensure_mp3(mp3_path)
        file_size = mp3_path.stat().st_size  # re-read after possible conversion

        # Extract duration using mutagen
        audio = MP3(str(mp3_path))
        duration_seconds = int(audio.info.length)

        if duration_seconds < 10:
            raise EpisodeProcessError(
                f"MP3 duration suspiciously short ({duration_seconds}s): {mp3_path}"
            )

        # Extract date from filename (noctua-YYYY-MM-DD.mp3)
        stem = mp3_path.stem
        date_str = stem.replace("noctua-", "") if stem.startswith("noctua-") else stem

        # Ensure file is in the episodes directory with canonical name
        canonical_path = EPISODES_DIR / f"noctua-{date_str}.mp3"
        if mp3_path != canonical_path:
            EPISODES_DIR.mkdir(parents=True, exist_ok=True)
            os.rename(str(mp3_path), str(canonical_path))
            logger.info("Moved episode to %s", canonical_path)

        duration_formatted = _format_duration(duration_seconds)

        logger.info(
            "Episode processed: %s, duration=%s, size=%.1fMB",
            canonical_path.name,
            duration_formatted,
            file_size / (1024 * 1024),
        )

        # Clean up old episodes
        _cleanup_old_episodes()

        return EpisodeMetadata(
            date=date_str,
            file_path=canonical_path,
            file_size_bytes=file_size,
            duration_seconds=duration_seconds,
            duration_formatted=duration_formatted,
            topics_summary=topics_summary,
        )

    except EpisodeProcessError:
        raise
    except Exception as e:
        raise EpisodeProcessError(f"Failed to process episode: {e}") from e
