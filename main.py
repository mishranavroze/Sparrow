"""FastAPI app — serves RSS feed, audio files, and dashboard."""

import asyncio
import io
import json
import logging
import re
import shutil
import subprocess
import zipfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Form, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import ShowConfig, ShowFormat, SHOW_FORMATS, settings, shows
from src import database, episode_manager, feed_builder
from src.models import CompiledDigest

ACCEPTED_AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav", ".ogg", ".webm"}
def _ffmpeg_path() -> str:
    """Resolve ffmpeg: system PATH first, then bundled imageio-ffmpeg fallback."""
    path = shutil.which("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return "ffmpeg"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

PST = timezone(timedelta(hours=-8))


def _pst_now() -> datetime:
    """Return the current datetime in PST (UTC-8)."""
    return datetime.now(PST)


def _iso_week_label(dt: datetime) -> str:
    """Return a week label like 'W08-2026' from a date."""
    iso_year, iso_week, _ = dt.isocalendar()
    return f"W{iso_week:02d}-{iso_year}"


def _week_date_range(dt: datetime) -> tuple[str, str]:
    """Return (monday_str, sunday_str) for the ISO week containing dt."""
    iso_year, iso_week, iso_day = dt.isocalendar()
    monday = dt.date() - timedelta(days=iso_day - 1)
    sunday = monday + timedelta(days=6)
    return monday.strftime("%Y-%m-%d"), sunday.strftime("%Y-%m-%d")


def _add_digests_to_zip(zf: zipfile.ZipFile, mon: str, sun: str,
                        db_path: Path | None = None) -> int:
    """Add digest .md files for dates in [mon, sun] to an open ZipFile."""
    count = 0
    digests = database.list_digests(limit=100, db_path=db_path)
    for d in digests:
        if mon <= d["date"] <= sun:
            full = database.get_digest(d["date"], db_path=db_path)
            if full and full["markdown_text"]:
                zf.writestr(f"noctua-digest-{d['date']}.md", full["markdown_text"])
                count += 1
    return count


# --- Per-show state ---

@dataclass
class ShowState:
    """Mutable per-show state for the preparation workflow."""

    show: ShowConfig
    generation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    generation_running: bool = False
    preparation_active: bool = False
    preparation_date: str | None = None
    preparation_cancelled: bool = False
    preparation_digest: CompiledDigest | None = None
    preparation_error: str | None = None


# Registry: populated during lifespan startup
_show_states: dict[str, ShowState] = {}
_next_scheduled_run: datetime | None = None


def _resolve_show(show_id: str = "") -> ShowState:
    """Resolve a show_id to its ShowState. Defaults to the first show."""
    if show_id and show_id in _show_states:
        return _show_states[show_id]
    # Default to the first configured show
    return next(iter(_show_states.values()))


def _calc_next_run() -> datetime:
    """Calculate the next scheduled run time based on generation_hour and generation_minute."""
    now = datetime.now(UTC)
    target = now.replace(hour=settings.generation_hour, minute=settings.generation_minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def _episode_date_for_latest_run() -> str:
    """Get the episode date (PST) that the most recent scheduled run would produce."""
    now_utc = datetime.now(UTC)
    latest_run = now_utc.replace(
        hour=settings.generation_hour, minute=settings.generation_minute,
        second=0, microsecond=0,
    )
    if latest_run > now_utc:
        latest_run -= timedelta(days=1)
    return latest_run.astimezone(PST).strftime("%Y-%m-%d")


def _today_digest_exists(state: ShowState) -> bool:
    """Check if a digest for the most recent episode date already exists."""
    return database.get_digest(_episode_date_for_latest_run(), db_path=state.show.db_path) is not None


def _missed_todays_run(state: ShowState) -> bool:
    """Return True if the scheduled time already passed today and no digest exists yet."""
    now = datetime.now(UTC)
    target = now.replace(hour=settings.generation_hour, minute=settings.generation_minute, second=0, microsecond=0)
    return now > target and not _today_digest_exists(state)


async def _run_generation(state: ShowState) -> None:
    """Run digest preparation (steps 1-3), guarded by a lock."""
    from generate import generate_digest_only

    show = state.show

    if state.generation_lock.locked():
        logger.warning("[%s] Generation already in progress, skipping.", show.show_id)
        return

    async with state.generation_lock:
        state.generation_running = True

        if not state.preparation_active:
            state.preparation_active = True
            state.preparation_date = datetime.now(PST).strftime("%Y-%m-%d")
            state.preparation_digest = None
            state.preparation_error = None
            stale_prep = show.episodes_dir / f"noctua-{state.preparation_date}.prep.mp3"
            if stale_prep.exists():
                stale_prep.unlink()
                logger.info("[%s] Removed stale prep file: %s", show.show_id, stale_prep.name)

        try:
            await _maybe_monday_cleanup(state)

            original_save = database.save_digest
            database.save_digest = lambda *args, **kwargs: None
            try:
                def _run_sync():
                    loop = asyncio.new_event_loop()
                    try:
                        return loop.run_until_complete(generate_digest_only(show=show))
                    finally:
                        loop.close()

                result = await asyncio.to_thread(_run_sync)
            finally:
                database.save_digest = original_save

            if state.preparation_cancelled:
                state.preparation_digest = None
                state.preparation_error = None
                logger.info("[%s] Preparation cancelled — discarded in-memory digest", show.show_id)
            elif result is None:
                state.preparation_digest = None
                state.preparation_error = "No newsletters found — nothing to prepare."
                logger.info("[%s] Preparation returned no digest (no emails/articles)", show.show_id)
            else:
                state.preparation_digest = result
                state.preparation_error = None
                logger.info("[%s] Preparation digest ready (in-memory only)", show.show_id)
        except Exception as e:
            logger.error("[%s] Digest preparation failed: %s", show.show_id, e)
            state.preparation_error = f"Generation failed: {e}"
        finally:
            state.generation_running = False
            state.preparation_cancelled = False


def _last_week_mp3s_exist(state: ShowState) -> bool:
    """Check if MP3s from last week are still on disk."""
    episodes_dir = state.show.episodes_dir
    if not episodes_dir.exists():
        return False
    now = _pst_now()
    last_week = now - timedelta(weeks=1)
    mon, sun = _week_date_range(last_week)
    for mp3 in episodes_dir.glob("noctua-*.mp3"):
        date_str = mp3.stem.removeprefix("noctua-")
        if mon <= date_str <= sun:
            return True
    return False


def _monday_cleanup(state: ShowState) -> None:
    """Archive last week's MP3s, then clear episodes and digests for that week."""
    show = state.show
    episodes_dir = show.episodes_dir
    exports_dir = show.exports_dir
    db_path = show.db_path

    now = _pst_now()
    last_week = now - timedelta(weeks=1)
    mon, sun = _week_date_range(last_week)
    week_label = _iso_week_label(last_week)

    mp3s = sorted(
        mp3 for mp3 in episodes_dir.glob("noctua-*.mp3")
        if mon <= mp3.stem.removeprefix("noctua-") <= sun
    )

    exports_dir.mkdir(parents=True, exist_ok=True)
    zip_name = f"{show.show_id}-{week_label}.zip"
    zip_path = exports_dir / zip_name
    if mp3s and not zip_path.exists():
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
            for mp3 in mp3s:
                zf.write(mp3, mp3.name)
            digest_count = _add_digests_to_zip(zf, mon, sun, db_path=db_path)
        logger.info("[%s] Archived %d episodes + %d digests to %s", show.show_id, len(mp3s), digest_count, zip_name)

    for mp3 in mp3s:
        mp3.unlink()
        logger.info("[%s] Deleted %s", show.show_id, mp3.name)

    # Only delete last week's digests from DB — never touch the feed/episodes catalog;
    # sync_catalog_from_db will rebuild the feed from whatever episodes remain in the DB.
    database.delete_digests_between(mon, sun, db_path=db_path)
    feed_builder.sync_catalog_from_db(show=show)
    logger.info("[%s] Monday cleanup complete for %s (%s to %s)", show.show_id, week_label, mon, sun)


async def _maybe_monday_cleanup(state: ShowState) -> None:
    """Run Monday cleanup if it's Monday PST and last week's MP3s still exist."""
    now = _pst_now()
    if now.weekday() == 0 and _last_week_mp3s_exist(state):
        logger.info("[%s] Monday PST detected — running weekly cleanup.", state.show.show_id)
        _monday_cleanup(state)


async def _scheduler() -> None:
    """Background fallback scheduler (in case external cron misses)."""
    global _next_scheduled_run
    while True:
        _next_scheduled_run = _calc_next_run()
        wait_seconds = (_next_scheduled_run - datetime.now(UTC)).total_seconds()
        logger.info(
            "Scheduler: next generation at %s UTC (in %.0f minutes)",
            _next_scheduled_run.strftime("%Y-%m-%d %H:%M"),
            wait_seconds / 60,
        )
        await asyncio.sleep(max(wait_seconds, 0))
        logger.info("Scheduler: triggering daily generation for all shows.")
        for state in _show_states.values():
            asyncio.create_task(_run_generation(state))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the background scheduler and check for missed runs on startup."""
    # Initialize per-show state
    for show_id, show in shows.items():
        _show_states[show_id] = ShowState(show=show)

    # Ensure output directories exist and sync feeds for all shows
    for state in _show_states.values():
        show = state.show
        show.episodes_dir.mkdir(parents=True, exist_ok=True)
        show.exports_dir.mkdir(parents=True, exist_ok=True)
        feed_builder.sync_catalog_from_db(show=show)
        await _maybe_monday_cleanup(state)

        if _missed_todays_run(state):
            logger.info("[%s] Startup: missed today's scheduled run — triggering now.", show.show_id)
            asyncio.create_task(_run_generation(state))

    task = asyncio.create_task(_scheduler())
    logger.info("Background scheduler started (%02d:%02d UTC). Shows: %s",
                settings.generation_hour, settings.generation_minute,
                ", ".join(_show_states.keys()))
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="The Hootline", description="Daily podcast generator", lifespan=lifespan)

# Serve static assets (cover image, etc.)
app.mount("/static", StaticFiles(directory="static"), name="static")


# --- API: Show discovery ---

@app.get("/api/shows")
async def api_shows():
    """List all configured shows."""
    return JSONResponse([
        {"id": s.show.show_id, "title": s.show.podcast_title}
        for s in _show_states.values()
    ])


@app.get("/api/show-format")
async def api_show_format(show_id: str = Query(default="")):
    """Return the segment format for a show (used by dashboard JS)."""
    state = _resolve_show(show_id)
    fmt = state.show.format
    return JSONResponse({
        "segments": [{"name": name, "minutes": mins} for name, mins in fmt.segments],
        "intro_minutes": fmt.intro_minutes,
        "outro_minutes": fmt.outro_minutes,
        "total_minutes": fmt.total_minutes,
    })


# --- Dashboard ---

@app.get("/")
async def dashboard():
    """Root redirects to default show dashboard."""
    return HTMLResponse(
        content=_build_dashboard_html(),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/{show_id}/")
async def show_dashboard(show_id: str):
    """Show-specific dashboard."""
    if show_id not in _show_states:
        return Response(content="Show not found.", status_code=404)
    return HTMLResponse(
        content=_build_dashboard_html(show_id),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


# --- API endpoints (show_id query parameter) ---

@app.get("/api/digests")
async def api_digests(show_id: str = Query(default="")):
    """List all digests."""
    state = _resolve_show(show_id)
    return JSONResponse(database.list_digests(db_path=state.show.db_path))


@app.get("/api/digests/{date}")
async def api_digest(date: str, show_id: str = Query(default="")):
    """Get a single digest by date."""
    state = _resolve_show(show_id)
    digest = database.get_digest(date, db_path=state.show.db_path)
    if not digest:
        return JSONResponse({"error": "Digest not found"}, status_code=404)
    return JSONResponse(digest)


@app.get("/api/runs")
async def api_runs(show_id: str = Query(default="")):
    """List pipeline runs."""
    state = _resolve_show(show_id)
    return JSONResponse(database.list_runs(db_path=state.show.db_path))


@app.get("/api/runs/{run_id}")
async def api_run(run_id: str, show_id: str = Query(default="")):
    """Get a single pipeline run."""
    state = _resolve_show(show_id)
    run = database.get_run(run_id, db_path=state.show.db_path)
    if not run:
        return JSONResponse({"error": "Run not found"}, status_code=404)
    return JSONResponse(run)


@app.get("/api/latest-episode")
async def api_latest_episode(show_id: str = Query(default="")):
    """Get the latest episode and the latest digest."""
    state = _resolve_show(show_id)
    show = state.show
    episodes_json = show.episodes_json_path
    episodes_dir = show.episodes_dir
    db_path = show.db_path

    # Determine URL prefix for episodes
    is_legacy = show.output_dir == Path("output")
    ep_url_prefix = "/episodes" if is_legacy else f"/{show.show_id}/episodes"

    episode_data = None
    if episodes_json.exists():
        episodes = json.loads(episodes_json.read_text())
        if episodes:
            for candidate in sorted(episodes, key=lambda e: e["date"], reverse=True):
                gcs_url = candidate.get("gcs_url", "")
                local_file = episodes_dir / f"noctua-{candidate['date']}.mp3"
                if gcs_url or local_file.exists():
                    audio_url = gcs_url or f"{ep_url_prefix}/noctua-{candidate['date']}.mp3"
                    episode_data = {**candidate, "audio_url": audio_url}
                    break

    digest_meta = None
    all_digests = database.list_digests(limit=1, db_path=db_path)
    if all_digests:
        latest_digest = database.get_digest(all_digests[0]["date"], db_path=db_path)
        if latest_digest:
            has_ep = database.has_episode(latest_digest["date"], db_path=db_path)
            seg_counts = json.loads(latest_digest.get("segment_counts") or "{}")

            # Digest download URL
            if is_legacy:
                dl_url = f"/digests/{latest_digest['date']}.md"
            else:
                dl_url = f"/{show.show_id}/digests/{latest_digest['date']}.md"

            digest_meta = {
                "date": latest_digest["date"],
                "article_count": latest_digest["article_count"],
                "total_words": latest_digest["total_words"],
                "total_chars": len(latest_digest["markdown_text"]),
                "email_count": latest_digest.get("email_count", 0),
                "topics_summary": latest_digest["topics_summary"],
                "segment_counts": seg_counts,
                "download_url": dl_url,
                "locked": has_ep,
            }

    prep = None
    if state.preparation_active and state.preparation_date:
        prep_mp3 = episodes_dir / f"noctua-{state.preparation_date}.prep.mp3"
        has_mp3 = prep_mp3.exists()
        has_digest = state.preparation_digest is not None

        if state.generation_running:
            prep_state = "generating"
        elif state.preparation_error:
            prep_state = "failed"
        elif has_digest and has_mp3:
            prep_state = "audio_uploaded"
        elif has_digest:
            prep_state = "digest_ready"
        else:
            prep_state = "generating"

        existing_episode = database.has_episode(state.preparation_date, db_path=db_path)

        prep = {
            "active": True,
            "generating": state.generation_running,
            "state": prep_state,
            "date": state.preparation_date,
            "existing_episode": existing_episode,
            "error": state.preparation_error,
            "digest": {
                "date": state.preparation_digest.date,
                "article_count": state.preparation_digest.article_count,
                "total_words": state.preparation_digest.total_words,
                "total_chars": len(state.preparation_digest.text),
                "email_count": state.preparation_digest.email_count,
                "topics_summary": state.preparation_digest.topics_summary,
                "segment_counts": state.preparation_digest.segment_counts or {},
                "download_url": f"/api/preparation-digest?show_id={show.show_id}",
            } if has_digest else None,
            "audio": {
                "date": state.preparation_date,
                "audio_url": f"{ep_url_prefix}/noctua-{state.preparation_date}.prep.mp3",
                "file_size_bytes": prep_mp3.stat().st_size if has_mp3 else 0,
            } if has_mp3 else None,
        }

    return JSONResponse({
        "episode": episode_data,
        "digest": digest_meta,
        "preparation": prep,
    })


@app.get("/api/episodes")
async def api_episodes(show_id: str = Query(default="")):
    """Get the full archive of all episodes ever published."""
    state = _resolve_show(show_id)
    episodes = database.list_episodes(db_path=state.show.db_path)
    return JSONResponse({"episodes": episodes, "total": len(episodes)})


@app.get("/api/topic-coverage")
async def api_topic_coverage(
    mode: str = Query("cumulative"),
    published_only: bool = Query(False),
    show_id: str = Query(default=""),
):
    """Radar chart data: target vs actual topic coverage."""
    state = _resolve_show(show_id)
    db_path = state.show.db_path
    fmt = state.show.format

    # Use per-show segment config
    show_segment_order = fmt.segment_order
    duration_map = fmt.segment_durations

    prep_digest_data = None
    if state.preparation_active and state.preparation_digest and not published_only:
        prep_digest_data = {
            "date": state.preparation_digest.date,
            "segment_counts": state.preparation_digest.segment_counts or {},
            "segment_sources": state.preparation_digest.segment_sources or {},
        }

    if mode == "latest" and prep_digest_data:
        digests = [prep_digest_data]
    else:
        limit = 1 if mode == "latest" else 30
        digests = database.get_topic_coverage(limit=limit, published_only=published_only, db_path=db_path)
        if mode == "cumulative" and prep_digest_data:
            digests = [prep_digest_data] + digests

    totals: dict[str, int] = {}
    all_sources: dict[str, set[str]] = {}
    for d in digests:
        for topic_name, count in d["segment_counts"].items():
            totals[topic_name] = totals.get(topic_name, 0) + count
        for topic_name, sources in d.get("segment_sources", {}).items():
            if topic_name not in all_sources:
                all_sources[topic_name] = set()
            all_sources[topic_name].update(sources)
    grand_total = sum(totals.values())
    has_data = grand_total > 0

    num_digests = max(len(digests), 1)
    topics = []
    for name in show_segment_order:
        mins = duration_map.get(name, 1)
        capacity = max(2, round(mins * 1.5))
        actual_articles = totals.get(name, 0)
        if has_data:
            avg_articles = actual_articles / num_digests
            actual_pct = min(avg_articles / capacity, 1.0) * 100
            raw_pct = (avg_articles / capacity) * 100
        else:
            actual_pct = 0
            raw_pct = 0
        label = f"{name} ({mins}m)"
        topics.append({
            "name": label,
            "target_pct": 100,
            "actual_pct": round(actual_pct, 1),
            "raw_pct": round(raw_pct, 1),
            "actual_articles": actual_articles,
            "allocated_min": mins,
            "capacity": capacity,
        })

    suggestions = []
    if has_data:
        for topic_name, t in zip(show_segment_order, topics):
            topic_sources = sorted(all_sources.get(topic_name, []))
            if t["actual_pct"] < 30:
                suggestions.append({
                    "topic": t["name"],
                    "action": "subscribe",
                    "reason": f"Only {t['actual_pct']:.0f}% filled — consider adding sources",
                })
            elif t["raw_pct"] > 200:
                src_list = ", ".join(topic_sources) if topic_sources else "unknown"
                suggestions.append({
                    "topic": t["name"],
                    "action": "unsubscribe",
                    "reason": f"{t['raw_pct']:.0f}% incoming vs capacity — content being discarded. Sources: {src_list}",
                })

    return JSONResponse({
        "topics": topics,
        "suggestions": suggestions,
        "digests_analyzed": len(digests),
        "total_articles": sum(totals.values()),
        "has_data": has_data,
        "mode": mode,
    })


@app.get("/api/history")
async def api_history(show_id: str = Query(default="")):
    """Combined digest + episode history for the History tab."""
    state = _resolve_show(show_id)
    show = state.show
    db_path = show.db_path
    episodes_dir = show.episodes_dir
    is_legacy = show.output_dir == Path("output")

    digests = database.list_digests(limit=100, db_path=db_path)
    episodes_list = database.list_episodes(db_path=db_path)
    ep_by_date = {ep["date"]: ep for ep in episodes_list}

    rows = []
    for d in digests:
        ep = ep_by_date.get(d["date"])
        full = database.get_digest(d["date"], db_path=db_path)
        gcs_url = ep.get("gcs_url", "") if ep else ""
        local_file = episodes_dir / f"noctua-{d['date']}.mp3"
        has_audio = bool(gcs_url) or local_file.exists()
        rows.append({
            "date": d["date"],
            "article_count": d["article_count"],
            "total_words": d["total_words"],
            "total_chars": len(full["markdown_text"]) if full else 0,
            "email_count": d.get("email_count", 0),
            "topics_summary": d["topics_summary"],
            "has_digest": True,
            "has_audio": has_audio,
            "duration_formatted": ep["duration_formatted"] if ep else None,
            "file_size_bytes": ep["file_size_bytes"] if ep else None,
            "rss_summary": ep.get("rss_summary", "") if ep else "",
            "gcs_url": gcs_url,
        })

    return JSONResponse({"rows": rows, "total": len(rows)})


@app.get("/api/export-episodes")
async def api_export_episodes(show_id: str = Query(default="")):
    """Bundle the current PST week's episode MP3s into a ZIP for download."""
    state = _resolve_show(show_id)
    show = state.show
    episodes_dir = show.episodes_dir
    exports_dir = show.exports_dir

    if not episodes_dir.exists():
        return JSONResponse({"error": "No episodes directory found."}, status_code=404)

    now = _pst_now()
    mon, sun = _week_date_range(now)
    week_label = _iso_week_label(now)

    exports_dir.mkdir(parents=True, exist_ok=True)
    zip_name = f"{show.show_id}-{week_label}.zip"
    zip_path = exports_dir / zip_name

    if zip_path.exists():
        return FileResponse(
            zip_path,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{zip_name}"'},
        )

    mp3_files = sorted(
        mp3 for mp3 in episodes_dir.glob("noctua-*.mp3")
        if mon <= mp3.stem.removeprefix("noctua-") <= sun
    )
    if not mp3_files:
        return JSONResponse({"error": "No episodes this week."}, status_code=404)

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
        for mp3 in mp3_files:
            zf.write(mp3, mp3.name)
        _add_digests_to_zip(zf, mon, sun, db_path=show.db_path)

    return FileResponse(
        zip_path,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_name}"'},
    )


@app.get("/api/export-weeks")
async def api_export_weeks(show_id: str = Query(default="")):
    """List all pending (un-downloaded) weekly ZIPs."""
    state = _resolve_show(show_id)
    exports_dir = state.show.exports_dir
    prefix = state.show.show_id

    if not exports_dir.exists():
        return JSONResponse([])
    zips = sorted(exports_dir.glob(f"{prefix}-W*.zip"))
    result = []
    for z in zips:
        label = z.stem.removeprefix(f"{prefix}-")
        stat = z.stat()
        created = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat()
        result.append({
            "filename": z.name,
            "size_bytes": stat.st_size,
            "week_label": label,
            "created_at": created,
        })
    return JSONResponse(result)


@app.get("/api/download-export/{filename}")
async def api_download_export(filename: str, show_id: str = Query(default="")):
    """Download a specific weekly ZIP and delete it afterward."""
    if ".." in filename or "/" in filename:
        return Response(content="Invalid filename.", status_code=400)

    state = _resolve_show(show_id)
    zip_path = state.show.exports_dir / filename
    if not zip_path.exists():
        return JSONResponse({"error": "Export not found."}, status_code=404)

    content = zip_path.read_bytes()
    zip_path.unlink()
    logger.info("Served and deleted export: %s", filename)

    return Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --- Digest downloads ---

@app.get("/digests/{date}.md")
async def digest_download(date: str, show_id: str = Query(default="")) -> Response:
    """Serve a digest as a downloadable .md file (legacy route)."""
    if ".." in date or "/" in date:
        return Response(content="Invalid date.", status_code=400)
    state = _resolve_show(show_id)
    digest = database.get_digest(date, db_path=state.show.db_path)
    if not digest:
        return Response(content="Digest not found.", status_code=404)
    return Response(
        content=digest["markdown_text"],
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="noctua-digest-{date}.md"'},
    )


@app.get("/{show_id}/digests/{date}.md")
async def show_digest_download(show_id: str, date: str) -> Response:
    """Serve a show-specific digest as a downloadable .md file."""
    if ".." in date or "/" in date:
        return Response(content="Invalid date.", status_code=400)
    if show_id not in _show_states:
        return Response(content="Show not found.", status_code=404)
    state = _show_states[show_id]
    digest = database.get_digest(date, db_path=state.show.db_path)
    if not digest:
        return Response(content="Digest not found.", status_code=404)
    return Response(
        content=digest["markdown_text"],
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="noctua-digest-{date}.md"'},
    )


# --- Feed & Episodes ---

@app.get("/feed.xml")
async def feed() -> Response:
    """Serve the RSS podcast feed (default show, backward compat)."""
    state = _resolve_show("")
    feed_path = state.show.feed_path
    if not feed_path.exists():
        return Response(content="Feed not yet generated.", status_code=404)
    return FileResponse(feed_path, media_type="application/rss+xml")


@app.get("/{show_id}/feed.xml")
async def show_feed(show_id: str) -> Response:
    """Serve a show-specific RSS podcast feed."""
    if show_id not in _show_states:
        return Response(content="Show not found.", status_code=404)
    state = _show_states[show_id]
    feed_path = state.show.feed_path
    if not feed_path.exists():
        return Response(content="Feed not yet generated.", status_code=404)
    return FileResponse(feed_path, media_type="application/rss+xml")


@app.get("/episodes/{filename}")
async def episode(filename: str, request: Request) -> Response:
    """Serve an episode MP3 file (default show, backward compat)."""
    state = _resolve_show("")
    return _serve_episode(state.show.episodes_dir, filename, request)


@app.get("/{show_id}/episodes/{filename}")
async def show_episode(show_id: str, filename: str, request: Request) -> Response:
    """Serve a show-specific episode MP3 file."""
    if show_id not in _show_states:
        return Response(content="Show not found.", status_code=404)
    state = _show_states[show_id]
    return _serve_episode(state.show.episodes_dir, filename, request)


def _serve_episode(episodes_dir: Path, filename: str, request: Request) -> Response:
    """Serve an episode MP3 file with range request support."""
    file_path = episodes_dir / filename

    if ".." in filename or "/" in filename:
        return Response(content="Invalid filename.", status_code=400)

    if not file_path.exists():
        return Response(content="Episode not found.", status_code=404)

    file_size = file_path.stat().st_size
    range_header = request.headers.get("range")

    if range_header:
        range_str = range_header.replace("bytes=", "")
        parts = range_str.split("-")
        start = int(parts[0]) if parts[0] else 0
        end = int(parts[1]) if parts[1] else file_size - 1

        if start >= file_size:
            return Response(
                content="Range not satisfiable",
                status_code=416,
                headers={"Content-Range": f"bytes */{file_size}"},
            )

        end = min(end, file_size - 1)
        content_length = end - start + 1

        with open(file_path, "rb") as f:
            f.seek(start)
            data = f.read(content_length)

        return Response(
            content=data,
            status_code=206,
            media_type="audio/mpeg",
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(content_length),
            },
        )

    return FileResponse(
        file_path,
        media_type="audio/mpeg",
        headers={"Accept-Ranges": "bytes"},
    )


