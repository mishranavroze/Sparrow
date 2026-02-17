"""RSS/podcast feed generation using feedgen."""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from feedgen.feed import FeedGenerator

from config import settings
from src.exceptions import FeedBuildError
from src.models import EpisodeMetadata

logger = logging.getLogger(__name__)

FEED_PATH = Path("output/feed.xml")
EPISODES_JSON = Path("output/episodes.json")
MAX_FEED_EPISODES = 30


def _load_episode_catalog() -> list[dict]:
    """Load the episode catalog from disk."""
    if EPISODES_JSON.exists():
        return json.loads(EPISODES_JSON.read_text())
    return []


def _save_episode_catalog(episodes: list[dict]) -> None:
    """Save the episode catalog to disk."""
    EPISODES_JSON.parent.mkdir(parents=True, exist_ok=True)
    EPISODES_JSON.write_text(json.dumps(episodes, indent=2))


def _build_feed_generator(episodes: list[dict]) -> FeedGenerator:
    """Create and configure a FeedGenerator with podcast extension.

    Args:
        episodes: List of episode metadata dicts.

    Returns:
        Configured FeedGenerator.
    """
    fg = FeedGenerator()
    fg.load_extension("podcast")

    fg.title(settings.podcast_title)
    fg.description(settings.podcast_description)
    fg.link(href=f"{settings.base_url}/feed.xml", rel="self")
    fg.link(href=settings.base_url, rel="alternate")
    fg.language("en")
    fg.generator("The Hootline Podcast Generator")

    # Channel-level image (standard RSS)
    fg.image(
        url=f"{settings.base_url}/static/noctua-owl.png",
        title=settings.podcast_title,
        link=settings.base_url,
    )

    # Podcast-specific metadata
    fg.podcast.itunes_category("News", "Daily News")
    fg.podcast.itunes_author("Aannesha Satpati")
    fg.podcast.itunes_explicit("no")
    fg.podcast.itunes_summary(settings.podcast_description)
    fg.podcast.itunes_owner(name="Aannesha Satpati", email="aannesha.satpati@gmail.com")
    fg.podcast.itunes_image(f"{settings.base_url}/static/noctua-owl.png")

    # Add episodes (most recent first)
    for ep in sorted(episodes, key=lambda e: e["date"], reverse=True)[:MAX_FEED_EPISODES]:
        fe = fg.add_entry()
        fe.id(f"{settings.base_url}/episodes/noctua-{ep['date']}.mp3")

        # Format title as the date, e.g. "February 17, 2026"
        try:
            dt = datetime.strptime(ep["date"], "%Y-%m-%d")
            display_date = dt.strftime("%B %-d, %Y")
        except ValueError:
            display_date = ep["date"]

        fe.title(display_date)
        fe.description(ep.get("topics_summary", "Daily knowledge briefing."))
        fe.published(
            datetime.fromisoformat(ep["published"]) if "published" in ep
            else datetime.now(UTC)
        )

        # Enclosure (the MP3 file)
        mp3_url = f"{settings.base_url}/episodes/noctua-{ep['date']}.mp3"
        fe.enclosure(mp3_url, str(ep["file_size_bytes"]), "audio/mpeg")

        # Podcast extensions
        fe.podcast.itunes_duration(ep.get("duration_formatted", "00:00:00"))
        fe.podcast.itunes_summary(ep.get("topics_summary", ""))
        fe.podcast.itunes_explicit("no")

    return fg


def add_episode(metadata: EpisodeMetadata) -> None:
    """Add a new episode to the podcast RSS feed.

    Args:
        metadata: Episode metadata (duration, size, path, summary).
    """
    try:
        episodes = _load_episode_catalog()

        # Remove existing entry for the same date (re-generation)
        episodes = [e for e in episodes if e["date"] != metadata.date]

        episodes.append({
            "date": metadata.date,
            "file_size_bytes": metadata.file_size_bytes,
            "duration_seconds": metadata.duration_seconds,
            "duration_formatted": metadata.duration_formatted,
            "topics_summary": metadata.topics_summary,
            "published": datetime.now(UTC).isoformat(),
        })

        # Keep only the most recent episodes
        episodes = sorted(episodes, key=lambda e: e["date"], reverse=True)[:MAX_FEED_EPISODES]

        _save_episode_catalog(episodes)
        build_feed()

        logger.info("Added episode for %s to feed", metadata.date)

    except Exception as e:
        raise FeedBuildError(f"Failed to add episode to feed: {e}") from e


def build_feed() -> str:
    """Build or rebuild the complete RSS feed XML.

    Returns:
        Path to the generated feed.xml file.
    """
    try:
        episodes = _load_episode_catalog()
        fg = _build_feed_generator(episodes)

        FEED_PATH.parent.mkdir(parents=True, exist_ok=True)
        fg.rss_file(str(FEED_PATH), pretty=True)

        logger.info("Feed written to %s (%d episodes)", FEED_PATH, len(episodes))
        return str(FEED_PATH)

    except Exception as e:
        raise FeedBuildError(f"Failed to build feed: {e}") from e
