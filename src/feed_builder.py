"""RSS/podcast feed generation using feedgen."""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from feedgen.feed import FeedGenerator

from config import ShowConfig, settings
from src import database
from src.exceptions import FeedBuildError
from src.models import EpisodeMetadata

logger = logging.getLogger(__name__)

DEFAULT_FEED_PATH = Path("output/feed.xml")
DEFAULT_EPISODES_JSON = Path("output/episodes.json")
MAX_FEED_EPISODES = 30
FALLBACK_RSS_DESCRIPTION = "Your nightly knowledge briefing."


def _resolve_paths(show: ShowConfig | None) -> tuple[Path, Path]:
    """Return (feed_path, episodes_json) for the given show."""
    if show:
        return show.feed_path, show.episodes_json_path
    return DEFAULT_FEED_PATH, DEFAULT_EPISODES_JSON


def _load_episode_catalog(show: ShowConfig | None = None) -> list[dict]:
    """Load the episode catalog from disk."""
    _, episodes_json = _resolve_paths(show)
    if episodes_json.exists():
        return json.loads(episodes_json.read_text())
    return []


def _save_episode_catalog(episodes: list[dict], show: ShowConfig | None = None) -> None:
    """Save the episode catalog to disk."""
    _, episodes_json = _resolve_paths(show)
    episodes_json.parent.mkdir(parents=True, exist_ok=True)
    episodes_json.write_text(json.dumps(episodes, indent=2))


def _build_feed_generator(episodes: list[dict], show: ShowConfig | None = None) -> FeedGenerator:
    """Create and configure a FeedGenerator with podcast extension.

    Args:
        episodes: List of episode metadata dicts.
        show: Show-specific config for metadata and URLs.

    Returns:
        Configured FeedGenerator.
    """
    fg = FeedGenerator()
    fg.load_extension("podcast")

    title = show.podcast_title if show else settings.podcast_title
    description = show.podcast_description if show else settings.podcast_description
    show_id = show.show_id if show else "hootline"

    # In legacy mode (output_dir == "output"), use root URLs for backward compat
    is_legacy = show and show.output_dir == Path("output")
    if is_legacy:
        feed_url = f"{settings.base_url}/feed.xml"
        episode_url_prefix = f"{settings.base_url}/episodes"
    else:
        feed_url = f"{settings.base_url}/{show_id}/feed.xml"
        episode_url_prefix = f"{settings.base_url}/{show_id}/episodes"

    fg.title(title)
    fg.description(description)
    fg.link(href=feed_url, rel="self")
    fg.link(href=settings.base_url, rel="alternate")
    fg.language("en")
    fg.generator(f"{title} Podcast Generator")

    # Channel-level image (standard RSS) — per-show icon
    if show_id == "sparrow":
        image_url = f"{settings.base_url}/static/noctua_owl.png"
    else:
        image_url = f"{settings.base_url}/static/noctua-owl.png"

    fg.image(
        url=image_url,
        title=title,
        link=settings.base_url,
    )

    # Podcast-specific metadata
    fg.podcast.itunes_category("News", "Daily News")
    fg.podcast.itunes_author("Aannesha Satpati")
    fg.podcast.itunes_explicit("no")
    fg.podcast.itunes_summary(description)
    fg.podcast.itunes_owner(name="Aannesha Satpati", email="aannesha.satpati@gmail.com")
    fg.podcast.itunes_image(image_url)

    # Add episodes (most recent first)
    for ep in sorted(episodes, key=lambda e: e["date"], reverse=True)[:MAX_FEED_EPISODES]:
        fe = fg.add_entry()
        # Use GCS URL if available, otherwise fall back to local URL
        # Always include file size as version param so podcast apps
        # re-download if the audio file changes.
        if ep.get("gcs_url"):
            mp3_url = ep["gcs_url"]
        else:
            mp3_url = f"{episode_url_prefix}/noctua-{ep['date']}.mp3?v={ep['file_size_bytes']}"

        fe.id(mp3_url)

        # Format title as the date, e.g. "February 17, 2026"
        try:
            dt = datetime.strptime(ep["date"], "%Y-%m-%d")
            display_date = dt.strftime("%B %-d, %Y")
        except ValueError:
            display_date = ep["date"]

        fe.title(display_date)
        fallback = f"Your nightly knowledge briefing from {title}."
        fe.description(ep.get("rss_summary") or fallback)
        fe.published(
            datetime.fromisoformat(ep["published"]) if "published" in ep
            else datetime.now(UTC)
        )

        # Enclosure (the MP3 file)
        fe.enclosure(mp3_url, str(ep["file_size_bytes"]), "audio/mpeg")

        # Podcast extensions
        fe.podcast.itunes_duration(ep.get("duration_formatted", "00:00:00"))
        fe.podcast.itunes_summary(ep.get("rss_summary") or fallback)
        fe.podcast.itunes_explicit("no")

    return fg


