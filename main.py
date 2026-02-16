"""FastAPI app — serves RSS feed, audio files, and dashboard."""

import logging
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from src import database

app = FastAPI(title="Noctua", description="Daily podcast generator")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

EPISODES_DIR = Path("output/episodes")
FEED_PATH = Path("output/feed.xml")


# --- Dashboard ---

@app.get("/", response_class=HTMLResponse)
async def dashboard() -> str:
    """Main dashboard with digest viewer, pipeline status, and logs."""
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
    }


# --- Dashboard HTML ---

DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Noctua Dashboard</title>
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

  header .feed-link {
    font-size: 12px;
    color: var(--accent);
    text-decoration: none;
    border: 1px solid var(--accent-dim);
    padding: 4px 12px;
    border-radius: 4px;
  }
  header .feed-link:hover { background: var(--accent-dim); color: var(--bg); }

  .container {
    display: grid;
    grid-template-columns: 300px 1fr;
    height: calc(100vh - 57px);
  }

  /* Sidebar */
  .sidebar {
    border-right: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  .tab-bar {
    display: flex;
    border-bottom: 1px solid var(--border);
  }

  .tab {
    flex: 1;
    padding: 10px;
    text-align: center;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 1px;
    cursor: pointer;
    color: var(--text-dim);
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    font-family: inherit;
  }

  .tab.active {
    color: var(--accent);
    border-bottom-color: var(--accent);
  }

  .tab:hover { color: var(--text); }

  .sidebar-content {
    flex: 1;
    overflow-y: auto;
    padding: 8px;
  }

  .digest-item, .run-item {
    padding: 10px 12px;
    border-radius: 6px;
    cursor: pointer;
    margin-bottom: 4px;
    border: 1px solid transparent;
  }

  .digest-item:hover, .run-item:hover {
    background: var(--surface2);
    border-color: var(--border);
  }

  .digest-item.active, .run-item.active {
    background: var(--surface2);
    border-color: var(--accent-dim);
  }

  .digest-date {
    font-size: 13px;
    font-weight: 500;
    color: var(--text);
  }

  .digest-meta {
    font-size: 11px;
    color: var(--text-dim);
    margin-top: 3px;
  }

  .run-time {
    font-size: 12px;
    color: var(--text);
  }

  .run-status {
    font-size: 11px;
    margin-top: 3px;
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .status-dot {
    width: 7px; height: 7px;
    border-radius: 50%;
    display: inline-block;
  }

  .status-dot.success { background: var(--green); }
  .status-dot.failed { background: var(--red); }
  .status-dot.running { background: var(--yellow); animation: pulse 1.5s infinite; }

  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
  }

  /* Main content area */
  .main {
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  .content-header {
    padding: 16px 24px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
  }

  .content-header h2 {
    font-size: 15px;
    font-weight: 500;
    color: var(--text);
  }

  .content-header .meta {
    font-size: 11px;
    color: var(--text-dim);
    margin-top: 4px;
  }

  .content-body {
    flex: 1;
    overflow-y: auto;
    padding: 24px;
  }

  /* Markdown rendering */
  .markdown-content h1 {
    font-size: 20px;
    color: var(--accent);
    margin-bottom: 16px;
    border-bottom: 1px solid var(--border);
    padding-bottom: 8px;
  }

  .markdown-content h2 {
    font-size: 14px;
    color: var(--blue);
    margin-top: 24px;
    margin-bottom: 4px;
  }

  .markdown-content h3 {
    font-size: 15px;
    color: var(--text);
    margin-bottom: 8px;
  }

  .markdown-content p {
    font-size: 13px;
    line-height: 1.7;
    color: var(--text);
    margin-bottom: 12px;
  }

  .markdown-content hr {
    border: none;
    border-top: 1px solid var(--border);
    margin: 20px 0;
  }

  .markdown-content a {
    color: var(--accent);
    text-decoration: none;
  }

  .markdown-content a:hover { text-decoration: underline; }

  /* Pipeline log view */
  .pipeline-steps {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .step-item {
    display: flex;
    align-items: flex-start;
    gap: 12px;
    padding: 12px;
    background: var(--surface);
    border-radius: 6px;
    border-left: 3px solid var(--border);
  }

  .step-item.success { border-left-color: var(--green); }
  .step-item.failed { border-left-color: var(--red); }
  .step-item.running { border-left-color: var(--yellow); }

  .step-icon { font-size: 16px; width: 20px; text-align: center; }

  .step-details { flex: 1; }

  .step-name {
    font-size: 13px;
    font-weight: 500;
    color: var(--text);
  }

  .step-message {
    font-size: 11px;
    color: var(--text-dim);
    margin-top: 3px;
    white-space: pre-wrap;
    word-break: break-word;
  }

  .step-time {
    font-size: 10px;
    color: var(--text-dim);
  }

  .error-box {
    margin-top: 16px;
    padding: 12px;
    background: rgba(248, 113, 113, 0.1);
    border: 1px solid rgba(248, 113, 113, 0.3);
    border-radius: 6px;
    font-size: 12px;
    color: var(--red);
    white-space: pre-wrap;
    word-break: break-word;
  }

  .empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    height: 100%;
    color: var(--text-dim);
    text-align: center;
    padding: 40px;
  }

  .empty-state .owl { font-size: 48px; margin-bottom: 16px; opacity: 0.5; }
  .empty-state p { font-size: 13px; line-height: 1.6; }

  .sidebar-content::-webkit-scrollbar,
  .content-body::-webkit-scrollbar {
    width: 6px;
  }
  .sidebar-content::-webkit-scrollbar-thumb,
  .content-body::-webkit-scrollbar-thumb {
    background: var(--border);
    border-radius: 3px;
  }

  @media (max-width: 768px) {
    .container { grid-template-columns: 1fr; }
    .sidebar { max-height: 40vh; }
  }
</style>
</head>
<body>

<header>
  <div>
    <h1>NOCTUA</h1>
    <div class="tagline">The owl of Minerva spreads its wings only with the falling of dusk.</div>
  </div>
  <a class="feed-link" href="/feed.xml">RSS Feed</a>
</header>

<div class="container">
  <div class="sidebar">
    <div class="tab-bar">
      <button class="tab active" data-tab="digests" onclick="switchTab('digests')">Digests</button>
      <button class="tab" data-tab="runs" onclick="switchTab('runs')">Pipeline Runs</button>
    </div>
    <div class="sidebar-content" id="sidebar-content"></div>
  </div>

  <div class="main">
    <div class="content-header" id="content-header" style="display:none">
      <h2 id="content-title"></h2>
      <div class="meta" id="content-meta"></div>
    </div>
    <div class="content-body" id="content-body">
      <div class="empty-state">
        <div class="owl">&#x1F989;</div>
        <p>Select a digest or pipeline run from the sidebar to view details.</p>
      </div>
    </div>
  </div>
</div>

<script>
  let currentTab = 'digests';
  let digestsCache = [];
  let runsCache = [];

  async function load() {
    const [dRes, rRes] = await Promise.all([
      fetch('/api/digests'),
      fetch('/api/runs')
    ]);
    digestsCache = await dRes.json();
    runsCache = await rRes.json();
    renderSidebar();
  }

  function switchTab(tab) {
    currentTab = tab;
    document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
    renderSidebar();
  }

  function renderSidebar() {
    const el = document.getElementById('sidebar-content');
    if (currentTab === 'digests') {
      if (digestsCache.length === 0) {
        el.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text-dim);font-size:12px;">No digests yet. Run the pipeline to generate your first digest.</div>';
        return;
      }
      el.innerHTML = digestsCache.map(d => `
        <div class="digest-item" onclick="showDigest('${d.date}')">
          <div class="digest-date">${formatDate(d.date)}</div>
          <div class="digest-meta">${d.article_count} articles &middot; ${d.total_words.toLocaleString()} words</div>
        </div>
      `).join('');
    } else {
      if (runsCache.length === 0) {
        el.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text-dim);font-size:12px;">No pipeline runs yet.</div>';
        return;
      }
      el.innerHTML = runsCache.map(r => `
        <div class="run-item" onclick="showRun('${r.run_id}')">
          <div class="run-time">${formatTimestamp(r.started_at)}</div>
          <div class="run-status">
            <span class="status-dot ${r.status}"></span>
            <span style="color:${statusColor(r.status)}">${r.status}</span>
            ${r.current_step ? ' &middot; ' + r.current_step : ''}
          </div>
        </div>
      `).join('');
    }
  }

  async function showDigest(date) {
    document.querySelectorAll('.digest-item').forEach(el => el.classList.remove('active'));
    event.currentTarget.classList.add('active');

    const res = await fetch(`/api/digests/${date}`);
    const d = await res.json();

    document.getElementById('content-header').style.display = 'block';
    document.getElementById('content-title').textContent = `Daily Digest — ${formatDate(date)}`;
    document.getElementById('content-meta').textContent =
      `${d.article_count} articles | ${d.total_words.toLocaleString()} words | ${d.topics_summary}`;

    document.getElementById('content-body').innerHTML =
      '<div class="markdown-content">' + renderMarkdown(d.markdown_text) + '</div>';
  }

  async function showRun(runId) {
    document.querySelectorAll('.run-item').forEach(el => el.classList.remove('active'));
    event.currentTarget.classList.add('active');

    const res = await fetch(`/api/runs/${runId}`);
    const r = await res.json();

    document.getElementById('content-header').style.display = 'block';
    document.getElementById('content-title').textContent = `Pipeline Run — ${formatTimestamp(r.started_at)}`;

    const duration = r.finished_at
      ? formatDuration(new Date(r.finished_at) - new Date(r.started_at))
      : 'running...';
    document.getElementById('content-meta').innerHTML =
      `Status: <span style="color:${statusColor(r.status)}">${r.status}</span> | Duration: ${duration}`;

    let html = '<div class="pipeline-steps">';
    for (const step of r.steps_log) {
      const icon = step.status === 'success' ? '&#10003;'
        : step.status === 'failed' ? '&#10007;'
        : step.status === 'skipped' ? '&#8212;'
        : '&#9679;';
      html += `
        <div class="step-item ${step.status}">
          <div class="step-icon">${icon}</div>
          <div class="step-details">
            <div class="step-name">${escapeHtml(step.step)}</div>
            ${step.message ? `<div class="step-message">${escapeHtml(step.message)}</div>` : ''}
          </div>
          <div class="step-time">${formatTimestamp(step.timestamp)}</div>
        </div>
      `;
    }
    html += '</div>';

    if (r.error_message) {
      html += `<div class="error-box"><strong>Error:</strong>\\n${escapeHtml(r.error_message)}</div>`;
    }

    document.getElementById('content-body').innerHTML = html;
  }

  // Simple markdown-to-HTML (handles headings, hrs, links, paragraphs)
  function renderMarkdown(text) {
    return text
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/^### (.+)$/gm, '<h3>$1</h3>')
      .replace(/^## (.+)$/gm, '<h2>$1</h2>')
      .replace(/^# (.+)$/gm, '<h1>$1</h1>')
      .replace(/^---$/gm, '<hr>')
      .replace(/\\[([^\\]]+)\\]\\(([^)]+)\\)/g, '<a href="$2" target="_blank">$1</a>')
      .replace(/\\n\\n/g, '</p><p>')
      .replace(/^(?!<[h1-6hr])/gm, function(m, offset, str) {
        return '';
      })
      .split('\\n\\n').map(block => {
        block = block.trim();
        if (!block) return '';
        if (block.startsWith('<h') || block.startsWith('<hr')) return block;
        return '<p>' + block + '</p>';
      }).join('\\n');
  }

  function escapeHtml(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }

  function formatDate(dateStr) {
    const [y, m, d] = dateStr.split('-');
    const dt = new Date(y, m - 1, d);
    return dt.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' });
  }

  function formatTimestamp(iso) {
    if (!iso) return '';
    const dt = new Date(iso);
    return dt.toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  }

  function formatDuration(ms) {
    const s = Math.floor(ms / 1000);
    if (s < 60) return s + 's';
    if (s < 3600) return Math.floor(s / 60) + 'm ' + (s % 60) + 's';
    return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm';
  }

  function statusColor(status) {
    return status === 'success' ? 'var(--green)'
      : status === 'failed' ? 'var(--red)'
      : 'var(--yellow)';
  }

  load();
  setInterval(load, 15000);
</script>

</body>
</html>
"""
