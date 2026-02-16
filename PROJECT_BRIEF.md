# Noctua — Build Specification (v2)

## Project Overview

**Noctua** is a personal daily podcast generator that ingests newsletter/blog emails from Gmail, compiles them into a daily digest, feeds them into Google NotebookLM via browser automation (Playwright), generates an Audio Overview (the NotebookLM two-host podcast), downloads the MP3, and publishes it as an RSS feed compatible with Spotify and other podcast apps.

The name comes from the owl of Athena — "the owl of Minerva spreads its wings only with the falling of dusk." This is an end-of-day knowledge briefing.

**Target output:** A 15–30 minute two-host podcast episode, generated daily at a scheduled time. Audio quality matches NotebookLM's Audio Overview — natural, conversational, with two AI hosts that banter, debate, and synthesize.

---

## Why NotebookLM via Playwright?

- **Best-in-class audio quality** — NotebookLM's Audio Overview produces the most natural-sounding AI podcast available. The hosts have natural hesitations, interruptions, reactions, and genuine conversational flow. No other TTS solution comes close.
- **Free** — No per-character or per-minute audio costs. NotebookLM is free to use.
- **No API allowlist needed** — The standalone Podcast API requires enterprise allowlist access. Browser automation sidesteps this entirely.
- **Simpler pipeline** — No need for a separate LLM to write a podcast script, no TTS engine, no audio stitching. NotebookLM handles script generation AND audio synthesis in one step.

**Tradeoff:** Browser automation is inherently fragile. NotebookLM UI changes can break selectors. The spec includes resilience patterns to handle this.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Runtime | Python 3.11+ |
| Hosting | Replit (Deployments → Scheduled for cron + Always On for server) |
| Email | Gmail API (`google-api-python-client`) |
| Browser Automation | Playwright (`playwright` Python SDK) |
| Content Prep | `beautifulsoup4` for HTML parsing, optionally Claude API for digest summarization |
| RSS | `feedgen` library for podcast RSS generation |
| Web Server | FastAPI (lightweight, serves RSS feed + MP3 files) |
| Storage | Local filesystem on Replit (or Replit Object Storage if available) |

---

## Architecture

```
┌─────────────┐     ┌──────────────────┐     ┌──────────────────────────┐
│  Gmail API   │────▶│  Content Parser   │────▶│  Digest Compiler          │
│  (fetch)     │     │  (extract text)   │     │  (compile into one doc)   │
└─────────────┘     └──────────────────┘     └────────────┬─────────────┘
                                                           │
                                                           ▼
┌─────────────┐     ┌──────────────────┐     ┌──────────────────────────┐
│  RSS Feed    │◀────│  Episode Manager  │◀────│  NotebookLM Automator    │
│  (FastAPI)   │     │  (save + catalog) │     │  (Playwright: upload →   │
└─────────────┘     └──────────────────┘     │   generate → download)   │
                                              └──────────────────────────┘
```

---

## Project Structure

```
noctua/
├── main.py                  # FastAPI app — serves RSS feed + audio files
├── generate.py              # Entry point for daily generation (cron calls this)
├── config.py                # All configuration, env vars, constants
├── src/
│   ├── __init__.py
│   ├── email_fetcher.py     # Gmail API integration
│   ├── content_parser.py    # HTML → clean text extraction
│   ├── digest_compiler.py   # Compile articles into a single source document
│   ├── notebooklm.py        # Playwright automation for NotebookLM
│   ├── episode_manager.py   # Save MP3, extract metadata (duration, size)
│   └── feed_builder.py      # RSS/podcast feed generation
├── output/
│   └── episodes/            # Generated MP3s stored here
├── tests/
│   ├── test_email_fetcher.py
│   ├── test_content_parser.py
│   ├── test_digest_compiler.py
│   ├── test_notebooklm.py
│   └── test_feed_builder.py
├── .env.example             # Template for required env vars
├── .gitignore
├── pyproject.toml           # Dependencies and project metadata
├── README.md
└── LICENSE
```

---

## Module Specifications

