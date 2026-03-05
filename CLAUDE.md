# Noctua Podcast Platform

Automated podcast generation: Gmail newsletters -> AI digest -> NotebookLM audio -> RSS feed.

## Tech Stack

- **Backend:** Python, FastAPI, SQLite (WAL mode)
- **AI:** Google Gemini API (classification + summarization)
- **Audio:** Google NotebookLM via Playwright, ffmpeg for conversion
- **Email:** Gmail API with OAuth2
- **Feed:** feedgen (RSS + iTunes extensions)
- **Storage:** Local filesystem, optional Google Cloud Storage
- **Frontend:** Single-page dashboard embedded in main.py (~2000 lines HTML/JS/CSS)

## Project Structure

```
main.py              - FastAPI app (40+ endpoints), dashboard HTML, scheduler, show state
config.py            - Pydantic settings, shows.json loader, ShowConfig/ShowFormat dataclasses
generate.py          - 3-step pipeline orchestrator (fetch -> parse -> compile)
shows.json           - Show definitions (title, segments, durations, icons)

src/
  models.py          - Data classes (EmailMessage, Article, DailyDigest, CompiledDigest, EpisodeMetadata)
  exceptions.py      - Custom exception hierarchy (NoctuaError base)
  email_fetcher.py   - Gmail API integration, 24-hour rolling window
  content_parser.py  - HTML email -> clean text, junk removal, dedup
  topic_classifier.py - Article -> segment classification via Gemini
  digest_compiler.py - Articles -> podcast script with time budgets
  llm_client.py      - Google Gemini API wrapper
  notebooklm.py      - Playwright automation for audio generation
  episode_manager.py - MP3 processing, metadata extraction, validation
  feed_builder.py    - RSS feed generation, episode catalog (JSON)
  gcs_storage.py     - Google Cloud Storage upload
  database.py        - SQLite: digests, episodes, pipeline_runs tables

scripts/
  gmail_auth.py       - OAuth2 token generation per show
  manual_publish.py   - Publish pre-existing audio+digest files
  backfill.py         - Regenerate digests for date ranges
  notebooklm_login.py - Chrome session setup for NotebookLM

tests/               - 137 tests across 11 modules
```

## Key Patterns

- **Multi-show architecture:** shows.json defines shows, env vars (`SHOW_{ID}_*`) hold secrets
- **Per-show isolation:** Each show gets its own output_dir, database, feed.xml, episodes dir
- **Preparation workflow:** Generate digest (in-memory) -> preview -> upload audio -> publish
- **ShowState:** Per-show state machine in main.py tracks generation locks and prep state
- **Weekly lifecycle:** Monday cleanup archives week's MP3s to ZIP, rebuilds feed

## Database Schema

- `digests`: date (unique), markdown_text, article_count, total_words, topics_summary, segment_counts (JSON)
- `episodes`: date (unique), file_size_bytes, duration_seconds, gcs_url, rss_summary
- `pipeline_runs`: run_id, status, current_step, steps_log (JSON array)

## Running

```bash
# Dev server
python -m uvicorn main:app --reload --port 8000

# Run tests
python -m pytest tests/ -v

# Generate digest manually
python generate.py
```

## Configuration

- `.env` for secrets (Gmail creds, Gemini API key, GCS creds, cron secret)
- `shows.json` for show metadata (segments, durations, titles)
- Per-show env vars: `SHOW_SPARROW_GMAIL_CREDENTIALS_JSON`, `SHOW_SPARROW_GMAIL_TOKEN_JSON`, etc.
