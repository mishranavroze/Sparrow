# Noctua

**Your nightly knowledge briefing.** *The owl of Minerva spreads its wings only with the falling of dusk.*

Noctua is a personal daily podcast generator that ingests newsletter emails from Gmail, compiles them into a digest, feeds them into Google NotebookLM via browser automation, and publishes the generated podcast as a Spotify-compatible RSS feed.

**Target output:** A 15–30 minute two-host AI podcast episode, generated daily.

## Architecture

```
Gmail API  →  Content Parser  →  Digest Compiler  →  NotebookLM (Playwright)
                                                            ↓
RSS Feed (FastAPI)  ←  Episode Manager  ←  Downloaded MP3
```

## Quick Start

### 1. Install dependencies

```bash
pip install -e ".[dev]"
playwright install chromium
```

### 2. Configure environment

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

Required variables:
- `GMAIL_CREDENTIALS_JSON` — Gmail OAuth2 credentials (JSON string)
- `GMAIL_TOKEN_JSON` — Gmail OAuth2 token (JSON string)
- `BASE_URL` — Public URL where the app is served

See `.env.example` for all options.

### 3. Gmail setup

Run the Gmail auth helper to get your OAuth2 token:

```bash
python scripts/gmail_auth.py
```

### 4. NotebookLM session

Log in to Google manually (one-time):

```bash
python scripts/notebooklm_login.py
```

This opens a browser where you log in to your Google account. The session is saved in the persistent Chrome profile and reused for headless runs.

### 5. Run the server

```bash
uvicorn main:app --host 0.0.0.0 --port 5000
```

### 6. Generate an episode

```bash
python generate.py
```

## Endpoints

| Route | Description |
|-------|-------------|
| `GET /` | Dashboard — digest viewer and pipeline status |
| `GET /feed.xml` | RSS podcast feed |
| `GET /episodes/{filename}` | Serve MP3 files (supports range requests) |
| `GET /health` | Health check |
| `GET /api/digests` | List all digests (JSON) |
| `GET /api/runs` | List pipeline runs (JSON) |

## Project Structure

```
├── main.py                  # FastAPI server
├── generate.py              # Daily generation orchestrator
├── config.py                # Pydantic settings
├── src/
│   ├── email_fetcher.py     # Gmail API integration
│   ├── content_parser.py    # HTML → clean text
│   ├── digest_compiler.py   # Articles → single source document
│   ├── notebooklm.py        # Playwright automation for NotebookLM
│   ├── episode_manager.py   # MP3 validation and metadata
│   ├── feed_builder.py      # RSS feed generation
│   ├── database.py          # SQLite storage
│   ├── models.py            # Data classes
│   └── exceptions.py        # Custom exception hierarchy
├── tests/                   # Unit tests (pytest)
├── scripts/
│   ├── gmail_auth.py        # Gmail OAuth2 setup
│   └── notebooklm_login.py  # Manual NotebookLM login
└── output/
    ├── episodes/            # Generated MP3s
    ├── feed.xml             # RSS feed
    └── noctua.db            # SQLite database
```

## Scheduling

The scheduler is built into the app — no separate cron or deployment needed. When the FastAPI server starts, a background task automatically triggers generation at the configured `GENERATION_HOUR`:`GENERATION_MINUTE` UTC every day (default: 02:30 UTC = 6:30 PM PST).

You can also trigger generation manually:
- Click **"Prepare Digest"** in the dashboard
- Or `POST /api/start-preparation`

The `/health` endpoint shows the next scheduled run time and whether generation is currently running.

## Spotify Submission

Once the feed is live and has at least one episode:

1. Go to [Spotify for Podcasters](https://podcasters.spotify.com)
2. Submit your RSS feed URL: `https://your-app.replit.app/feed.xml`
3. Spotify will validate the feed and begin indexing

## Testing

```bash
pytest
```

## Tech Stack

- **Python 3.11+**, **FastAPI**, **Playwright**, **SQLite**
- **Gmail API** for email ingestion
- **Google NotebookLM** for AI podcast generation
- **feedgen** for RSS, **mutagen** for MP3 metadata, **BeautifulSoup4** for HTML parsing

## License

MIT