# --- Generation & Preparation ---

@app.post("/api/generate")
async def api_generate(show_id: str = Query(default="")):
    """Manually trigger digest preparation."""
    state = _resolve_show(show_id)

    if state.generation_lock.locked():
        return JSONResponse(
            {"status": "already_running", "message": "Digest preparation is already in progress."},
            status_code=409,
        )

    asyncio.create_task(_run_generation(state))
    return JSONResponse({"status": "started", "message": "Digest preparation started."})


@app.get("/api/cron/generate")
async def api_cron_generate(secret: str = Query(""), show_id: str = Query(default="")):
    """External cron trigger for daily digest generation.

    When show_id is omitted, triggers all shows. When provided, triggers only that show.
    """
    if not settings.cron_secret:
        return JSONResponse(
            {"error": "CRON_SECRET not configured on server."},
            status_code=500,
        )
    if secret != settings.cron_secret:
        return JSONResponse({"error": "Invalid secret."}, status_code=403)

    if show_id:
        if show_id not in _show_states:
            return JSONResponse({"error": f"Unknown show: {show_id}"}, status_code=404)
        state = _show_states[show_id]
        if state.generation_lock.locked():
            return JSONResponse(
                {"status": "already_running", "message": f"Generation already in progress for {show_id}."},
                status_code=409,
            )
        logger.info("Cron trigger: starting generation for %s.", show_id)
        asyncio.create_task(_run_generation(state))
        return JSONResponse({"status": "started", "message": f"Generation started for {show_id} via cron."})

    # Trigger all shows
    started = []
    skipped = []
    for sid, state in _show_states.items():
        if state.generation_lock.locked():
            skipped.append(sid)
        else:
            asyncio.create_task(_run_generation(state))
            started.append(sid)

    logger.info("Cron trigger: started=%s, skipped=%s", started, skipped)
    return JSONResponse({
        "status": "started",
        "message": f"Generation started via cron.",
        "started": started,
        "skipped": skipped,
    })