def add_episode(metadata: EpisodeMetadata, show: ShowConfig | None = None) -> None:
    """Add a new episode to the podcast RSS feed.

    Args:
        metadata: Episode metadata (duration, size, path, summary).
        show: Show-specific config for paths and metadata.
    """
    db_path = show.db_path if show else None
    try:
        episodes = _load_episode_catalog(show)

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

        _save_episode_catalog(episodes, show)
        build_feed(show)

        # Permanent archive (no limit)
        database.save_episode(
            date=metadata.date,
            file_size_bytes=metadata.file_size_bytes,
            duration_seconds=metadata.duration_seconds,
            duration_formatted=metadata.duration_formatted,
            topics_summary=metadata.topics_summary,
            rss_summary=metadata.rss_summary,
            gcs_url=metadata.gcs_url,
            db_path=db_path,
        )

        logger.info("Added episode for %s to feed", metadata.date)

    except Exception as e:
        raise FeedBuildError(f"Failed to add episode to feed: {e}") from e


def sync_catalog_from_db(show: ShowConfig | None = None) -> None:
    """Rebuild episodes.json and feed.xml from the database.

    This ensures the RSS feed matches the database (source of truth)
    after deploys or manual DB edits. Preserves revision numbers from
    the existing catalog (used for cache-busting with podcast services).
    """
    db_path = show.db_path if show else None

    # Preserve revision numbers from existing catalog
    old_catalog = _load_episode_catalog(show)
    old_revisions = {ep["date"]: ep.get("revision", 1) for ep in old_catalog}

    episodes_db = database.list_episodes(db_path=db_path)
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
    _save_episode_catalog(catalog, show)
    build_feed(show)
    logger.info("Synced episodes.json from DB (%d episodes), feed rebuilt.", len(catalog))


def bump_revision(date: str, show: ShowConfig | None = None) -> int:
    """Increment the revision for an episode, forcing podcast apps to re-download.

    Returns the new revision number.
    """
    catalog = _load_episode_catalog(show)
    new_rev = 1
    for ep in catalog:
        if ep["date"] == date:
            ep["revision"] = ep.get("revision", 1) + 1
            new_rev = ep["revision"]
            break
    _save_episode_catalog(catalog, show)
    build_feed(show)
    logger.info("Bumped revision for %s to v%d, feed rebuilt.", date, new_rev)
    return new_rev


def clear_feed(show: ShowConfig | None = None) -> None:
    """Remove all episodes from the feed catalog and rebuild an empty feed."""
    _save_episode_catalog([], show)
    build_feed(show)
    logger.info("Feed cleared — episodes.json emptied and feed.xml rebuilt.")


def build_feed(show: ShowConfig | None = None) -> str:
    """Build or rebuild the complete RSS feed XML.

    Returns:
        Path to the generated feed.xml file.
    """
    try:
        feed_path, _ = _resolve_paths(show)
        episodes = _load_episode_catalog(show)
        fg = _build_feed_generator(episodes, show)

        feed_path.parent.mkdir(parents=True, exist_ok=True)
        fg.rss_file(str(feed_path), pretty=True)

        logger.info("Feed written to %s (%d episodes)", feed_path, len(episodes))
        return str(feed_path)

    except Exception as e:
        raise FeedBuildError(f"Failed to build feed: {e}") from e