### 1. `config.py`

Centralized configuration using `pydantic-settings` or simple `os.getenv`.

```python
# Required environment variables:
GMAIL_CREDENTIALS_JSON    # Gmail OAuth2 credentials (JSON string)
GMAIL_TOKEN_JSON          # Gmail OAuth2 token (JSON string)
GMAIL_LABEL               # Gmail label to filter (default: "Newsletters")
GOOGLE_ACCOUNT_EMAIL      # Google account email for NotebookLM login
GOOGLE_ACCOUNT_PASSWORD   # Google account password (or use persistent session)
GENERATION_HOUR           # Hour to generate (24h format, default: 18)
BASE_URL                  # Public URL where Replit serves the app (for RSS feed links)
PODCAST_TITLE             # Default: "Noctua"
PODCAST_DESCRIPTION       # Default: "Your nightly knowledge briefing."
CHROME_USER_DATA_DIR      # Path to persistent Chrome profile (to keep Google login alive)

# Optional:
ANTHROPIC_API_KEY         # Only needed if using Claude to pre-summarize the digest
NOTEBOOKLM_NOTEBOOK_URL   # URL of a pre-created notebook to reuse (recommended)
```

---

### 2. `src/email_fetcher.py`

**Responsibility:** Authenticate with Gmail and fetch today's newsletter emails.

- Use Gmail API with OAuth2 (store credentials as Replit Secrets)
- Query: `label:{GMAIL_LABEL} after:{today_date} before:{tomorrow_date}`
- Return a list of `EmailMessage` dataclass objects:

```python
@dataclass
class EmailMessage:
    subject: str
    sender: str
    date: datetime
    body_html: str
    body_text: str  # fallback if no HTML
```

- Handle pagination if there are many emails
- Graceful error handling — if Gmail auth fails, log and exit cleanly
- Rate limiting awareness

---

### 3. `src/content_parser.py`

**Responsibility:** Convert raw email HTML into clean, readable text.

- Use `beautifulsoup4` to parse HTML
- Strip: navigation, footers, unsubscribe links, tracking pixels, social media links, ads
- Preserve: headings, paragraphs, article text, links (as plain text URLs)
- Deduplicate: if multiple newsletters cover the same story, detect overlap (simple similarity check using difflib or similar) and merge
- Output a list of `Article` dataclass objects:

```python
@dataclass
class Article:
    source: str          # newsletter name
    title: str           # subject line or extracted headline
    content: str         # clean text
    estimated_words: int
```

- Also output a `DailyDigest` that includes all articles and total word count

---

### 4. `src/digest_compiler.py`

**Responsibility:** Compile all parsed articles into a single source document suitable for NotebookLM upload.

- Take the list of `Article` objects from the content parser
- Compile into a single well-structured text document:

```
# Daily News Digest — February 16, 2026

## From: Morning Brew
### [Article Title]
[Article content...]

---

## From: TLDR
### [Article Title]
[Article content...]

---

[...more articles...]
```

- **Important:** NotebookLM works best when sources are clear and well-organized. Use markdown-style headings and separators.
- Save the compiled digest as a temporary `.txt` file for upload (NotebookLM accepts plain text via paste)
- If total content exceeds NotebookLM's source limit (~500K characters), prioritize most recent/important sources and truncate
- Optionally: use Claude API to pre-summarize the digest if it's too long, to stay within limits while preserving key information

```python
@dataclass
class CompiledDigest:
    text: str              # The full compiled text
    article_count: int
    total_words: int
    date: str              # YYYY-MM-DD
    topics_summary: str    # Brief list of topics for RSS episode description
```

---

### 5. `src/notebooklm.py` — THE CORE MODULE

**Responsibility:** Automate NotebookLM via Playwright to upload the digest, generate an Audio Overview, and download the resulting MP3.

**Reference implementation:** https://github.com/israelbls/notebooklm-podcast-automator (FastAPI + Playwright project that does exactly this). Study this repo's approach to selectors, wait strategies, and error handling. Adapt patterns from it but write clean Python — don't just copy-paste.