@app.post("/api/start-preparation")
async def api_start_preparation(show_id: str = Query(default="")):
    """Start the preparation workflow: generate a new digest."""
    state = _resolve_show(show_id)

    today_str = datetime.now(PST).strftime("%Y-%m-%d")

    state.preparation_cancelled = False
    state.preparation_active = True
    state.preparation_date = today_str
    state.preparation_digest = None
    state.preparation_error = None

    if state.generation_lock.locked():
        return JSONResponse({
            "state": "generating",
            "date": today_str,
            "message": "Generation already in progress.",
        })

    asyncio.create_task(_run_generation(state))
    return JSONResponse({
        "state": "generating",
        "date": today_str,
        "message": "Digest preparation started.",
    })


@app.get("/api/preparation-digest")
async def api_preparation_digest(show_id: str = Query(default="")):
    """Serve the in-memory preparation digest as a downloadable .md file."""
    state = _resolve_show(show_id)
    if not state.preparation_digest:
        return Response(content="No preparation digest available.", status_code=404)
    return Response(
        content=state.preparation_digest.text,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="noctua-digest-{state.preparation_digest.date}.md"'},
    )


@app.post("/api/publish-episode")
async def api_publish_episode(date: str = Form(""), show_id: str = Form("")):
    """Publish a prepared episode to RSS and archive."""
    state = _resolve_show(show_id)
    show = state.show

    if not date or not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return JSONResponse({"error": "Invalid date format."}, status_code=400)

    if not state.preparation_digest or state.preparation_digest.date != date:
        return JSONResponse({"error": "No preparation digest available for this date."}, status_code=404)

    prep_mp3 = show.episodes_dir / f"noctua-{date}.prep.mp3"
    if not prep_mp3.exists():
        return JSONResponse({"error": f"No uploaded audio found for {date}."}, status_code=404)

    mp3_path = show.episodes_dir / f"noctua-{date}.mp3"
    prep_mp3.rename(mp3_path)

    digest = state.preparation_digest
    database.save_digest(
        date=digest.date,
        markdown_text=digest.text,
        article_count=digest.article_count,
        total_words=digest.total_words,
        topics_summary=digest.topics_summary,
        rss_summary=digest.rss_summary,
        segment_counts=digest.segment_counts,
        segment_sources=digest.segment_sources,
        force=True,
        db_path=show.db_path,
    )

    try:
        metadata = episode_manager.process(mp3_path, digest.topics_summary, digest.rss_summary, show=show)
    except Exception as e:
        return JSONResponse(
            {"error": f"Episode processing failed: {e}"},
            status_code=422,
        )

    try:
        feed_builder.add_episode(metadata, show=show)
    except Exception as e:
        return JSONResponse(
            {"error": f"Feed update failed: {e}"},
            status_code=500,
        )

    state.preparation_active = False
    state.preparation_digest = None

    # Determine feed URL
    is_legacy = show.output_dir == Path("output")
    feed_url = f"{settings.base_url}/feed.xml" if is_legacy else f"{settings.base_url}/{show.show_id}/feed.xml"

    return JSONResponse({
        "status": "ok",
        "message": f"Episode for {date} published to RSS.",
        "feed_url": feed_url,
        "episode": {
            "date": metadata.date,
            "duration_formatted": metadata.duration_formatted,
            "file_size_bytes": metadata.file_size_bytes,
            "topics_summary": metadata.topics_summary,
            "gcs_url": metadata.gcs_url,
        },
    })


