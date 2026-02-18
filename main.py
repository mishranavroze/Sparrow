"""FastAPI app — serves RSS feed, audio files, and dashboard."""

import asyncio
import io
import json
import logging
import re
import subprocess
import zipfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Form, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import settings
from src import database, episode_manager, feed_builder

ACCEPTED_AUDIO_EXTENSIONS = {".mp3", ".m4a", ".wav", ".ogg", ".webm"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

EPISODES_DIR = Path("output/episodes")
EPISODES_JSON = Path("output/episodes.json")
FEED_PATH = Path("output/feed.xml")

# --- Generation state ---
_generation_lock = asyncio.Lock()
_generation_running = False
_next_scheduled_run: datetime | None = None


def _calc_next_run() -> datetime:
    """Calculate the next scheduled run time based on generation_hour and generation_minute."""
    now = datetime.now(UTC)
    target = now.replace(hour=settings.generation_hour, minute=settings.generation_minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def _today_digest_exists() -> bool:
    """Check if a digest for today's UTC date already exists."""
    today_str = datetime.now(UTC).strftime("%Y-%m-%d")
    return database.get_digest(today_str) is not None


def _missed_todays_run() -> bool:
    """Return True if the scheduled time already passed today and no digest exists yet."""
    now = datetime.now(UTC)
    target = now.replace(hour=settings.generation_hour, minute=settings.generation_minute, second=0, microsecond=0)
    return now > target and not _today_digest_exists()


async def _run_generation() -> None:
    """Run digest preparation (steps 1-3), guarded by a lock."""
    global _generation_running
    from generate import generate_digest_only

    if _generation_lock.locked():
        logger.warning("Generation already in progress, skipping.")
        return

    async with _generation_lock:
        _generation_running = True
        try:
            await generate_digest_only()
        except Exception as e:
            logger.error("Digest preparation failed: %s", e)
        finally:
            _generation_running = False


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
        logger.info("Scheduler: triggering daily generation.")
        await _run_generation()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the background scheduler and check for missed runs on startup."""
    # Check if we missed today's run (e.g. autoscale spun down during scheduled time)
    if _missed_todays_run():
        logger.info("Startup: missed today's scheduled run — triggering now.")
        asyncio.create_task(_run_generation())

    task = asyncio.create_task(_scheduler())
    logger.info("Background scheduler started (%02d:%02d UTC).", settings.generation_hour, settings.generation_minute)
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="The Hootline", description="Daily podcast generator", lifespan=lifespan)

# Serve static assets (cover image, etc.)
app.mount("/static", StaticFiles(directory="static"), name="static")


# --- Dashboard ---

@app.get("/", response_class=HTMLResponse)
async def dashboard() -> str:
    """Main dashboard showing latest episode with audio player and digest."""
    return DASHBOARD_HTML


@app.get("/api/digests")
async def api_digests():
    """List all digests."""
    return JSONResponse(database.list_digests())


@app.get("/api/digests/{date}")
async def api_digest(date: str):
    """Get a single digest by date."""
    digest = database.get_digest(date)
    if not digest:
        return JSONResponse({"error": "Digest not found"}, status_code=404)
    return JSONResponse(digest)


@app.get("/api/runs")
async def api_runs():
    """List pipeline runs."""
    return JSONResponse(database.list_runs())


@app.get("/api/runs/{run_id}")
async def api_run(run_id: str):
    """Get a single pipeline run."""
    run = database.get_run(run_id)
    if not run:
        return JSONResponse({"error": "Run not found"}, status_code=404)
    return JSONResponse(run)


@app.get("/api/latest-episode")
async def api_latest_episode():
    """Get the latest episode and the latest digest."""
    # Load episodes catalog
    episode_data = None
    if EPISODES_JSON.exists():
        episodes = json.loads(EPISODES_JSON.read_text())
        if episodes:
            # Find the most recent episode that actually has a reachable audio file
            for candidate in sorted(episodes, key=lambda e: e["date"], reverse=True):
                gcs_url = candidate.get("gcs_url", "")
                local_file = EPISODES_DIR / f"noctua-{candidate['date']}.mp3"
                if gcs_url or local_file.exists():
                    audio_url = gcs_url or f"/episodes/noctua-{candidate['date']}.mp3"
                    episode_data = {**candidate, "audio_url": audio_url}
                    break

    # Get the most recent digest (independent of episode date)
    digest_meta = None
    all_digests = database.list_digests(limit=1)
    if all_digests:
        latest_digest = database.get_digest(all_digests[0]["date"])
        if latest_digest:
            digest_meta = {
                "date": latest_digest["date"],
                "article_count": latest_digest["article_count"],
                "total_words": latest_digest["total_words"],
                "total_chars": len(latest_digest["markdown_text"]),
                "topics_summary": latest_digest["topics_summary"],
                "download_url": f"/digests/{latest_digest['date']}.md",
            }

    return JSONResponse({
        "episode": episode_data,
        "digest": digest_meta,
    })


@app.get("/api/episodes")
async def api_episodes():
    """Get the full archive of all episodes ever published."""
    episodes = database.list_episodes()
    return JSONResponse({"episodes": episodes, "total": len(episodes)})


@app.get("/api/topic-coverage")
async def api_topic_coverage(mode: str = Query("cumulative")):
    """Radar chart data: target vs actual topic coverage with suggestions.

    Each topic's target is 100% (its full allocated time). Actual is the
    percentage of that allocation covered, based on article counts.

    Args:
        mode: "cumulative" for all digests, "latest" for most recent only.
    """
    from src.topic_classifier import SEGMENT_DURATIONS, SEGMENT_ORDER

    # Parse allocated minutes per topic
    duration_map = {}
    for topic, dur_str in SEGMENT_DURATIONS.items():
        minutes = int(dur_str.replace("~", "").replace(" minutes", "").replace(" minute", ""))
        duration_map[topic.value] = minutes
    total_minutes = sum(duration_map.values())

    # Actual coverage from recent digests
    limit = 1 if mode == "latest" else 30
    digests = database.get_topic_coverage(limit=limit)
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

    # Per-topic actual as % of allocated time covered:
    # expected_articles = (topic_minutes / total_minutes) * grand_total
    # actual_pct = (actual_articles / expected_articles) * 100
    # Target is always 100% (fully covered)
    topics = []
    for topic in SEGMENT_ORDER:
        name = topic.value
        mins = duration_map.get(name, 1)
        actual_articles = totals.get(name, 0)
        if has_data:
            expected = (mins / total_minutes) * grand_total
            actual_pct = (actual_articles / expected) * 100 if expected > 0 else 0
        else:
            actual_pct = 0
        gap = actual_pct - 100  # positive = over-covered, negative = under-covered
        # Label includes allocated minutes
        label = f"{name} ({mins}m)"
        topics.append({
            "name": label,
            "target_pct": 100,
            "actual_pct": round(actual_pct, 1),
            "actual_articles": actual_articles,
            "allocated_min": mins,
            "gap": round(gap, 1),
        })

    # Suggestions (only when we have actual data)
    suggestions = []
    if has_data:
        for topic_enum, t in zip(SEGMENT_ORDER, topics):
            topic_sources = sorted(all_sources.get(topic_enum.value, []))
            if t["actual_pct"] < 30:
                suggestions.append({
                    "topic": t["name"],
                    "action": "subscribe",
                    "reason": f"Only {t['actual_pct']:.0f}% covered — consider adding sources",
                })
            elif t["actual_pct"] > 200:
                src_list = ", ".join(topic_sources) if topic_sources else "unknown"
                suggestions.append({
                    "topic": t["name"],
                    "action": "unsubscribe",
                    "reason": f"{t['actual_pct']:.0f}% covered — current sources: {src_list}",
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
async def api_history():
    """Combined digest + episode history for the History tab."""
    digests = database.list_digests(limit=100)
    episodes_list = database.list_episodes()
    ep_by_date = {ep["date"]: ep for ep in episodes_list}

    rows = []
    for d in digests:
        ep = ep_by_date.get(d["date"])
        full = database.get_digest(d["date"])
        # Audio is only available if there's a GCS URL or the local file exists
        gcs_url = ep.get("gcs_url", "") if ep else ""
        local_file = EPISODES_DIR / f"noctua-{d['date']}.mp3"
        has_audio = bool(gcs_url) or local_file.exists()
        rows.append({
            "date": d["date"],
            "article_count": d["article_count"],
            "total_words": d["total_words"],
            "total_chars": len(full["markdown_text"]) if full else 0,
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
async def api_export_episodes():
    """Bundle all local episode MP3s into a ZIP for download."""
    if not EPISODES_DIR.exists():
        return JSONResponse({"error": "No episodes directory found."}, status_code=404)

    cutoff = (datetime.now(UTC) - timedelta(days=7)).strftime("%Y-%m-%d")
    mp3_files = sorted(
        mp3 for mp3 in EPISODES_DIR.glob("noctua-*.mp3")
        if mp3.stem.removeprefix("noctua-") >= cutoff
    )
    if not mp3_files:
        return JSONResponse({"error": "No episodes from the last 7 days."}, status_code=404)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        for mp3 in mp3_files:
            zf.write(mp3, mp3.name)
    buf.seek(0)

    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="hootline-episodes.zip"'},
    )


@app.get("/digests/{date}.md")
async def digest_download(date: str) -> Response:
    """Serve a digest as a downloadable .md file."""
    if ".." in date or "/" in date:
        return Response(content="Invalid date.", status_code=400)
    digest = database.get_digest(date)
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
    """Serve the RSS podcast feed."""
    if not FEED_PATH.exists():
        return Response(content="Feed not yet generated.", status_code=404)
    return FileResponse(FEED_PATH, media_type="application/rss+xml")


@app.get("/episodes/{filename}")
async def episode(filename: str, request: Request) -> Response:
    """Serve an episode MP3 file with range request support."""
    file_path = EPISODES_DIR / filename

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


@app.post("/api/generate")
async def api_generate():
    """Manually trigger digest preparation (steps 1-3)."""
    if _generation_lock.locked():
        return JSONResponse(
            {"status": "already_running", "message": "Digest preparation is already in progress."},
            status_code=409,
        )
    asyncio.create_task(_run_generation())
    return JSONResponse({"status": "started", "message": "Digest preparation started."})


@app.get("/api/cron/generate")
async def api_cron_generate(secret: str = Query("")):
    """External cron trigger for daily digest generation.

    Call this from an external cron service (e.g. cron-job.org) at 07:30 UTC.
    Requires the CRON_SECRET query parameter for authentication.
    """
    if not settings.cron_secret:
        return JSONResponse(
            {"error": "CRON_SECRET not configured on server."},
            status_code=500,
        )
    if secret != settings.cron_secret:
        return JSONResponse({"error": "Invalid secret."}, status_code=403)

    if _generation_lock.locked():
        return JSONResponse(
            {"status": "already_running", "message": "Digest preparation is already in progress."},
            status_code=409,
        )

    logger.info("Cron trigger: starting digest generation.")
    asyncio.create_task(_run_generation())
    return JSONResponse({"status": "started", "message": "Digest preparation started via cron."})


@app.post("/api/upload-episode")
async def api_upload_episode(file: UploadFile, date: str = Form("")):
    """Upload an MP3 episode for a given digest date."""
    # Validate date format
    if not date or not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return JSONResponse(
            {"error": "Invalid date format. Use YYYY-MM-DD."},
            status_code=400,
        )

    # Validate the date is a real date
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return JSONResponse(
            {"error": "Invalid date."},
            status_code=400,
        )

    # Check digest exists for that date
    digest = database.get_digest(date)
    if not digest:
        return JSONResponse(
            {"error": f"No digest found for {date}. Prepare a digest first."},
            status_code=404,
        )

    # Validate file extension
    if not file.filename:
        return JSONResponse({"error": "No file provided."}, status_code=400)
    ext = Path(file.filename).suffix.lower()
    if ext not in ACCEPTED_AUDIO_EXTENSIONS:
        return JSONResponse(
            {"error": f"Unsupported format '{ext}'. Accepted: {', '.join(sorted(ACCEPTED_AUDIO_EXTENSIONS))}"},
            status_code=400,
        )

    # Save uploaded file
    EPISODES_DIR.mkdir(parents=True, exist_ok=True)
    mp3_path = EPISODES_DIR / f"noctua-{date}.mp3"
    upload_path = EPISODES_DIR / f"noctua-{date}{ext}"
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

    # Convert to MP3 if needed
    if ext != ".mp3":
        try:
            result = subprocess.run(
                ["ffmpeg", "-i", str(upload_path), "-codec:a", "libmp3lame", "-qscale:a", "2", "-y", str(mp3_path)],
                capture_output=True, text=True,
            )
            upload_path.unlink(missing_ok=True)
            if result.returncode != 0:
                mp3_path.unlink(missing_ok=True)
                return JSONResponse(
                    {"error": f"Audio conversion failed: {result.stderr[:300]}"},
                    status_code=422,
                )
            logger.info("Converted %s to MP3", ext)
        except FileNotFoundError:
            upload_path.unlink(missing_ok=True)
            return JSONResponse(
                {"error": "ffmpeg not found. Cannot convert audio."},
                status_code=500,
            )

    # Process episode (validate MP3, extract metadata)
    try:
        topics_summary = digest.get("topics_summary", "")
        rss_summary = digest.get("rss_summary", "")
        metadata = episode_manager.process(mp3_path, topics_summary, rss_summary)
    except Exception as e:
        mp3_path.unlink(missing_ok=True)
        return JSONResponse(
            {"error": f"Episode processing failed: {e}"},
            status_code=422,
        )

    # Publish to RSS feed
    try:
        feed_builder.add_episode(metadata)
    except Exception as e:
        return JSONResponse(
            {"error": f"Feed update failed: {e}"},
            status_code=500,
        )

    return JSONResponse({
        "status": "ok",
        "message": f"Episode for {date} published.",
        "feed_url": f"{settings.base_url}/feed.xml",
        "episode": {
            "date": metadata.date,
            "duration_formatted": metadata.duration_formatted,
            "file_size_bytes": metadata.file_size_bytes,
            "topics_summary": metadata.topics_summary,
            "gcs_url": metadata.gcs_url,
        },
    })


@app.get("/health")
async def health() -> dict:
    """Health check endpoint."""
    episode_count = len(list(EPISODES_DIR.glob("noctua-*.mp3"))) if EPISODES_DIR.exists() else 0
    feed_exists = FEED_PATH.exists()
    digest_count = len(database.list_digests())
    return {
        "status": "ok",
        "episodes": episode_count,
        "digests": digest_count,
        "feed_exists": feed_exists,
        "generation_running": _generation_running,
        "next_scheduled_run": _next_scheduled_run.isoformat() if _next_scheduled_run else None,
        "generation_schedule_utc": f"{settings.generation_hour:02d}:{settings.generation_minute:02d}",
    }


# --- Dashboard HTML ---

DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>The Hootline</title>
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
  <div>
    <h1>THE HOOTLINE</h1>
    <div class="tagline">The owl of Minerva spreads its wings only with the falling of dusk.</div>
  </div>
  <div class="actions">
    <span style="font-size:10px;color:var(--text-dim);" id="sched-info"></span>
    <button class="btn" id="gen-btn" onclick="triggerGen()">Prepare Digest</button>
    <a class="btn" href="/feed.xml">RSS Feed</a>
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
  <div class="history-wrap" id="hist-content">
    <div class="empty"><p>Loading history...</p></div>
  </div>
</div>

<script>
let radarMode = 'cumulative';

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
async function loadLatest() {
  const res = await fetch('/api/latest-episode');
  const data = await res.json();
  const left = document.getElementById('left-col');

  if (!data.episode && !data.digest) {
    left.innerHTML = '<div class="empty"><div class="owl">&#x1F989;</div><p>No episodes yet.<br>Click <strong>Prepare Digest</strong> to fetch today\\'s newsletters, then upload audio from NotebookLM.</p></div>';
    document.getElementById('right-col').innerHTML = '';
    return;
  }

  let h = '';

  if (data.episode) {
    const ep = data.episode;
    const dt = new Date(ep.date + 'T00:00:00');
    const dd = dt.toLocaleDateString('en-US', { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' });
    const mb = (ep.file_size_bytes / 1048576).toFixed(1);
    h += '<div class="card"><div class="card-label">Latest Episode</div>';
    h += '<div class="ep-title">The Hootline &mdash; ' + esc(dd) + '</div>';
    if (ep.rss_summary) h += '<div class="ep-desc">' + esc(ep.rss_summary) + '</div>';
    h += '<div class="ep-meta"><span>' + (ep.duration_formatted||'') + '</span><span>' + mb + ' MB</span><span>' + esc(ep.topics_summary||'') + '</span></div>';
    h += '<audio controls preload="metadata" src="' + ep.audio_url + '"></audio></div>';
  }

  if (data.digest) {
    const d = data.digest;
    h += '<div class="card"><div class="card-label">Today\\'s Digest</div><div class="digest-row"><div>';
    h += '<div class="digest-stats">' + d.article_count + ' articles &middot; ' + d.total_words.toLocaleString() + ' words &middot; ' + d.total_chars.toLocaleString() + ' chars</div>';
    h += '<div class="digest-topics">' + esc(d.topics_summary||'') + '</div>';
    h += '</div><a class="dl-btn" href="' + d.download_url + '" download>Download .md</a></div></div>';

    h += '<div class="card"><div class="card-label">Upload Episode MP3</div>';
    h += '<div class="upload-row"><input type="file" id="mp3-file" accept=".mp3,.m4a,.wav,.ogg,.webm,audio/*">';
    h += '<button class="up-btn" id="up-btn" onclick="uploadEp(\\'' + esc(d.date) + '\\')">Upload</button></div>';
    h += '<div class="upload-status" id="up-status"></div></div>';
  }

  left.innerHTML = h;
  loadRadar(radarMode);
}

// ===== RADAR =====
async function loadRadar(mode) {
  radarMode = mode || 'cumulative';
  const box = document.getElementById('right-col');
  try {
    const res = await fetch('/api/topic-coverage?mode=' + radarMode);
    const data = await res.json();
    const topics = data.topics;
    if (!topics) { box.innerHTML = ''; return; }

    let h = '<div class="radar-card"><div class="radar-hdr"><div class="card-label">Topic Coverage</div>';
    h += '<div class="radar-toggle">';
    h += '<button class="' + (radarMode==='cumulative'?'active':'') + '" onclick="loadRadar(\\'cumulative\\')">All Time</button>';
    h += '<button class="' + (radarMode==='latest'?'active':'') + '" onclick="loadRadar(\\'latest\\')">Latest</button>';
    h += '</div></div>';
    h += '<canvas id="radar-cv" width="500" height="500" style="display:block;margin:0 auto;"></canvas>';
    h += '<div class="radar-legend"><span><span class="sw" style="background:rgba(196,160,82,0.6);"></span>Target (100%)</span>';
    h += '<span><span class="sw" style="background:rgba(96,165,250,0.6);"></span>Actual</span></div>';
    const lbl = radarMode==='latest' ? 'latest digest' : data.digests_analyzed + ' digests';
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
    drawRadar(topics, data.has_data);
  } catch (e) {
    console.error('Radar error', e);
    box.innerHTML = '';
  }
}

function drawRadar(topics, hasActual) {
  const cv = document.getElementById('radar-cv');
  if (!cv) return;
  const ctx = cv.getContext('2d');
  const W = cv.width, H = cv.height;
  const cx = W/2, cy = H/2, R = Math.min(cx,cy)-75, n = topics.length, mx = 150;
  ctx.clearRect(0,0,W,H);

  const ang = i => (Math.PI*2*i/n) - Math.PI/2;
  const pt = (i,p) => { const a=ang(i), r=(Math.min(p,mx)/mx)*R; return [cx+r*Math.cos(a), cy+r*Math.sin(a)]; };

  // Grid rings at 50%, 100%, 150%
  [50,100,150].forEach(r => {
    ctx.beginPath();
    for (let i=0;i<=n;i++) { const [x,y]=pt(i%n,r); i===0?ctx.moveTo(x,y):ctx.lineTo(x,y); }
    ctx.closePath();
    ctx.strokeStyle = r===100 ? '#4a4d5e' : '#2e3140';
    ctx.lineWidth = r===100 ? 1.2 : 0.5;
    ctx.stroke();
    // Label the 100% ring
    if (r===100) { ctx.font='8px monospace'; ctx.fillStyle='#5a5d6e'; ctx.textAlign='left'; ctx.fillText('100%',cx+3,cy-((r/mx)*R)-2); }
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
async function loadHistory() {
  const box = document.getElementById('hist-content');
  try {
    const res = await fetch('/api/history');
    const data = await res.json();
    if (!data.rows || data.rows.length===0) { box.innerHTML='<div class="empty"><p>No digests yet.</p></div>'; return; }

    // Export button — last 7 days only
    const cutoff = new Date(Date.now() - 7*86400000).toISOString().slice(0,10);
    const recent = data.rows.filter(r => r.has_audio && r.date >= cutoff);
    const recentBytes = recent.reduce((s, r) => s + (r.file_size_bytes || 0), 0);
    const recentMB = (recentBytes / 1048576).toFixed(1);
    let h = '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;">';
    h += '<span style="font-size:12px;color:var(--text-dim);">' + recent.length + ' episodes last 7 days &middot; ' + recentMB + ' MB</span>';
    if (recent.length > 0) {
      h += '<button class="btn" onclick="window.location=\\'/api/export-episodes\\'">Export Last 7 Days (ZIP)</button>';
    }
    h += '</div>';

    h += '<table class="htable"><thead><tr><th>Date</th><th>Digest</th><th>Audio</th><th>Digest Details</th><th>Audio Details</th></tr></thead><tbody>';
    for (const r of data.rows) {
      const dt = new Date(r.date+'T00:00:00');
      const dd = dt.toLocaleDateString('en-US',{weekday:'short',month:'short',day:'numeric',year:'numeric'});

      const digestC = r.has_digest
        ? '<a class="h-link digest" href="/digests/'+r.date+'.md" download>Download</a>'
        : '<span class="h-badge no">none</span>';

      let audioC;
      if (r.has_audio) {
        const url = r.gcs_url || ('/episodes/noctua-'+r.date+'.mp3');
        audioC = '<a class="h-link audio" href="'+url+'" target="_blank">Play</a>';
      } else {
        audioC = '<span class="h-badge no">none</span>';
      }

      const dDetail = r.has_digest
        ? '<span class="h-detail">'+r.article_count+' articles &middot; '+r.total_words.toLocaleString()+' words &middot; '+r.total_chars.toLocaleString()+' chars</span>'
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
  } catch (e) {
    console.error('History error', e);
    box.innerHTML = '<div class="empty"><p>Failed to load history.</p></div>';
  }
}

// ===== ACTIONS =====
async function triggerGen() {
  const btn = document.getElementById('gen-btn');
  btn.disabled = true; btn.textContent = 'Preparing...';
  try { await fetch('/api/generate',{method:'POST'}); } catch(e) {}
  const poll = setInterval(async () => {
    try {
      const h = await (await fetch('/health')).json();
      if (!h.generation_running) { clearInterval(poll); btn.disabled=false; btn.textContent='Prepare Digest'; loadLatest(); }
    } catch(e) {}
  }, 5000);
}

async function uploadEp(date) {
  const fi = document.getElementById('mp3-file');
  const btn = document.getElementById('up-btn');
  const st = document.getElementById('up-status');
  if (!fi.files.length) { st.className='upload-status error'; st.textContent='Select a file first.'; return; }
  btn.disabled=true; btn.textContent='Uploading...';
  st.className='upload-status'; st.textContent='Uploading and processing...';
  const form = new FormData(); form.append('file',fi.files[0]); form.append('date',date);
  try {
    const res = await fetch('/api/upload-episode',{method:'POST',body:form});
    const data = await res.json();
    if (res.ok) {
      st.className='upload-status success';
      st.innerHTML = data.message + ' <a href="/feed.xml" style="color:var(--accent);">View Feed</a>';
      setTimeout(() => loadLatest(), 2000);
    } else { st.className='upload-status error'; st.textContent=data.error||'Upload failed.'; }
  } catch(e) { st.className='upload-status error'; st.textContent='Network error.'; }
  btn.disabled=false; btn.textContent='Upload';
}

async function updateHealth() {
  try {
    const h = await (await fetch('/health')).json();
    const btn = document.getElementById('gen-btn');
    const info = document.getElementById('sched-info');
    if (h.generation_running) { btn.disabled=true; btn.textContent='Preparing...'; }
    else { btn.disabled=false; btn.textContent='Prepare Digest'; }
    if (h.next_scheduled_run) {
      const next = new Date(h.next_scheduled_run);
      info.textContent = 'Next: ' + next.toLocaleString('en-US',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
    }
  } catch(e) {}
}

loadLatest();
updateHealth();
setInterval(updateHealth, 10000);
</script>
</body>
</html>
"""