#### Authentication Strategy

- Use a **persistent Chrome profile** (`CHROME_USER_DATA_DIR`) so the Google account session survives between runs
- On first run: the script opens Chrome with the profile, you manually log in to your Google account. The session cookie persists.
- On subsequent runs: Playwright reuses the profile — no login needed
- If session expires: detect the login page, log a warning, and notify (manual re-login required). Do NOT attempt to automate Google login — it's against ToS and will trigger CAPTCHAs.

#### Notebook Strategy

Two approaches (implement both, configurable):

**Option A: Reuse a single notebook (recommended)**
- Pre-create a notebook in NotebookLM manually. Save its URL in `NOTEBOOKLM_NOTEBOOK_URL`
- Each day: delete previous sources → add new digest as source → delete old audio overview → generate new audio overview → download
- Cleaner, less chance of hitting notebook creation limits

**Option B: Create a new notebook per day**
- Create a new notebook each run
- Add digest as source
- Generate audio overview
- Download
- Optionally delete old notebooks to avoid clutter

#### Playwright Automation Flow

```python
class NotebookLMAutomator:
    """Automates NotebookLM Audio Overview generation via Playwright."""

    async def generate_episode(self, digest: CompiledDigest) -> Path:
        """
        Full pipeline: upload digest → generate audio → download MP3.
        Returns path to downloaded MP3 file.
        """
        async with async_playwright() as p:
            browser = await p.chromium.launch_persistent_context(
                user_data_dir=config.CHROME_USER_DATA_DIR,
                headless=True,  # Run headless in production
                # Use a realistic viewport and user agent
                viewport={"width": 1280, "height": 720},
            )
            page = browser.pages[0] if browser.pages else await browser.new_page()

            try:
                # Step 1: Navigate to notebook
                await self._navigate_to_notebook(page)

                # Step 2: Clear previous sources (if reusing notebook)
                await self._clear_sources(page)

                # Step 3: Add digest as source
                await self._add_text_source(page, digest.text)

                # Step 4: Delete existing audio overview (if any)
                await self._delete_existing_audio(page)

                # Step 5: Trigger Audio Overview generation
                await self._generate_audio_overview(page)

                # Step 6: Wait for generation to complete
                await self._wait_for_audio_ready(page)

                # Step 7: Download the MP3
                mp3_path = await self._download_audio(page, digest.date)

                return mp3_path

            except Exception as e:
                # Take screenshot for debugging
                await page.screenshot(path=f"output/debug/error-{digest.date}.png")
                raise NotebookLMError(f"Audio generation failed: {e}") from e

            finally:
                await browser.close()
```

#### Key Implementation Details

**Selector resilience:**
- Use multiple selector strategies (aria labels, data attributes, text content) as fallbacks
- Wrap selectors in a helper that tries multiple approaches:

```python
async def _find_element(self, page, selectors: list[str], description: str):
    """Try multiple selectors to find an element. Raises if none work."""
    for selector in selectors:
        try:
            element = await page.wait_for_selector(selector, timeout=5000)
            if element:
                return element
        except TimeoutError:
            continue
    raise SelectorNotFoundError(f"Could not find {description} with any selector: {selectors}")
```

**Adding source text:**
- NotebookLM supports adding text via "Copied text" source type
- Navigate to add source → select "Copied text" → paste the digest text → confirm
- Alternatively, save digest as a `.txt` file and use the file upload option

**Waiting for audio generation:**
- Audio Overview generation takes 2-5 minutes
- Poll for completion: check for the audio player element or a "Ready" status indicator
- Set a reasonable timeout (10 minutes max) with polling interval of 15 seconds
- Log progress updates

**Downloading:**
- NotebookLM has a download button on the audio player
- Intercept the download via Playwright's download handling:

```python
async def _download_audio(self, page, date: str) -> Path:
    download_path = Path(f"output/episodes/noctua-{date}.mp3")

    async with page.expect_download() as download_info:
        await self._click_download_button(page)

    download = await download_info.value
    await download.save_as(str(download_path))
    return download_path
```

