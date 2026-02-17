"""FastAPI app — serves RSS feed, audio files, and dashboard."""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import settings
from src import database

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
    """Calculate the next scheduled run time based on GENERATION_HOUR."""
    now = datetime.now(UTC)
    target = now.replace(hour=settings.generation_hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


async def _run_generation() -> None:
    """Run the generation pipeline, guarded by a lock to prevent concurrent runs."""
    global _generation_running
    from generate import generate_episode

    if _generation_lock.locked():
        logger.warning("Generation already in progress, skipping.")
        return

    async with _generation_lock:
        _generation_running = True
        try:
            await generate_episode()
        except Exception as e:
            logger.error("Generation pipeline failed: %s", e)
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
    logger.info("Background scheduler started (generation_hour=%d UTC).", settings.generation_hour)
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Noctua", description="Daily podcast generator", lifespan=lifespan)

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
    """Get the latest episode with its digest."""
    # Load episodes catalog
    if not EPISODES_JSON.exists():
        return JSONResponse({"episode": None, "digest": None})
    episodes = json.loads(EPISODES_JSON.read_text())
    if not episodes:
        return JSONResponse({"episode": None, "digest": None})

    # Most recent episode
    latest = sorted(episodes, key=lambda e: e["date"], reverse=True)[0]
    date = latest["date"]

    # Try to find matching digest
    digest = database.get_digest(date)

    return JSONResponse({
        "episode": {
            **latest,
            "audio_url": f"/episodes/noctua-{date}.mp3",
        },
        "digest": digest,
    })


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
    """Manually trigger episode generation."""
    if _generation_lock.locked():
        return JSONResponse(
            {"status": "already_running", "message": "Generation is already in progress."},
            status_code=409,
        )
    asyncio.create_task(_run_generation())
    return JSONResponse({"status": "started", "message": "Generation pipeline started."})


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
        "generation_hour_utc": settings.generation_hour,
    }


# --- Dashboard HTML ---

DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Noctua</title>
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

  /* Digest section */
  .digest-section {
    margin-bottom: 32px;
  }

  .digest-section .section-title {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 1.5px;
    color: var(--accent);
    margin-bottom: 16px;
    padding-bottom: 8px;
    border-bottom: 1px solid var(--border);
  }

  /* Markdown */
  .markdown h1 {
    font-size: 18px;
    color: var(--accent);
    margin-bottom: 16px;
    padding-bottom: 8px;
    border-bottom: 1px solid var(--border);
  }

  .markdown h2 {
    font-size: 13px;
    color: var(--blue);
    margin-top: 28px;
    margin-bottom: 4px;
  }

  .markdown h3 {
    font-size: 14px;
    color: var(--text);
    margin-bottom: 6px;
  }

  .markdown p {
    font-size: 13px;
    line-height: 1.75;
    color: var(--text);
    margin-bottom: 12px;
  }

  .markdown hr {
    border: none;
    border-top: 1px solid var(--border);
    margin: 24px 0;
  }

  .markdown a { color: var(--accent); text-decoration: none; }
  .markdown a:hover { text-decoration: underline; }

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
    <h1>NOCTUA</h1>
    <div class="tagline">The owl of Minerva spreads its wings only with the falling of dusk.</div>
  </div>
  <div class="header-actions">
    <div class="scheduler-info" id="scheduler-info"></div>
    <button class="btn" id="generate-btn" onclick="triggerGenerate()">Generate Now</button>
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
          <p>No episodes yet.<br>Click <strong>Generate Now</strong> to create your first episode, or wait for the scheduled run.</p>
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
          <div class="episode-title">Noctua &mdash; ${escapeHtml(displayDate)}</div>
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

    // Digest content
    if (data.digest && data.digest.markdown_text) {
      const d = data.digest;
      html += `
        <div class="digest-section">
          <div class="section-title">Today's Digest &mdash; ${d.article_count} articles, ${d.total_words.toLocaleString()} words</div>
          <div class="markdown">${renderMarkdown(d.markdown_text)}</div>
        </div>`;
    }

    page.innerHTML = html;
  }

  function renderMarkdown(text) {
    return text
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/^### (.+)$/gm, '<h3>$1</h3>')
      .replace(/^## (.+)$/gm, '<h2>$1</h2>')
      .replace(/^# (.+)$/gm, '<h1>$1</h1>')
      .replace(/^---$/gm, '<hr>')
      .replace(/\\[([^\\]]+)\\]\\(([^)]+)\\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
      .split('\\n\\n').map(block => {
        block = block.trim();
        if (!block) return '';
        if (block.startsWith('<h') || block.startsWith('<hr')) return block;
        return '<p>' + block.replace(/\\n/g, '<br>') + '</p>';
      }).join('');
  }

  function escapeHtml(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }

  async function triggerGenerate() {
    const btn = document.getElementById('generate-btn');
    btn.disabled = true;
    btn.textContent = 'Running...';
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
          btn.textContent = 'Generate Now';
          load();
        }
      } catch (e) {}
    }, 5000);
  }

  async function updateHealth() {
    try {
      const h = await (await fetch('/health')).json();
      const btn = document.getElementById('generate-btn');
      const info = document.getElementById('scheduler-info');
      if (h.generation_running) {
        btn.disabled = true;
        btn.textContent = 'Running...';
      } else {
        btn.disabled = false;
        btn.textContent = 'Generate Now';
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