@app.post("/api/bump-revision")
async def api_bump_revision(date: str = Form(""), show_id: str = Form("")):
    """Bump the revision for an episode."""
    state = _resolve_show(show_id)
    if not date or not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return JSONResponse({"error": "Invalid date format."}, status_code=400)
    new_rev = feed_builder.bump_revision(date, show=state.show)
    return JSONResponse({"status": "ok", "date": date, "revision": new_rev})


@app.post("/api/cancel-preparation")
async def api_cancel_preparation(show_id: str = Query(default="")):
    """Cancel the preparation workflow."""
    state = _resolve_show(show_id)

    if state.generation_running:
        state.preparation_cancelled = True
        logger.info("[%s] Preparation cancel requested.", state.show.show_id)

    if state.preparation_date:
        prep_mp3 = state.show.episodes_dir / f"noctua-{state.preparation_date}.prep.mp3"
        prep_mp3.unlink(missing_ok=True)

    state.preparation_active = False
    state.preparation_date = None
    state.preparation_digest = None
    state.preparation_error = None

    return JSONResponse({"status": "ok", "message": "Preparation cancelled."})


@app.post("/api/upload-episode")
async def api_upload_episode(file: UploadFile, date: str = Form(""), show_id: str = Form("")):
    """Upload audio for a given digest date (preview only, no publishing)."""
    state = _resolve_show(show_id)
    show = state.show
    episodes_dir = show.episodes_dir
    is_legacy = show.output_dir == Path("output")
    ep_url_prefix = "/episodes" if is_legacy else f"/{show.show_id}/episodes"

    if not date or not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return JSONResponse(
            {"error": "Invalid date format. Use YYYY-MM-DD."},
            status_code=400,
        )

    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return JSONResponse(
            {"error": "Invalid date."},
            status_code=400,
        )

    has_digest = (state.preparation_digest and state.preparation_digest.date == date) or \
                 database.get_digest(date, db_path=show.db_path) is not None
    if not has_digest:
        return JSONResponse(
            {"error": f"No digest found for {date}. Prepare a digest first."},
            status_code=404,
        )

    if not file.filename:
        return JSONResponse({"error": "No file provided."}, status_code=400)
    ext = Path(file.filename).suffix.lower()
    if ext not in ACCEPTED_AUDIO_EXTENSIONS:
        return JSONResponse(
            {"error": f"Unsupported format '{ext}'. Accepted: {', '.join(sorted(ACCEPTED_AUDIO_EXTENSIONS))}"},
            status_code=400,
        )

    episodes_dir.mkdir(parents=True, exist_ok=True)
    mp3_path = episodes_dir / f"noctua-{date}.prep.mp3"
    upload_path = episodes_dir / f"noctua-{date}.prep{ext}"
    try:
        contents = await file.read()
        if len(contents) == 0:
            return JSONResponse(
                {"error": "Uploaded file is empty."},
                status_code=400,
            )
        upload_path.write_bytes(contents)
    except Exception as e:
        return JSONResponse(
            {"error": f"Failed to save file: {e}"},
            status_code=500,
        )

    if ext != ".mp3":
        try:
            logger.info("Converting %s (%d bytes) to MP3...", upload_path.name, upload_path.stat().st_size)
            result = subprocess.run(
                [_ffmpeg_path(), "-i", str(upload_path), "-codec:a", "libmp3lame", "-qscale:a", "2", "-y", str(mp3_path)],
                capture_output=True, text=True, timeout=300,
            )
            upload_path.unlink(missing_ok=True)
            if result.returncode != 0:
                logger.error("ffmpeg failed (exit %d): %s", result.returncode, result.stderr[:500])
                mp3_path.unlink(missing_ok=True)
                return JSONResponse(
                    {"error": f"Audio conversion failed: {result.stderr[:300]}"},
                    status_code=422,
                )
            logger.info("Converted %s to MP3 (%d bytes)", ext, mp3_path.stat().st_size)
        except subprocess.TimeoutExpired:
            upload_path.unlink(missing_ok=True)
            mp3_path.unlink(missing_ok=True)
            return JSONResponse(
                {"error": "Audio conversion timed out (file may be too large)."},
                status_code=422,
            )
        except FileNotFoundError:
            ffpath = _ffmpeg_path()
            logger.error("ffmpeg not found. Resolved path: %s, which: %s", ffpath, shutil.which("ffmpeg"))
            upload_path.unlink(missing_ok=True)
            return JSONResponse(
                {"error": f"ffmpeg not found (path={ffpath}). Cannot convert audio."},
                status_code=500,
            )

    try:
        from src.episode_manager import _ensure_mp3, _format_duration
        mp3_path = _ensure_mp3(mp3_path)
        from mutagen.mp3 import MP3
        audio = MP3(str(mp3_path))
        duration_seconds = int(audio.info.length)
        duration_formatted = _format_duration(duration_seconds)
        file_size_bytes = mp3_path.stat().st_size
    except Exception as e:
        logger.error("Audio validation failed for %s: %s", mp3_path.name, e)
        mp3_path.unlink(missing_ok=True)
        return JSONResponse(
            {"error": f"Audio validation failed: {e}"},
            status_code=422,
        )

    return JSONResponse({
        "status": "ok",
        "message": f"Audio for {date} uploaded. Preview ready — publish when ready.",
        "episode": {
            "date": date,
            "duration_formatted": duration_formatted,
            "duration_seconds": duration_seconds,
            "file_size_bytes": file_size_bytes,
            "audio_url": f"{ep_url_prefix}/noctua-{date}.prep.mp3",
        },
    })


@app.get("/health")
async def health() -> dict:
    """Health check endpoint."""
    any_running = any(s.generation_running for s in _show_states.values())
    return {
        "status": "ok",
        "generation_running": any_running,
        "next_scheduled_run": _next_scheduled_run.isoformat() if _next_scheduled_run else None,
        "generation_schedule_utc": f"{settings.generation_hour:02d}:{settings.generation_minute:02d}",
        "shows": list(_show_states.keys()),
    }