**Error recovery:**
- If generation fails, retry once with a fresh page load
- If selectors break (UI update), log detailed error with screenshot and fail gracefully
- Never leave zombie browser processes — always close in `finally` block

---

### 6. `src/episode_manager.py`

**Responsibility:** Manage downloaded MP3 files and extract metadata.

- Verify downloaded MP3 is valid (non-zero size, valid audio headers)
- Extract metadata using `mutagen` or `pydub`:
  - Duration (in seconds and HH:MM:SS format)
  - File size in bytes
- Rename/move to canonical path: `output/episodes/noctua-{YYYY-MM-DD}.mp3`
- Clean up old episodes beyond retention limit (keep last 30)
- Return episode metadata:

```python
@dataclass
class EpisodeMetadata:
    date: str                  # YYYY-MM-DD
    file_path: Path
    file_size_bytes: int
    duration_seconds: int
    duration_formatted: str    # HH:MM:SS
    topics_summary: str        # From digest compiler
```

---

### 7. `src/feed_builder.py`

**Responsibility:** Generate and maintain a podcast-compliant RSS feed.

- Use `feedgen` with the podcast extension
- Feed metadata:
  - Title: "Noctua"
  - Description: "Your nightly knowledge briefing. The owl of Minerva spreads its wings only with the falling of dusk."
  - Language: en
  - Category: News > Daily News
  - Image: (provide a placeholder URL, can add artwork later)
