# Noctua Podcast Platform — Product Overview

Noctua is a fully-automated podcast generation platform that transforms daily newsletter emails into AI-hosted audio episodes, published to an RSS feed and managed through a web dashboard.

---

## How It Works

Noctua runs a daily pipeline: **Gmail Inbox → Content Parsing → AI Digest → Audio Generation → RSS Feed**. Emails are fetched from a configured Gmail label, cleaned and deduplicated, classified into topics, summarized into a podcast script by Gemini, converted to audio via Google NotebookLM, and published to a podcast-compatible RSS feed.

---

## Product Features

### Multi-Show Podcast Engine
- Host multiple independent podcasts from a single deployment, each with its own Gmail source, topic format, segment durations, branding, database, and output directory.
- Per-show configuration defines podcast title, description, icon, segment order, intro/outro length, and target topic durations.

### Intelligent Content Curation
- Fetches emails from Gmail within a rolling 24-hour window using OAuth2.
- Strips tracking pixels, hidden elements, scripts, footers, and unsubscribe links from HTML emails.
- Deduplicates articles across newsletters using content similarity matching.
- Filters out transactional senders (Google, noreply, Substack notifications, etc.).

### AI-Powered Topic Classification
- Classifies each article into configurable topic segments (e.g., World Politics, Tech & AI, Entertainment, Sports sub-topics like F1, CrossFit, Arsenal).
- Uses a layered approach: known single-topic sources are mapped directly, multi-topic aggregators are classified via keyword scoring, and unmatched articles fall back to a general category.

### Podcast Script Compilation
- Generates flowing prose narratives per topic segment, word-budgeted to match target audio durations (150 words/minute).
- Includes a podcast preamble with production instructions, optional location-based weather in the intro (via wttr.in), and per-segment duration labels.
- Produces a one-sentence RSS episode summary alongside the full script.

### Audio Generation & Format Support
- Automates NotebookLM Audio Overview generation using Playwright browser automation.
- Uploads the compiled digest, triggers audio generation, and downloads the resulting MP3.
- Accepts manual audio uploads in MP3, M4A, WAV, OGG, and WebM formats with automatic conversion to MP3 via ffmpeg.
- Validates MP3 integrity before publishing.

### Preparation Workflow (Review Before Publish)
- A multi-step workflow lets users review content before it goes live: **Generate Digest → Preview → Upload Audio → Publish**.
- In-memory digest can be downloaded as markdown, inspected for topic balance, and discarded without affecting the live feed.
- Prep audio is saved separately from the canonical episode file until the user explicitly publishes.
- Supports cancellation at any stage, preserving existing published content.

### RSS Podcast Feed
- Standards-compliant RSS feed with iTunes podcast extensions (category, author, artwork, per-episode summaries).
- Retains the 30 most recent episodes, with proper enclosure tags and duration metadata.
- Supports Google Cloud Storage URLs for permanent hosting, falling back to local streaming with HTTP Range request support.

### Web Dashboard
- **Latest tab**: Current episode player, digest stats (article count, word count, emails processed), topic breakdown table, show format reference card, and a radar chart comparing actual vs. target topic coverage with source recommendations.
- **History tab**: Full archive of past episodes and digests with download links, audio playback, and weekly export management.
- Dark theme, mobile-responsive layout, real-time status polling.

### Topic Coverage Analytics
- Radar chart visualization compares target topic capacity against actual content fill.
- Supports "Latest" (current episode) and "All Time" (last 30 episodes) views.
- Generates actionable suggestions: subscribe to more sources when a topic is under-filled (<30%), trim sources when over-filled (>200%).

### Scheduling & Automation
- Configurable daily generation time with a built-in background scheduler.
- Automatic missed-run detection on startup — if the scheduled time has passed and no episode exists for today, generation triggers immediately.
- Supports external cron services (protected by a shared secret) for reliable triggering.

### Weekly Archive & Cleanup
- Every Monday, the previous week's MP3 episodes and markdown digests are bundled into a ZIP archive.
- Local MP3 files are deleted after archiving to manage disk space.
- Manual "Export This Week" option available from the dashboard at any time.
- Pending export ZIPs are listed for download and removed after retrieval.

### Episode Management
- Bump episode revision to force podcast apps to re-download updated audio.
- Episode lock protection: once an episode is published, its digest cannot be accidentally overwritten.
- Per-episode metadata stored permanently: date, duration, file size, topics, RSS summary, GCS URL.

### Pipeline Observability
- Every generation run is logged with step-by-step execution details (fetch emails, parse content, compile digest, generate audio, publish).
- Run history accessible via API and dashboard, including timing, status, and error messages.

### Google Cloud Storage Integration
- Optional GCS upload for published episodes, providing permanent public URLs.
- RSS feed automatically uses GCS URLs when available.

### Helper Scripts
- **Gmail auth**: Interactive OAuth2 token generation for each show.
- **Manual publish**: Publish pre-existing audio and digest files outside the normal pipeline.
- **Backfill**: Regenerate digests for a date range (useful for retroactive corrections).
- **NotebookLM login**: Interactive Playwright session for initial NotebookLM authentication.

---

## Technical Summary

| Component | Technology |
|---|---|
| Backend | Python, FastAPI, SQLite (WAL mode) |
| AI | Google Gemini API (summarization + classification) |
| Audio | Google NotebookLM via Playwright, ffmpeg |
| Email | Gmail API with OAuth2 |
| Feed | feedgen (RSS + iTunes extensions) |
| Storage | Local filesystem, optional Google Cloud Storage |
| Frontend | Single-page app (vanilla HTML/JS/CSS, Canvas radar chart) |