@app.get("/health/detail")
async def health_detail() -> dict:
    """Detailed health check with file system and database stats."""
    show_details = {}
    for sid, state in _show_states.items():
        show = state.show
        ep_count = len(list(show.episodes_dir.glob("noctua-*.mp3"))) if show.episodes_dir.exists() else 0
        show_details[sid] = {
            "episodes": ep_count,
            "digests": len(database.list_digests(db_path=show.db_path)),
            "feed_exists": show.feed_path.exists(),
            "generation_running": state.generation_running,
        }

    return {
        "status": "ok",
        "shows": show_details,
        "next_scheduled_run": _next_scheduled_run.isoformat() if _next_scheduled_run else None,
        "generation_schedule_utc": f"{settings.generation_hour:02d}:{settings.generation_minute:02d}",
        "ffmpeg": _ffmpeg_path(),
        "ffmpeg_available": shutil.which("ffmpeg") is not None,
    }


# --- Dashboard HTML ---

def _build_dashboard_html(show_id: str = "") -> str:
    """Build the dashboard HTML with the given show_id baked in."""
    # Determine show title and tagline for the header
    if show_id and show_id in _show_states:
        show = _show_states[show_id].show
    else:
        show = _resolve_show("").show
        show_id = show.show_id
    title = show.podcast_title
    tagline = show.podcast_description

    return (DASHBOARD_HTML
            .replace("__SHOW_ID__", show_id)
            .replace("__SHOW_TITLE__", title)
            .replace("__SHOW_TAGLINE__", tagline))


DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/png" href="/static/favicon.png">
<title>__SHOW_TITLE__</title>
<style>
  :root {
    --bg: #0f1117;
    --surface: #1a1d27;
    --surface2: #242734;
    --border: #2e3140;
    --text: #e4e4e7;
    --text-dim: #8b8d98;
    --accent: #c4a052;
    --accent-dim: #a08438;
    --green: #4ade80;
    --red: #f87171;
    --yellow: #fbbf24;
    --blue: #60a5fa;
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    font-family: 'SF Mono', 'Cascadia Code', 'Fira Code', 'Consolas', monospace;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
  }

  /* Header */
  header {
    border-bottom: 1px solid var(--border);
    padding: 12px 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  header h1 { font-size: 18px; font-weight: 600; color: var(--accent); letter-spacing: 2px; }
  header .tagline { font-size: 11px; color: var(--text-dim); font-style: italic; }
  header .actions { display: flex; align-items: center; gap: 8px; }

  .btn {
    font-size: 12px; color: var(--accent); text-decoration: none;
    border: 1px solid var(--accent-dim); padding: 4px 12px; border-radius: 4px;
    cursor: pointer; background: transparent; font-family: inherit;
  }
  .btn:hover { background: var(--accent-dim); color: var(--bg); }
  .btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .btn:disabled:hover { background: transparent; color: var(--accent); }

  /* Tabs */
  .tab-bar { display: flex; border-bottom: 1px solid var(--border); padding: 0 24px; }
  .tab-btn {
    font-family: inherit; font-size: 12px; font-weight: 500; color: var(--text-dim);
    background: none; border: none; padding: 10px 20px; cursor: pointer;
    border-bottom: 2px solid transparent; letter-spacing: 1px; text-transform: uppercase;
  }
  .tab-btn:hover { color: var(--text); }
  .tab-btn.active { color: var(--accent); border-bottom-color: var(--accent); }
  .tab-content { display: none; }
  .tab-content.active { display: block; }

  /* Latest: two-column */
  .latest-layout { display: flex; gap: 24px; max-width: 1200px; margin: 0 auto; padding: 24px; align-items: flex-start; }
  .latest-left { flex: 1; min-width: 0; }
  .latest-right { width: 540px; flex-shrink: 0; }

  /* Card */
  .card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px; padding: 24px; margin-bottom: 16px;
  }
  .card-label { font-size: 11px; text-transform: uppercase; letter-spacing: 1.5px; color: var(--accent); margin-bottom: 8px; }

  /* Episode */
  .ep-title { font-size: 20px; font-weight: 600; margin-bottom: 6px; }
  .ep-desc { font-size: 13px; margin-bottom: 8px; line-height: 1.4; }
  .ep-meta { font-size: 11px; color: var(--text-dim); margin-bottom: 16px; display: flex; gap: 12px; flex-wrap: wrap; }
  .card audio { width: 100%; height: 44px; border-radius: 8px; }

  /* Digest */
  .digest-row { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
  .digest-stats { font-size: 12px; color: var(--text-dim); margin-bottom: 4px; }
  .digest-topics { font-size: 11px; color: var(--text-dim); line-height: 1.5; }
  .dl-btn {
    flex-shrink: 0; font-size: 12px; color: var(--bg); background: var(--accent);
    border: none; padding: 8px 18px; border-radius: 6px; cursor: pointer;
    font-family: inherit; font-weight: 500; text-decoration: none; white-space: nowrap;
  }
  .dl-btn:hover { background: var(--accent-dim); }

  /* Upload */
  .upload-row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  .upload-row input[type="file"] { font-family: inherit; font-size: 12px; color: var(--text-dim); flex: 1; min-width: 180px; }
  .up-btn {
    font-size: 12px; color: var(--bg); background: var(--accent); border: none;
    padding: 8px 18px; border-radius: 6px; cursor: pointer; font-family: inherit; font-weight: 500;
  }
  .up-btn:hover { background: var(--accent-dim); }
  .up-btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .upload-status { font-size: 12px; margin-top: 8px; }
  .upload-status.error { color: var(--red); }
  .upload-status.success { color: var(--green); }

  /* Radar */
  .radar-card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 20px; }
  .radar-hdr { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
  .radar-hdr .card-label { margin-bottom: 0; }
  .radar-toggle { display: flex; background: var(--bg); border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
  .radar-toggle button {
    font-family: inherit; font-size: 10px; padding: 4px 10px; border: none;
    background: none; color: var(--text-dim); cursor: pointer;
  }
  .radar-toggle button.active { background: var(--accent-dim); color: var(--bg); }
  .radar-legend { display: flex; gap: 16px; justify-content: center; margin: 10px 0; font-size: 10px; color: var(--text-dim); }
  .radar-legend .sw { display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin-right: 4px; vertical-align: middle; }
  .radar-stats { font-size: 10px; color: var(--text-dim); text-align: center; margin-bottom: 8px; }

  /* Suggestions */
  .sug-label { font-size: 10px; text-transform: uppercase; letter-spacing: 1.5px; color: var(--accent); margin: 12px 0 8px; }
  .sug-item { font-size: 11px; padding: 6px 10px; border-radius: 5px; margin-bottom: 4px; display: flex; align-items: flex-start; gap: 6px; }
  .sug-item.subscribe { background: rgba(74,222,128,0.08); border: 1px solid rgba(74,222,128,0.2); color: var(--green); }
  .sug-item.unsubscribe { background: rgba(248,113,113,0.08); border: 1px solid rgba(248,113,113,0.2); color: var(--red); }
  .sug-item .pill { font-size: 9px; padding: 1px 5px; border-radius: 3px; font-weight: 600; text-transform: uppercase; flex-shrink: 0; margin-top: 1px; }
  .sug-item.subscribe .pill { background: rgba(74,222,128,0.15); }
  .sug-item.unsubscribe .pill { background: rgba(248,113,113,0.15); }
  .no-sug { font-size: 11px; color: var(--text-dim); font-style: italic; }

  /* History */
  .history-wrap { max-width: 1200px; margin: 0 auto; padding: 24px; }
  .htable { width: 100%; border-collapse: collapse; font-size: 12px; }
  .htable th {
    text-align: left; color: var(--text-dim); font-weight: 500; padding: 8px 12px;
    border-bottom: 1px solid var(--border); font-size: 10px; text-transform: uppercase; letter-spacing: 1px; white-space: nowrap;
  }
  .htable td { padding: 10px 12px; border-bottom: 1px solid var(--border); vertical-align: top; }
  .htable tr:hover td { background: var(--surface); }
  .h-date { font-weight: 500; white-space: nowrap; }
  .h-link { text-decoration: none; font-size: 11px; }
  .h-link.digest { color: var(--accent); }
  .h-link.audio { color: var(--blue); }
  .h-link:hover { text-decoration: underline; }
  .h-detail { font-size: 11px; color: var(--text-dim); line-height: 1.5; }
  .h-badge { display: inline-block; font-size: 9px; padding: 1px 5px; border-radius: 3px; font-weight: 600; text-transform: uppercase; }
  .h-badge.yes { background: rgba(74,222,128,0.15); color: var(--green); }
  .h-badge.no { background: rgba(248,113,113,0.08); color: var(--text-dim); }

  /* Empty state */
  .empty { display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 50vh; color: var(--text-dim); text-align: center; padding: 40px; }
  .empty .owl { font-size: 56px; margin-bottom: 20px; opacity: 0.4; }
  .empty p { font-size: 13px; line-height: 1.7; max-width: 400px; }

  ::-webkit-scrollbar { width: 6px; }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

  @media (max-width: 860px) {
    .latest-layout { flex-direction: column; }
    .latest-right { width: 100%; }
  }
</style>
</head>
<body>

<header>
  <div style="display:flex;align-items:center;gap:12px;">
    <div>
      <h1 id="show-title">__SHOW_TITLE__</h1>
      <div class="tagline">__SHOW_TAGLINE__</div>
    </div>
  </div>
  <div class="actions">
    <span style="font-size:10px;color:var(--text-dim);" id="sched-info"></span>
    <button class="btn" id="gen-btn" onclick="startPrep()">Prepare Digest</button>
    <a class="btn" id="feed-link" href="/feed.xml">RSS Feed</a>
  </div>
</header>

<div class="tab-bar">
  <button class="tab-btn active" onclick="switchTab('latest')">Latest</button>
  <button class="tab-btn" onclick="switchTab('history')">History</button>
</div>

<div id="tab-latest" class="tab-content active">
  <div class="latest-layout">
    <div class="latest-left" id="left-col">
      <div class="empty"><div class="owl">&#x1F989;</div><p>Loading...</p></div>
    </div>
    <div class="latest-right" id="right-col"></div>
  </div>
</div>

<div id="tab-history" class="tab-content">
  <div class="latest-layout">
    <div class="latest-left" id="hist-content">
      <div class="empty"><p>Loading history...</p></div>
    </div>
    <div class="latest-right" id="hist-radar"></div>
  </div>
</div>

<script>
const SHOW_ID = '__SHOW_ID__';
const SHOW_TITLE = '__SHOW_TITLE__';
let radarMode = 'latest';
let _showFormat = null; // loaded from API

// Helper to append show_id query parameter
function apiUrl(path, extraParams) {
  const sep = path.includes('?') ? '&' : '?';
  let url = path + sep + 'show_id=' + encodeURIComponent(SHOW_ID);
  if (extraParams) url += '&' + extraParams;
  return url;
}

// Load show format from API
async function loadShowFormat() {
  try {
    const res = await fetch(apiUrl('/api/show-format'));
    _showFormat = await res.json();
  } catch(e) { console.error('Failed to load show format', e); }
  // Set correct RSS feed link per show
  const feedLink = document.getElementById('feed-link');
  if (feedLink) {
    feedLink.href = SHOW_ID === 'hootline' ? '/feed.xml' : '/' + SHOW_ID + '/feed.xml';
  }
}

function switchTab(tab) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
  event.target.classList.add('active');
  document.getElementById('tab-' + tab).classList.add('active');
  if (tab === 'history') loadHistory();
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

// ===== LATEST TAB =====
let _prepActive = false;

function segmentCard() {
  if (!_showFormat) return '';
  const segs = _showFormat.segments;
  const introMin = _showFormat.intro_minutes;
  const outroMin = _showFormat.outro_minutes;
  const isShort = introMin < 1;

  let s = '<div class="card"><div class="card-label">Show Format</div>';
  // Intro
  const introDur = isShort ? '~30s' : '~' + Math.round(introMin) + ' min';
  const introText = isShort
    ? 'Welcome to ' + esc(SHOW_TITLE) + '! Here\\'s what\\'s happening.'
    : 'Welcome to ' + esc(SHOW_TITLE) + ', your daily knowledge briefing. Let\\'s dive in.';
  s += '<div style="font-size:11px;color:var(--text-dim);margin-top:6px;padding:6px 8px;background:rgba(255,255,255,0.03);border-radius:4px;">';
  s += '<span style="color:var(--gold);font-weight:600;">Intro (' + introDur + ')</span><br>' + introText;
  s += '</div>';
  // Segments
  s += '<div style="display:grid;grid-template-columns:1fr auto;gap:2px 12px;font-size:11px;margin-top:8px;">';
  let total = introMin + outroMin;
  for (const seg of segs) {
    total += seg.minutes;
    s += '<span style="color:var(--text);">' + esc(seg.name) + '</span>';
    s += '<span style="color:var(--text-dim);text-align:right;">~' + seg.minutes + ' min</span>';
  }
  s += '</div>';
  // Outro
  const outroDur = isShort ? '~30s' : '~' + Math.round(outroMin) + ' min';
  const outroText = isShort
    ? 'That\\'s your morning briefing from ' + esc(SHOW_TITLE) + '! See you tomorrow.'
    : 'That\\'s all for today\\'s ' + esc(SHOW_TITLE) + '. Thanks for listening — we\\'ll be back tomorrow with more. Until then, stay curious.';
  s += '<div style="font-size:11px;color:var(--text-dim);margin-top:8px;padding:6px 8px;background:rgba(255,255,255,0.03);border-radius:4px;">';
  s += '<span style="color:var(--gold);font-weight:600;">Outro (' + outroDur + ')</span><br>' + outroText;
  s += '</div>';
  // Total
  s += '<div style="display:grid;grid-template-columns:1fr auto;gap:0 12px;font-size:11px;margin-top:8px;border-top:1px solid var(--border);padding-top:6px;">';
  s += '<span style="color:var(--gold);font-weight:600;">Total</span>';
  s += '<span style="color:var(--gold);font-weight:600;text-align:right;">~' + Math.round(total) + ' min</span>';
  s += '</div></div>';
  return s;
}

function topicBreakdown(d) {
  if (!d || !d.segment_counts || !_showFormat) return '';
  const sc = d.segment_counts;
  // Build duration map from show format
  const durMap = {};
  const order = [];
  for (const seg of _showFormat.segments) {
    durMap[seg.name] = seg.minutes;
    order.push(seg.name);
  }
  let rows = '';
  let totalArticles = 0, totalMins = 0;
  for (const topic of order) {
    const count = sc[topic] || 0;
    if (count === 0) continue;
    const mins = durMap[topic] || 1;
    const words = mins * 150;
    totalArticles += count;
    totalMins += mins;
    rows += '<span style="color:var(--text);">' + esc(topic) + '</span>';
    rows += '<span style="color:var(--text-dim);text-align:right;">' + count + '</span>';
    rows += '<span style="color:var(--text-dim);text-align:right;">~' + words + '</span>';
    rows += '<span style="color:var(--text-dim);text-align:right;">~' + mins + ' min</span>';
  }
  if (!rows) return '';
  let s = '<div class="card" style="margin-top:8px;"><div class="card-label">Topic Breakdown</div>';
  s += '<div style="display:grid;grid-template-columns:1fr auto auto auto;gap:2px 12px;font-size:11px;margin-top:6px;">';
  s += '<span style="color:var(--accent);font-weight:600;">Topic</span>';
  s += '<span style="color:var(--accent);font-weight:600;text-align:right;">Articles</span>';
  s += '<span style="color:var(--accent);font-weight:600;text-align:right;">Words</span>';
  s += '<span style="color:var(--accent);font-weight:600;text-align:right;">Duration</span>';
  s += rows;
  s += '</div>';
  s += '<div style="display:grid;grid-template-columns:1fr auto auto auto;gap:0 12px;font-size:11px;margin-top:6px;border-top:1px solid var(--border);padding-top:4px;">';
  s += '<span style="color:var(--gold);font-weight:600;">Total</span>';
  s += '<span style="color:var(--gold);font-weight:600;text-align:right;">' + totalArticles + '</span>';
  s += '<span style="color:var(--gold);font-weight:600;text-align:right;">~' + (totalMins*150) + '</span>';
  s += '<span style="color:var(--gold);font-weight:600;text-align:right;">~' + totalMins + ' min</span>';
  s += '</div></div>';
  return s;
}

async function loadLatest() {
  let res, data;
  try {
    res = await fetch(apiUrl('/api/latest-episode'));
    data = await res.json();
  } catch(e) {
    console.error('loadLatest fetch error:', e);
    document.getElementById('left-col').innerHTML = '<div class="empty"><p>Failed to load. Check console.</p></div>';
    return;
  }
  const left = document.getElementById('left-col');

  // If preparation workflow is active, render that instead
  if (data.preparation && data.preparation.active) {
    _prepActive = true;
    renderPreparation(data.preparation);
    updateGenBtn(null, true);
    loadRadar(radarMode, 'right-col');
    return;
  }

  _prepActive = false;

  if (!data.episode && !data.digest) {
    left.innerHTML = '<div class="empty"><div class="owl">&#x1F989;</div><p>No episodes yet.<br>Click <strong>Prepare Digest</strong> to fetch today\\'s newsletters, then upload audio from NotebookLM.</p></div>';
    document.getElementById('right-col').innerHTML = '';
    updateGenBtn(null, false);
    return;
  }

  let h = '';

  if (data.digest) {
    const d = data.digest;
    h += '<div class="card"><div class="card-label">Today\\'s Digest</div><div class="digest-row"><div>';
    h += '<div class="digest-stats">' + d.article_count + ' articles &middot; ' + (d.email_count||0) + ' emails &middot; ' + d.total_words.toLocaleString() + ' words</div>';
    h += '<div class="digest-topics">' + esc(d.topics_summary||'') + '</div>';
    h += '</div><a class="dl-btn" href="' + d.download_url + '" download>Download .md</a></div></div>';
    h += topicBreakdown(d);
  }

  if (data.episode) {
    const ep = data.episode;
    const dt = new Date(ep.date + 'T00:00:00');
    const dd = dt.toLocaleDateString('en-US', { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' });
    const mb = (ep.file_size_bytes / 1048576).toFixed(1);
    h += '<div class="card"><div class="card-label">Latest Episode</div>';
    h += '<div class="ep-title">' + esc(SHOW_TITLE) + ' &mdash; ' + esc(dd) + '</div>';
    if (ep.rss_summary) h += '<div class="ep-desc">' + esc(ep.rss_summary) + '</div>';
    h += '<div class="ep-meta"><span>' + (ep.duration_formatted||'') + '</span><span>' + mb + ' MB</span><span>' + esc(ep.topics_summary||'') + '</span></div>';
    h += '<audio controls preload="metadata" src="' + ep.audio_url + '"></audio></div>';
  }

  h += segmentCard();
  left.innerHTML = h;
  updateGenBtn(data.digest, false);
  loadRadar(radarMode, 'right-col');
}

function renderPreparation(prep) {
  const left = document.getElementById('left-col');
  let h = '';

  if (prep.state === 'generating') {
    h += '<div class="card"><div class="card-label">Preparing Digest</div>';
    h += '<p style="color:var(--text-dim);font-size:13px;">Processing... fetching newsletters and generating digest.</p>';
    h += '</div>';
    left.innerHTML = h;
    // Poll until generation finishes
    setTimeout(() => loadLatest(), 5000);
    return;
  }

  if (prep.state === 'failed') {
    h += '<div class="card"><div class="card-label" style="color:#e74c3c;">Preparation Failed</div>';
    h += '<p style="color:var(--text-dim);font-size:13px;">' + esc(prep.error || 'Unknown error.') + '</p>';
    h += '<p style="margin-top:8px;font-size:12px;color:var(--text-dim);">Click <strong>Cancel</strong> to return, or try again.</p>';
    h += '</div>';
    left.innerHTML = h;
    loadRadar(radarMode, 'right-col');
    return;
  }

  // Digest card (shared by digest_ready and audio_uploaded)
  if (prep.digest) {
    const d = prep.digest;
    h += '<div class="card"><div class="card-label">New Digest Ready</div><div class="digest-row"><div>';
    h += '<div class="digest-stats">' + d.article_count + ' articles &middot; ' + (d.email_count||0) + ' emails &middot; ' + d.total_words.toLocaleString() + ' words</div>';
    h += '<div class="digest-topics">' + esc(d.topics_summary||'') + '</div>';
    h += '</div><a class="dl-btn" href="' + d.download_url + '" download>Download .md</a></div></div>';
    h += topicBreakdown(d);
  }

  if (prep.state === 'digest_ready') {
    // Upload section
    h += '<div class="card"><div class="card-label">Upload Audio</div>';
    h += '<div class="upload-row"><input type="file" id="mp3-file" accept=".mp3,.m4a,.wav,.ogg,.webm">';
    h += '<button class="up-btn" id="up-btn" onclick="uploadEp(\\'' + prep.date + '\\')">Upload</button></div>';
    h += '<div class="upload-status" id="up-status"></div></div>';
  }

  if (prep.state === 'audio_uploaded') {
    // Audio preview + publish button
    h += '<div class="card"><div class="card-label">Audio Preview</div>';
    if (prep.audio) {
      const mb = (prep.audio.file_size_bytes / 1048576).toFixed(1);
      h += '<div class="ep-meta"><span>' + mb + ' MB</span></div>';
      h += '<audio controls preload="metadata" src="' + prep.audio.audio_url + '"></audio>';
    }
    if (prep.existing_episode) {
      h += '<div style="margin-top:10px;padding:8px 12px;background:rgba(251,191,36,0.08);border:1px solid rgba(251,191,36,0.25);border-radius:6px;font-size:11px;color:var(--yellow);">';
      h += 'This will replace today\\'s existing episode in the RSS feed and History.';
      h += '</div>';
    }
    h += '<div style="margin-top:12px;">';
    h += '<button class="up-btn" onclick="publishEp(\\'' + prep.date + '\\',' + (prep.existing_episode?'true':'false') + ')">Publish to RSS</button>';
    h += '</div></div>';
  }

  h += segmentCard();
  left.innerHTML = h;
}

// ===== RADAR =====
async function loadRadar(mode, containerId) {
  containerId = containerId || 'right-col';
  var canvasId = 'radar-cv-' + containerId;
  if (containerId === 'right-col') radarMode = mode || 'latest';
  mode = mode || 'latest';
  const box = document.getElementById(containerId);
  try {
    let tcUrl = apiUrl('/api/topic-coverage', 'mode=' + mode);
    if (containerId === 'hist-radar') tcUrl += '&published_only=true';
    const res = await fetch(tcUrl);
    const data = await res.json();
    const topics = data.topics;
    if (!topics) { box.innerHTML = ''; return; }

    let h = '<div class="radar-card"><div class="radar-hdr"><div class="card-label">Topic Coverage</div>';
    if (containerId !== 'hist-radar') {
      h += '<div class="radar-toggle">';
      h += '<button class="' + (mode==='latest'?'active':'') + '" onclick="loadRadar(\\'latest\\',\\'' + containerId + '\\')">Latest</button>';
      h += '<button class="' + (mode==='cumulative'?'active':'') + '" onclick="loadRadar(\\'cumulative\\',\\'' + containerId + '\\')">All Time</button>';
      h += '</div>';
    }
    h += '</div>';
    h += '<canvas id="' + canvasId + '" width="500" height="500" style="display:block;margin:0 auto;"></canvas>';
    h += '<div class="radar-legend"><span><span class="sw" style="background:rgba(196,160,82,0.6);"></span>Capacity (100%)</span>';
    h += '<span><span class="sw" style="background:rgba(96,165,250,0.6);"></span>Actual</span></div>';
    const lbl = mode==='latest' ? 'latest digest' : data.digests_analyzed + ' digests';
    h += '<div class="radar-stats">' + lbl + ' &middot; ' + data.total_articles + ' articles' + (!data.has_data?' &middot; no coverage data yet':'') + '</div>';

    if (data.suggestions && data.suggestions.length > 0) {
      const adds = data.suggestions.filter(s => s.action==='subscribe');
      const trims = data.suggestions.filter(s => s.action==='unsubscribe');
      if (adds.length) {
        h += '<div class="sug-label">Add Sources</div>';
        for (const s of adds) {
          h += '<div class="sug-item subscribe"><span class="pill">+ Add</span>';
          h += '<span><strong>' + esc(s.topic) + '</strong> &mdash; ' + esc(s.reason) + '</span></div>';
        }
      }
      if (trims.length) {
        h += '<div class="sug-label">Trim Sources</div>';
        for (const s of trims) {
          h += '<div class="sug-item unsubscribe"><span class="pill">- Trim</span>';
          h += '<span><strong>' + esc(s.topic) + '</strong> &mdash; ' + esc(s.reason) + '</span></div>';
        }
      }
    } else if (data.has_data) {
      h += '<div class="sug-label">Recommendations</div><div class="no-sug">Coverage is well balanced.</div>';
    }

    h += '</div>';
    box.innerHTML = h;
    drawRadar(topics, data.has_data, canvasId);
  } catch (e) {
    console.error('Radar error', e);
    box.innerHTML = '';
  }
}

function drawRadar(topics, hasActual, canvasId) {
  const cv = document.getElementById(canvasId || 'radar-cv-right-col');
  if (!cv) return;
  const ctx = cv.getContext('2d');
  const W = cv.width, H = cv.height;
  const cx = W/2, cy = H/2, R = Math.min(cx,cy)-75, n = topics.length, mx = 100;
  ctx.clearRect(0,0,W,H);

  const ang = i => (Math.PI*2*i/n) - Math.PI/2;
  const pt = (i,p) => { const a=ang(i), r=(Math.min(p,mx)/mx)*R; return [cx+r*Math.cos(a), cy+r*Math.sin(a)]; };

  // Grid rings at 25%, 50%, 75%, 100%
  [25,50,75,100].forEach(r => {
    ctx.beginPath();
    for (let i=0;i<=n;i++) { const [x,y]=pt(i%n,r); i===0?ctx.moveTo(x,y):ctx.lineTo(x,y); }
    ctx.closePath();
    ctx.strokeStyle = r===100 ? '#4a4d5e' : '#2e3140';
    ctx.lineWidth = r===100 ? 1.2 : 0.5;
    ctx.stroke();
    if (r===100) { ctx.font='8px monospace'; ctx.fillStyle='#5a5d6e'; ctx.textAlign='left'; ctx.fillText('100%',cx+3,cy-R-2); }
    if (r===50) { ctx.font='8px monospace'; ctx.fillStyle='#3a3d4e'; ctx.textAlign='left'; ctx.fillText('50%',cx+3,cy-((r/mx)*R)-2); }
  });

  // Spokes
  for (let i=0;i<n;i++) {
    const [x,y]=pt(i,mx);
    ctx.beginPath(); ctx.moveTo(cx,cy); ctx.lineTo(x,y); ctx.strokeStyle='#2e3140'; ctx.lineWidth=0.5; ctx.stroke();
  }

  // Target (gold) — always a circle at 100%
  ctx.beginPath();
  for (let i=0;i<=n;i++) { const [x,y]=pt(i%n,100); i===0?ctx.moveTo(x,y):ctx.lineTo(x,y); }
  ctx.closePath(); ctx.fillStyle='rgba(196,160,82,0.08)'; ctx.fill();
  ctx.strokeStyle='rgba(196,160,82,0.6)'; ctx.lineWidth=2; ctx.stroke();

  // Actual (blue)
  if (hasActual) {
    ctx.beginPath();
    for (let i=0;i<=n;i++) { const [x,y]=pt(i%n,topics[i%n].actual_pct); i===0?ctx.moveTo(x,y):ctx.lineTo(x,y); }
    ctx.closePath(); ctx.fillStyle='rgba(96,165,250,0.12)'; ctx.fill();
    ctx.strokeStyle='rgba(96,165,250,0.7)'; ctx.lineWidth=2; ctx.stroke();
    for (let i=0;i<n;i++) { const [x,y]=pt(i,topics[i].actual_pct); ctx.beginPath(); ctx.arc(x,y,3,0,Math.PI*2); ctx.fillStyle='rgba(96,165,250,0.9)'; ctx.fill(); }
  }

  // Labels (topic name with allocated minutes)
  ctx.font='10px monospace'; ctx.fillStyle='#c8c8cc';
  for (let i=0;i<n;i++) {
    const a=ang(i), lr=R+30, x=cx+lr*Math.cos(a), y=cy+lr*Math.sin(a);
    ctx.textAlign = Math.abs(Math.cos(a))<0.15?'center':Math.cos(a)>0?'left':'right';
    ctx.textBaseline = Math.abs(Math.sin(a))<0.15?'middle':Math.sin(a)>0?'top':'bottom';
    ctx.fillText(topics[i].name,x,y);
  }
}

// ===== HISTORY TAB =====
function getWeekRange() {
  // Compute current PST week (Mon-Sun) using UTC-8
  const now = new Date(Date.now() - 8*3600000);
  const day = now.getUTCDay(); // 0=Sun,...,6=Sat
  const diffToMon = day === 0 ? -6 : 1 - day;
  const mon = new Date(now);
  mon.setUTCDate(mon.getUTCDate() + diffToMon);
  const sun = new Date(mon);
  sun.setUTCDate(sun.getUTCDate() + 6);
  const fmt = d => d.toISOString().slice(0,10);
  return { mon: fmt(mon), sun: fmt(sun) };
}

async function loadHistory() {
  const box = document.getElementById('hist-content');
  try {
    const [histRes, weeksRes] = await Promise.all([
      fetch(apiUrl('/api/history')),
      fetch(apiUrl('/api/export-weeks')),
    ]);
    const data = await histRes.json();
    const pendingWeeks = await weeksRes.json();

    if ((!data.rows || data.rows.length===0) && pendingWeeks.length===0) { box.innerHTML='<div class="empty"><p>No digests yet.</p></div>'; loadRadar('cumulative', 'hist-radar'); return; }

    const week = getWeekRange();
    const thisWeek = (data.rows||[]).filter(r => r.has_audio && r.date >= week.mon && r.date <= week.sun);
    const weekBytes = thisWeek.reduce((s, r) => s + (r.file_size_bytes || 0), 0);
    const weekMB = (weekBytes / 1048576).toFixed(1);

    let h = '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;flex-wrap:wrap;gap:8px;">';
    h += '<span style="font-size:12px;color:var(--text-dim);">' + thisWeek.length + ' episodes this week &middot; ' + weekMB + ' MB</span>';
    if (thisWeek.length > 0) {
      h += '<button class="btn" onclick="window.location=\\'' + apiUrl('/api/export-episodes') + '\\'">Export This Week (ZIP)</button>';
    }
    h += '</div>';

    // Pending exports section
    if (pendingWeeks.length > 0) {
      h += '<div class="card" style="margin-bottom:16px;padding:16px;"><div class="card-label">Pending Exports</div>';
      h += '<div style="display:flex;flex-direction:column;gap:6px;margin-top:8px;">';
      for (const w of pendingWeeks) {
        const mb = (w.size_bytes / 1048576).toFixed(1);
        const ts = w.created_at ? new Date(w.created_at).toLocaleString('en-US',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '';
        h += '<div style="display:flex;align-items:center;justify-content:space-between;">';
        h += '<span style="font-size:12px;color:var(--text);">' + esc(w.week_label) + ' &middot; ' + mb + ' MB';
        if (ts) h += ' &middot; <span style="color:var(--text-dim);">' + esc(ts) + '</span>';
        h += '</span>';
        h += '<a class="btn" href="' + apiUrl('/api/download-export/' + encodeURIComponent(w.filename)) + '">Download</a>';
        h += '</div>';
      }
      h += '</div></div>';
    }

    h += '<table class="htable"><thead><tr><th>Date</th><th>Digest</th><th>Audio</th><th>Digest Details</th><th>Audio Details</th></tr></thead><tbody>';
    for (const r of data.rows) {
      const dt = new Date(r.date+'T00:00:00');
      const dd = dt.toLocaleDateString('en-US',{weekday:'short',month:'short',day:'numeric',year:'numeric'});

      const digestC = r.has_digest
        ? '<a class="h-link digest" href="/digests/'+r.date+'.md?show_id='+encodeURIComponent(SHOW_ID)+'" download>Download</a>'
        : '<span class="h-badge no">none</span>';

      let audioC;
      if (r.has_audio) {
        const url = r.gcs_url || ('/episodes/noctua-'+r.date+'.mp3');
        audioC = '<a class="h-link audio" href="'+url+'" target="_blank">Play</a>';
      } else {
        audioC = '<span class="h-badge no">none</span>';
      }

      const dDetail = r.has_digest
        ? '<span class="h-detail">'+r.article_count+' articles &middot; '+(r.email_count||0)+' emails &middot; '+r.total_words.toLocaleString()+' words</span>'
        : '<span class="h-detail">&mdash;</span>';

      let aDetail = '&mdash;';
      if (r.has_audio) {
        const mb = (r.file_size_bytes/1048576).toFixed(1);
        aDetail = r.duration_formatted+' &middot; '+mb+' MB';
        if (r.rss_summary) aDetail += '<br><span style="color:var(--text);">'+esc(r.rss_summary)+'</span>';
      }

      let topics = '';
      if (r.topics_summary) topics = '<br><span style="color:var(--text-dim);font-size:10px;">'+esc(r.topics_summary)+'</span>';

      h += '<tr><td class="h-date">'+esc(dd)+'</td><td>'+digestC+'</td><td>'+audioC+'</td>';
      h += '<td>'+dDetail+topics+'</td><td class="h-detail">'+aDetail+'</td></tr>';
    }
    h += '</tbody></table>';
    box.innerHTML = h;
    loadRadar('cumulative', 'hist-radar');
  } catch (e) {
    console.error('History error', e);
    box.innerHTML = '<div class="empty"><p>Failed to load history.</p></div>';
  }
}

// ===== ACTIONS =====
let _digestState = null; // tracks current digest state for button logic

function updateGenBtn(digest, prepActive) {
  _digestState = digest;
  const btn = document.getElementById('gen-btn');
  if (!btn) return;
  if (prepActive) {
    btn.textContent = 'Cancel';
    btn.disabled = false;
    btn.onclick = () => cancelPrep();
  } else if (digest) {
    btn.textContent = 'Prepare New Digest';
    btn.disabled = false;
    btn.onclick = () => startPrep();
  } else {
    btn.textContent = 'Prepare Digest';
    btn.disabled = false;
    btn.onclick = () => startPrep();
  }
}

async function startPrep() {
  const btn = document.getElementById('gen-btn');
  _prepActive = true;
  btn.disabled = true; btn.textContent = 'Preparing...';
  try {
    const res = await fetch(apiUrl('/api/start-preparation'), {method:'POST'});
    const data = await res.json();
    if (!res.ok) {
      _prepActive = false;
      btn.disabled = false;
      updateGenBtn(_digestState, false);
      if (data.error) alert(data.error);
      return;
    }
    loadLatest();
  } catch(e) {
    _prepActive = false;
    btn.disabled = false;
    updateGenBtn(_digestState, false);
    alert('Failed to start preparation: ' + e.message);
  }
}

async function cancelPrep() {
  const btn = document.getElementById('gen-btn');
  btn.disabled = true; btn.textContent = 'Cancelling...';
  try {
    await fetch(apiUrl('/api/cancel-preparation'), {method:'POST'});
  } catch(e) {}
  _prepActive = false;
  loadLatest();
}

async function publishEp(date, hasExisting) {
  if (hasExisting) {
    if (!confirm('There is already a published episode for today. Publishing will replace it in the RSS feed and History. Continue?')) return;
  }
  const btn = event.target;
  btn.disabled = true; btn.textContent = 'Publishing...';
  const form = new FormData(); form.append('date', date); form.append('show_id', SHOW_ID);
  try {
    const res = await fetch('/api/publish-episode', {method:'POST', body:form});
    const data = await res.json();
    if (res.ok) {
      _prepActive = false;
      loadLatest();
    } else {
      btn.disabled = false; btn.textContent = 'Publish to RSS';
      alert(data.error || 'Publish failed.');
    }
  } catch(e) {
    btn.disabled = false; btn.textContent = 'Publish to RSS';
  }
}

async function uploadEp(date) {
  const fi = document.getElementById('mp3-file');
  const btn = document.getElementById('up-btn');
  const st = document.getElementById('up-status');
  if (!fi.files.length) { st.className='upload-status error'; st.textContent='Select a file first.'; return; }
  btn.disabled=true; btn.textContent='Uploading...';
  st.className='upload-status'; st.textContent='Uploading and processing...';
  const form = new FormData(); form.append('file',fi.files[0]); form.append('date',date); form.append('show_id',SHOW_ID);
  try {
    const res = await fetch('/api/upload-episode',{method:'POST',body:form});
    const data = await res.json();
    if (res.ok) {
      st.className='upload-status success';
      st.textContent = data.message;
      setTimeout(() => loadLatest(), 1000);
    } else { st.className='upload-status error'; st.textContent=data.error||'Upload failed.'; }
  } catch(e) { st.className='upload-status error'; st.textContent='Network error.'; }
  btn.disabled=false; btn.textContent='Upload';
}

async function updateHealth() {
  try {
    const h = await (await fetch('/health')).json();
    const btn = document.getElementById('gen-btn');
    const info = document.getElementById('sched-info');
    if (!btn.disabled) {
      if (_prepActive && h.generation_running) { btn.disabled=true; btn.textContent='Preparing...'; }
      else if (_prepActive) { updateGenBtn(null, true); }
      else if (_digestState !== null) { updateGenBtn(_digestState, false); }
    }
    if (h.next_scheduled_run) {
      const next = new Date(h.next_scheduled_run);
      info.textContent = 'Next: ' + next.toLocaleString('en-US',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
    }
  } catch(e) {}
}

loadShowFormat().then(() => {
  loadLatest();
  updateHealth();
});
setInterval(updateHealth, 10000);
</script>
</body>
</html>
"""