- Each episode entry:
  - Title: "Noctua — {Month Day, Year}" (e.g., "Noctua — February 16, 2026")
  - Description: Brief summary of topics covered (from digest compiler's `topics_summary`)
  - Enclosure: URL to the MP3 file served by FastAPI
  - Duration: actual episode duration from episode_manager
  - Publication date: generation timestamp
- Keep last 30 episodes in the feed (Spotify wants recent content)
- Write RSS XML to `output/feed.xml`
- The RSS feed URL must be publicly accessible for Spotify to index it

---

### 8. `main.py` — FastAPI Server

**Responsibility:** Serve the RSS feed and audio files over HTTP.

```python
# Endpoints:
GET /                        # Simple landing page with Noctua branding
GET /feed.xml                # RSS podcast feed (Content-Type: application/rss+xml)
GET /episodes/{filename}     # Serve MP3 files (Content-Type: audio/mpeg)
GET /health                  # Health check endpoint
```

- Set correct Content-Type headers (critical for Spotify to recognize the feed)
- Support range requests for audio files (some podcast apps need this)
- CORS headers if needed
- Keep it minimal — this is a file server, not a full web app

---

### 9. `generate.py` — Orchestrator

**Responsibility:** The main pipeline that runs on schedule.

```python
# Pseudocode:
async def generate_episode():
    # 1. Fetch emails
    emails = email_fetcher.fetch_todays_emails()
    if not emails:
        log("No newsletters today. Skipping.")
        return

    # 2. Parse content
    digest = content_parser.parse_emails(emails)
    log(f"Parsed {len(digest.articles)} articles, {digest.total_words} words")

    # 3. Compile into single source document
    compiled = digest_compiler.compile(digest)
    log(f"Compiled digest: {compiled.total_words} words, {compiled.article_count} articles")

    # 4. Generate audio via NotebookLM
    automator = NotebookLMAutomator()
    mp3_path = await automator.generate_episode(compiled)
    log(f"Audio generated: {mp3_path}")

    # 5. Process episode (verify, extract metadata)
    metadata = episode_manager.process(mp3_path, compiled.topics_summary)
    log(f"Episode: {metadata.duration_formatted}, {metadata.file_size_bytes / 1_000_000:.1f} MB")

    # 6. Update RSS feed
    feed_builder.add_episode(metadata)
    log("Feed updated. Episode published.")
```

- Cron: set up as Replit Scheduled Deployment to run at configured hour daily
- Full error handling with structured logging at every step
- If any step fails, send a notification (optional: email, Discord webhook, or just log)
- The entire generate function must be async (Playwright requires it)

---

## Playwright Setup on Replit

Playwright needs Chromium installed. On Replit:

```bash
# Install Playwright and browsers
pip install playwright
playwright install chromium
playwright install-deps  # System dependencies (may need Replit's Nix config)
```

**Replit Nix configuration** (if needed in `replit.nix`):
```nix
{ pkgs }: {
  deps = [
    pkgs.chromium
    pkgs.ffmpeg  # For audio processing if needed
  ];
}
```

**Headless mode:** Always run headless in production (`headless=True`). For debugging during development, set `headless=False` to see what's happening.

**Persistent Chrome profile:** Store at a stable path (e.g., `~/.noctua-chrome-profile/`). This keeps the Google login session alive between cron runs.

---

## Google Account Session Management

**CRITICAL: Do NOT automate Google login.** Google actively detects and blocks automated login attempts. You'll hit CAPTCHAs, security challenges, and potential account locks.

**Instead:**
1. On first setup, run the script with `headless=False`
2. The browser opens to NotebookLM
3. Manually log in to your Google account
4. Close the browser — the session is saved in the persistent Chrome profile
5. All subsequent headless runs reuse the session

**Session refresh:** Google sessions typically last weeks/months. If the session expires:
- The script detects the login page (check for login URL patterns)
- Logs a warning and sends a notification
- You manually re-run with `headless=False` to re-login
- Add a health check that verifies the session is alive before running the full pipeline

---

## Development Practices

### Code Quality
- Type hints on all function signatures
- Docstrings on all public functions (Google style)
- Use `dataclasses` or `pydantic` models for all data structures — no raw dicts
- Linting: `ruff` (replaces flake8 + isort + black)
- Formatting: `ruff format`
- Async code: use `asyncio` properly, avoid mixing sync/async

### Error Handling
- Custom exception hierarchy:
  - `NoctuaError` (base)
  - `EmailFetchError`
  - `ContentParseError`
  - `DigestCompileError`
  - `NotebookLMError`
  - `SelectorNotFoundError` (subclass of NotebookLMError)
  - `AudioGenerationTimeout` (subclass of NotebookLMError)
  - `SessionExpiredError` (subclass of NotebookLMError)
  - `EpisodeProcessError`
  - `FeedBuildError`
- Never let the pipeline silently fail — always log with context
- Take debug screenshots on any Playwright failure
- Use `structlog` or standard `logging` with structured output

### Testing
- Unit tests for each module using `pytest` + `pytest-asyncio`
- Mock external APIs (Gmail) in tests
- Test content parser with sample HTML newsletters
- Test digest compiler with various article counts
- For NotebookLM automator: test selector logic with saved HTML snapshots (don't hit live NotebookLM in tests)
- Integration test: run the full pipeline against a test notebook

### Configuration
- All secrets in Replit Secrets (environment variables)
- `.env.example` documents every required variable
- No hardcoded API keys, URLs, or credentials anywhere
- Config validation on startup — fail fast if missing required vars

### Git
- `.gitignore`: exclude `output/`, `.env`, `__pycache__/`, Chrome profile dir, debug screenshots
- Meaningful commit messages
- No generated audio files in the repo

---

## Environment Variables (.env.example)

```bash
# Gmail OAuth2 (paste the full JSON as a string)
GMAIL_CREDENTIALS_JSON='{...}'
GMAIL_TOKEN_JSON='{...}'

# Google Account (for NotebookLM session — login is manual, not automated)
GOOGLE_ACCOUNT_EMAIL=your.email@gmail.com

# Optional: Claude API for digest summarization (if emails exceed NotebookLM limits)
ANTHROPIC_API_KEY=sk-ant-...

# NotebookLM Configuration
NOTEBOOKLM_NOTEBOOK_URL=https://notebooklm.google.com/notebook/xxxxx
CHROME_USER_DATA_DIR=~/.noctua-chrome-profile

# Podcast Configuration
GMAIL_LABEL=Newsletters
GENERATION_HOUR=18
BASE_URL=https://your-replit-app.replit.app
PODCAST_TITLE=Noctua
PODCAST_DESCRIPTION=Your nightly knowledge briefing. The owl of Minerva spreads its wings only with the falling of dusk.
```

---

## RSS Feed Requirements for Spotify

Spotify requires specific RSS elements. Ensure:

1. `<itunes:image>` tag with a square image (minimum 1400x1400px, max 3000x3000px)
2. `<itunes:category>` — use "News" > "Daily News"
3. `<itunes:author>` and `<itunes:owner>` tags
4. Each `<item>` must have `<enclosure>` with `type="audio/mpeg"` and `length` (file size in bytes)
5. `<itunes:duration>` in HH:MM:SS format
6. `<itunes:explicit>false</itunes:explicit>`
7. Feed must be publicly accessible via HTTPS
8. Submit the feed URL to Spotify for Podcasters (podcasters.spotify.com) for indexing

---

## Known Risks & Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| NotebookLM UI changes break selectors | Pipeline stops working | Use resilient selectors (aria labels + text + data attrs). Take screenshots on failure. Pin to known-good selector versions and update as needed. |
| Google session expires | Can't access NotebookLM | Detect login page, notify owner, manual re-login. Health check before each run. |
| NotebookLM rate limits / usage caps | Can't generate daily | NotebookLM free tier has limits (~3 Audio Overviews/day for free, more with Plus). Monitor usage. Fallback: skip a day or use shorter content. |
| Audio generation takes too long | Cron job times out | Set 15-min timeout. If exceeded, log and retry next day. |
| NotebookLM is deprecated/changed | Project breaks | Design modules so NotebookLM can be swapped for ElevenLabs GenFM or Play.ai with minimal changes. Keep `notebooklm.py` as the only module that touches browser automation. |
| Replit cold starts | Slow first run | Keep the Always On deployment for the FastAPI server. Cron job can tolerate cold starts. |

---

## Estimated Costs

| Component | Cost |
|---|---|
| NotebookLM Audio Overview | Free (within daily limits) |
| Gmail API | Free |
| Claude API (optional digest summarization) | ~$0.01-0.05/day |
| Replit hosting | Free tier or ~$7/mo for Always On |
| **Total** | **$0–7/month** |

---

## Future Enhancements (Do NOT build now, but design for extensibility)

- Custom intro/outro jingle (prepend/append to downloaded MP3)
- Episode artwork generation (per episode, showing topics covered)
- Multi-language support (NotebookLM supports 50+ languages)
- Source priority weighting (some newsletters > others)
- Web dashboard showing episode history and analytics
- Discord/Telegram notification when new episode is ready
- Transcript generation (NotebookLM provides transcripts — could extract those too)
- OPML import to auto-configure newsletter sources
- Fallback TTS engine: if NotebookLM fails, fall back to ElevenLabs GenFM API or Play.ai

---

## Build Order

Build and test in this order:

1. **Project scaffolding** — structure, config, dependencies, .env
2. **Playwright setup** — get Chromium running on Replit, persistent profile, manual Google login
3. **NotebookLM automator** — the hardest part first. Get a working proof-of-concept that creates a notebook, adds text, generates audio, and downloads the MP3. Test with hardcoded sample text.
4. **Email fetcher** — get Gmail auth working, fetch test emails
5. **Content parser** — parse sample emails into clean text
6. **Digest compiler** — compile articles into a single source doc
7. **Episode manager** — validate MP3, extract metadata
8. **Feed builder** — generate valid RSS feed
9. **FastAPI server** — serve feed + episodes
10. **Orchestrator** — wire everything together in generate.py
11. **Scheduling** — set up Replit cron
12. **Spotify submission** — submit RSS feed URL

**NOTE:** Step 3 (NotebookLM automator) is the riskiest and most complex piece. Build this first as a standalone proof-of-concept before investing time in the other modules. If Playwright automation proves unreliable, you can pivot to ElevenLabs or Play.ai before building the rest of the pipeline.
