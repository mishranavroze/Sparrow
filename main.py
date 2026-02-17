"""FastAPI app — serves RSS feed, audio files, and dashboard."""

import asyncio
import json
import logging
import re
import subprocess
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Form, Request, Response, UploadFile
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
    """Background task that triggers generation at the configured hour daily."""
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
    """Start the background scheduler when the app starts."""
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
            latest = sorted(episodes, key=lambda e: e["date"], reverse=True)[0]
            episode_data = {
                **latest,
                "audio_url": f"/episodes/noctua-{latest['date']}.mp3",
            }

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
        metadata = episode_manager.process(mp3_path, topics_summary)
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
        "episode": {
            "date": metadata.date,
            "duration_formatted": metadata.duration_formatted,
            "file_size_bytes": metadata.file_size_bytes,
            "topics_summary": metadata.topics_summary,
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

  header {
    border-bottom: 1px solid var(--border);
    padding: 16px 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  header h1 {
    font-size: 18px;
    font-weight: 600;
    color: var(--accent);
    letter-spacing: 2px;
  }

  header .tagline {
    font-size: 11px;
    color: var(--text-dim);
    font-style: italic;
  }

  header .header-actions {
    display: flex;
    align-items: center;
    gap: 10px;
  }

  .btn {
    font-size: 12px;
    color: var(--accent);
    text-decoration: none;
    border: 1px solid var(--accent-dim);
    padding: 4px 12px;
    border-radius: 4px;
    cursor: pointer;
    background: transparent;
    font-family: inherit;
  }
  .btn:hover { background: var(--accent-dim); color: var(--bg); }
  .btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .btn:disabled:hover { background: transparent; color: var(--accent); }

  .scheduler-info {
    font-size: 10px;
    color: var(--text-dim);
    text-align: right;
  }

  /* Main layout */
  .page {
    max-width: 860px;
    margin: 0 auto;
    padding: 32px 24px;
  }

  /* Episode player card */
  .episode-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 28px;
    margin-bottom: 32px;
  }

  .episode-card .episode-date {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 1.5px;
    color: var(--accent);
    margin-bottom: 8px;
  }

  .episode-card .episode-title {
    font-size: 22px;
    font-weight: 600;
    color: var(--text);
    margin-bottom: 6px;
  }

  .episode-card .episode-meta {
    font-size: 12px;
    color: var(--text-dim);
    margin-bottom: 20px;
    display: flex;
    gap: 16px;
  }

  .episode-card audio {
    width: 100%;
    height: 48px;
    border-radius: 8px;
    outline: none;
  }

  /* Digest download card */
  .digest-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 24px 28px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 20px;
  }

  .digest-card .digest-info {
    flex: 1;
  }

  .digest-card .digest-label {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 1.5px;
    color: var(--accent);
    margin-bottom: 6px;
  }

  .digest-card .digest-stats {
    font-size: 12px;
    color: var(--text-dim);
    margin-bottom: 4px;
  }

  .digest-card .digest-topics {
    font-size: 12px;
    color: var(--text-dim);
    line-height: 1.5;
  }

  .digest-card .download-btn {
    flex-shrink: 0;
    font-size: 12px;
    color: var(--bg);
    background: var(--accent);
    border: none;
    padding: 8px 20px;
    border-radius: 6px;
    cursor: pointer;
    font-family: inherit;
    font-weight: 500;
    text-decoration: none;
    white-space: nowrap;
  }
  .digest-card .download-btn:hover { background: var(--accent-dim); }

  /* Upload section */
  .upload-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 24px 28px;
    margin-top: 16px;
  }

  .upload-card .upload-label {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 1.5px;
    color: var(--accent);
    margin-bottom: 12px;
  }

  .upload-card .upload-row {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
  }

  .upload-card input[type="file"] {
    font-family: inherit;
    font-size: 12px;
    color: var(--text-dim);
    flex: 1;
    min-width: 200px;
  }

  .upload-card .upload-btn {
    font-size: 12px;
    color: var(--bg);
    background: var(--accent);
    border: none;
    padding: 8px 20px;
    border-radius: 6px;
    cursor: pointer;
    font-family: inherit;
    font-weight: 500;
    white-space: nowrap;
  }
  .upload-card .upload-btn:hover { background: var(--accent-dim); }
  .upload-card .upload-btn:disabled { opacity: 0.4; cursor: not-allowed; }

  .upload-card .upload-status {
    font-size: 12px;
    margin-top: 10px;
    line-height: 1.5;
  }
  .upload-card .upload-status.error { color: var(--red); }
  .upload-card .upload-status.success { color: var(--green); }

  /* Empty state */
  .empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    min-height: 60vh;
    color: var(--text-dim);
    text-align: center;
    padding: 40px;
  }

  .empty-state .owl { font-size: 56px; margin-bottom: 20px; opacity: 0.4; }
  .empty-state p { font-size: 13px; line-height: 1.7; max-width: 400px; }

  /* Scrollbar */
  ::-webkit-scrollbar { width: 6px; }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

  @media (max-width: 640px) {
    .page { padding: 20px 16px; }
    .episode-card { padding: 20px; }
    .episode-card .episode-title { font-size: 18px; }
    .episode-card .episode-meta { flex-direction: column; gap: 4px; }
  }
</style>
</head>
<body>

