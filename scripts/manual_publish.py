#!/usr/bin/env python3
"""Manually publish a podcast episode from pre-existing audio and digest files.

Usage:
    python scripts/manual_publish.py 2026-02-22 path/to/audio.m4a path/to/digest.md
"""

import argparse
import logging
import re
import shutil
import sys
from pathlib import Path

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import shows
from src import database, episode_manager, feed_builder

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def parse_digest(md_text: str) -> dict:
    """Extract metadata from a digest markdown file.

    Returns dict with: segment_counts, total_words, topics_summary, article_count
    """
    segment_counts: dict[str, int] = {}
    current_segment: str | None = None

    for line in md_text.splitlines():
        # Match segment headers like: ## SEGMENT 1: Latest in Tech (~5 minutes)
        seg_match = re.match(r"^## SEGMENT \d+:\s*(.+?)\s*\(", line)
        if seg_match:
            current_segment = seg_match.group(1).strip()
            segment_counts[current_segment] = 0
            continue

        # Count ### sub-headers within each segment
        if current_segment and line.startswith("### "):
            segment_counts[current_segment] = segment_counts.get(current_segment, 0) + 1

    # Total words in the markdown
    total_words = len(md_text.split())

    # Total article count = sum of sub-headers across segments
    article_count = sum(segment_counts.values())

    # Build topics summary like "Latest in Tech (3); World Politics (1)"
    parts = [f"{topic} ({count})" for topic, count in segment_counts.items() if count > 0]
    topics_summary = "; ".join(parts)

    return {
        "segment_counts": segment_counts,
        "total_words": total_words,
        "article_count": article_count,
        "topics_summary": topics_summary,
    }


def make_rss_summary(audio_filename: str) -> str:
    """Create an RSS summary from the audio filename."""
    stem = Path(audio_filename).stem
    # Replace underscores with spaces
    return stem.replace("_", " ")


def main():
    parser = argparse.ArgumentParser(description="Manually publish a podcast episode.")
    parser.add_argument("date", help="Episode date (YYYY-MM-DD)")
    parser.add_argument("audio", help="Path to audio file (e.g. .m4a)")
    parser.add_argument("digest", help="Path to digest markdown file")
    parser.add_argument("--show", default="hootline", help="Show ID (default: hootline)")
    args = parser.parse_args()

    date_str = args.date
    audio_path = Path(args.audio)
    digest_path = Path(args.digest)
    show = shows.get(args.show)

    if not show:
        logger.error("Unknown show: %s (available: %s)", args.show, list(shows.keys()))
        sys.exit(1)
    if not audio_path.exists():
        logger.error("Audio file not found: %s", audio_path)
        sys.exit(1)
    if not digest_path.exists():
        logger.error("Digest file not found: %s", digest_path)
        sys.exit(1)

    # 1. Parse digest
    md_text = digest_path.read_text()
    meta = parse_digest(md_text)
    rss_summary = make_rss_summary(audio_path.name)

    logger.info("Digest parsed: %d words, %d articles, topics: %s",
                meta["total_words"], meta["article_count"], meta["topics_summary"])

    # 2. Copy audio to staging location with canonical name
    episodes_dir = show.episodes_dir
    episodes_dir.mkdir(parents=True, exist_ok=True)
    staged_path = episodes_dir / f"noctua-{date_str}{audio_path.suffix}"
    shutil.copy2(str(audio_path), str(staged_path))
    logger.info("Copied audio to %s", staged_path)

    # 3. Process episode (convert to MP3 if needed, extract duration, upload to GCS)
    episode_meta = episode_manager.process(
        mp3_path=staged_path,
        topics_summary=meta["topics_summary"],
        rss_summary=rss_summary,
        show=show,
    )
    logger.info("Episode processed: duration=%s, size=%.1fMB",
                episode_meta.duration_formatted,
                episode_meta.file_size_bytes / (1024 * 1024))

    # 4. Save digest to database
    database.save_digest(
        date=date_str,
        markdown_text=md_text,
        article_count=meta["article_count"],
        total_words=meta["total_words"],
        topics_summary=meta["topics_summary"],
        rss_summary=rss_summary,
        segment_counts=meta["segment_counts"],
        force=True,
        db_path=show.db_path,
    )
    logger.info("Digest saved to database")

    # 5. Add episode to feed (updates episodes.json, feed.xml, and episodes table)
    feed_builder.add_episode(episode_meta, show=show)
    logger.info("Episode added to feed")

    # Summary
    print(f"\n{'='*60}")
    print(f"Published episode for {date_str}")
    print(f"  MP3:    {episode_meta.file_path}")
    print(f"  Duration: {episode_meta.duration_formatted}")
    print(f"  Size:   {episode_meta.file_size_bytes / (1024*1024):.1f} MB")
    print(f"  Topics: {meta['topics_summary']}")
    if episode_meta.gcs_url:
        print(f"  GCS:    {episode_meta.gcs_url}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
