"""RSS/podcast feed generation using feedgen."""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from feedgen.feed import FeedGenerator

from config import settings
from src import database
from src.exceptions import FeedBuildError
from src.models import EpisodeMetadata

logger = logging.getLogger(__name__)

FEED_PATH = Path("output/feed.xml")
EPISODES_JSON = Path("output/episodes.json")
MAX_FEED_EPISODES = 30
FALLBACK_RSS_DESCRIPTION = "Your nightly knowledge briefing from The Hootline."


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
        # Use GCS URL if available, otherwise fall back to local URL
        # Always include file size as version param so podcast apps
        # re-download if the audio file changes.
        if ep.get("gcs_url"):
            mp3_url = ep["gcs_url"]
        else:
            mp3_url = f"{settings.base_url}/episodes/noctua-{ep['date']}.mp3?v={ep['file_size_bytes']}"

        fe.id(mp3_url)

        # Format title as the date, e.g. "February 17, 2026"
        try:
            dt = datetime.strptime(ep["date"], "%Y-%m-%d")
            display_date = dt.strftime("%B %-d, %Y")
        except ValueError:
            display_date = ep["date"]

        fe.title(display_date)
        fe.description(ep.get("rss_summary") or FALLBACK_RSS_DESCRIPTION)
        fe.published(
            datetime.fromisoformat(ep["published"]) if "published" in ep
            else datetime.now(UTC)
        )

        # Enclosure (the MP3 file)
        fe.enclosure(mp3_url, str(ep["file_size_bytes"]), "audio/mpeg")

        # Podcast extensions
        fe.podcast.itunes_duration(ep.get("duration_formatted", "00:00:00"))
        fe.podcast.itunes_summary(ep.get("rss_summary") or FALLBACK_RSS_DESCRIPTION)
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

        entry = {
            "date": metadata.date,
            "file_size_bytes": metadata.file_size_bytes,
            "duration_seconds": metadata.duration_seconds,
            "duration_formatted": metadata.duration_formatted,
            "topics_summary": metadata.topics_summary,
            "rss_summary": metadata.rss_summary,
            "published": datetime.now(UTC).isoformat(),
        }
        if metadata.gcs_url:
            entry["gcs_url"] = metadata.gcs_url
        episodes.append(entry)

        # Keep only the most recent episodes
        episodes = sorted(episodes, key=lambda e: e["date"], reverse=True)[:MAX_FEED_EPISODES]

        _save_episode_catalog(episodes)
        build_feed()

        # Permanent archive (no limit)
        database.save_episode(
            date=metadata.date,
            file_size_bytes=metadata.file_size_bytes,
            duration_seconds=metadata.duration_seconds,
            duration_formatted=metadata.duration_formatted,
            topics_summary=metadata.topics_summary,
            rss_summary=metadata.rss_summary,
            gcs_url=metadata.gcs_url,
        )

        logger.info("Added episode for %s to feed", metadata.date)

    except Exception as e:
        raise FeedBuildError(f"Failed to add episode to feed: {e}") from e


def sync_catalog_from_db() -> None:
    """Rebuild episodes.json and feed.xml from the database.

    This ensures the RSS feed matches the database (source of truth)
    after deploys or manual DB edits. Preserves revision numbers from
    the existing catalog (used for cache-busting with podcast services).
    """
    # Preserve revision numbers from existing catalog
    old_catalog = _load_episode_catalog()
    old_revisions = {ep["date"]: ep.get("revision", 1) for ep in old_catalog}

    episodes_db = database.list_episodes()
    catalog = []
    for ep in episodes_db:
        entry = {
            "date": ep["date"],
            "file_size_bytes": ep["file_size_bytes"],
            "duration_seconds": ep["duration_seconds"],
            "duration_formatted": ep["duration_formatted"],
            "topics_summary": ep.get("topics_summary", ""),
            "rss_summary": ep.get("rss_summary", ""),
            "published": ep.get("published_at", ""),
        }
        if ep.get("gcs_url"):
            entry["gcs_url"] = ep["gcs_url"]
        if old_revisions.get(ep["date"], 1) > 1:
            entry["revision"] = old_revisions[ep["date"]]
        catalog.append(entry)
    _save_episode_catalog(catalog)
    build_feed()
    logger.info("Synced episodes.json from DB (%d episodes), feed rebuilt.", len(catalog))


def bump_revision(date: str) -> int:
    """Increment the revision for an episode, forcing podcast apps to re-download.

    Returns the new revision number.
    """
    catalog = _load_episode_catalog()
    new_rev = 1
    for ep in catalog:
        if ep["date"] == date:
            ep["revision"] = ep.get("revision", 1) + 1
            new_rev = ep["revision"]
            break
    _save_episode_catalog(catalog)
    build_feed()
    logger.info("Bumped revision for %s to v%d, feed rebuilt.", date, new_rev)
    return new_rev


def clear_feed() -> None:
    """Remove all episodes from the feed catalog and rebuild an empty feed."""
    _save_episode_catalog([])
    build_feed()
    logger.info("Feed cleared — episodes.json emptied and feed.xml rebuilt.")


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