<header>
  <div>
    <h1>THE HOOTLINE</h1>
    <div class="tagline">The owl of Minerva spreads its wings only with the falling of dusk.</div>
  </div>
  <div class="header-actions">
    <div class="scheduler-info" id="scheduler-info"></div>
    <button class="btn" id="generate-btn" onclick="triggerGenerate()">Prepare Digest</button>
    <a class="btn" href="/feed.xml">RSS Feed</a>
  </div>
</header>

<div class="page" id="page">
  <div class="empty-state" id="loading">
    <div class="owl">&#x1F989;</div>
    <p>Loading...</p>
  </div>
</div>

<script>
  async function load() {
    const res = await fetch('/api/latest-episode');
    const data = await res.json();
    const page = document.getElementById('page');

    if (!data.episode && !data.digest) {
      page.innerHTML = `
        <div class="empty-state">
          <div class="owl">&#x1F989;</div>
          <p>No episodes yet.<br>Click <strong>Prepare Digest</strong> to fetch and compile today's newsletters, then upload the audio from NotebookLM.</p>
        </div>`;
      return;
    }

    let html = '';

    // Episode player card
    if (data.episode) {
      const ep = data.episode;
      const dt = new Date(ep.date + 'T00:00:00');
      const displayDate = dt.toLocaleDateString('en-US', { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' });
      const sizeMB = (ep.file_size_bytes / (1024 * 1024)).toFixed(1);

      html += `
        <div class="episode-card">
          <div class="episode-date">Latest Episode</div>
          <div class="episode-title">The Hootline &mdash; ${escapeHtml(displayDate)}</div>
          <div class="episode-meta">
            <span>${ep.duration_formatted || ''}</span>
            <span>${sizeMB} MB</span>
            <span>${escapeHtml(ep.topics_summary || '')}</span>
          </div>
          <audio controls preload="metadata" src="${ep.audio_url}">
            Your browser does not support the audio element.
          </audio>
        </div>`;
    }

    // Digest download card
    if (data.digest) {
      const d = data.digest;
      html += `
        <div class="digest-card">
          <div class="digest-info">
            <div class="digest-label">Today's Digest</div>
            <div class="digest-stats">${d.article_count} articles &middot; ${d.total_words.toLocaleString()} words &middot; ${d.total_chars.toLocaleString()} chars</div>
            <div class="digest-topics">${escapeHtml(d.topics_summary || '')}</div>
          </div>
          <a class="download-btn" href="${d.download_url}" download>Download .md</a>
        </div>`;

      // Upload form — shown when a digest exists
      html += `
        <div class="upload-card">
          <div class="upload-label">Upload Episode MP3</div>
          <div class="upload-row">
            <input type="file" id="mp3-file" accept=".mp3,.m4a,.wav,.ogg,.webm,audio/*">
            <button class="upload-btn" id="upload-btn" onclick="uploadEpisode('${escapeHtml(d.date)}')">Upload Episode</button>
          </div>
          <div class="upload-status" id="upload-status"></div>
        </div>`;
    }

    page.innerHTML = html;
  }

  function escapeHtml(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }

  async function triggerGenerate() {
    const btn = document.getElementById('generate-btn');
    btn.disabled = true;
    btn.textContent = 'Preparing...';
    try {
      await fetch('/api/generate', { method: 'POST' });
    } catch (e) {}
    // Poll for completion
    const poll = setInterval(async () => {
      try {
        const h = await (await fetch('/health')).json();
        if (!h.generation_running) {
          clearInterval(poll);
          btn.disabled = false;
          btn.textContent = 'Prepare Digest';
          load();
        }
      } catch (e) {}
    }, 5000);
  }

  async function uploadEpisode(date) {
    const fileInput = document.getElementById('mp3-file');
    const btn = document.getElementById('upload-btn');
    const status = document.getElementById('upload-status');

    if (!fileInput.files.length) {
      status.className = 'upload-status error';
      status.textContent = 'Please select an MP3 file first.';
      return;
    }

    btn.disabled = true;
    btn.textContent = 'Uploading...';
    status.className = 'upload-status';
    status.textContent = 'Uploading and processing...';

    const form = new FormData();
    form.append('file', fileInput.files[0]);
    form.append('date', date);

    try {
      const res = await fetch('/api/upload-episode', { method: 'POST', body: form });
      const data = await res.json();
      if (res.ok) {
        status.className = 'upload-status success';
        status.textContent = data.message;
        setTimeout(() => load(), 1500);
      } else {
        status.className = 'upload-status error';
        status.textContent = data.error || 'Upload failed.';
      }
    } catch (e) {
      status.className = 'upload-status error';
      status.textContent = 'Network error. Please try again.';
    }

    btn.disabled = false;
    btn.textContent = 'Upload Episode';
  }

  async function updateHealth() {
    try {
      const h = await (await fetch('/health')).json();
      const btn = document.getElementById('generate-btn');
      const info = document.getElementById('scheduler-info');
      if (h.generation_running) {
        btn.disabled = true;
        btn.textContent = 'Preparing...';
      } else {
        btn.disabled = false;
        btn.textContent = 'Prepare Digest';
      }
      if (h.next_scheduled_run) {
        const next = new Date(h.next_scheduled_run);
        info.textContent = 'Next: ' + next.toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
      }
    } catch (e) {}
  }

  load();
  updateHealth();
  setInterval(updateHealth, 10000);
</script>

</body>
</html>
"""
