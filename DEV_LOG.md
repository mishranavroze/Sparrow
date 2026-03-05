# Noctua Development Log

Track project milestones, decisions, experiments, and deployment status.

---

## Current Status

**Phase:** Multi-show refactor complete, ready for testing with live show
**Active Show:** The Morning Sparrow (Indian Politics + Entertainment, 5 min total)
**Deployment:** Replit (auto-deploy on commit)

---

## Architecture Decisions

### 2026-03 — Multi-Show Architecture
- **Decision:** Refactored from single-show to multi-show support
- **Why:** Enable running multiple independent podcasts from one deployment
- **How:** `shows.json` defines show metadata, `SHOW_{ID}_*` env vars for secrets, per-show database/output/feed isolation
- **Impact:** Every module updated to accept ShowConfig, all tests updated

### 2026-03 — Preparation Workflow
- **Decision:** Added multi-step review workflow before publishing
- **Why:** Allow human review of AI-generated digest before it goes live
- **Flow:** Start preparation -> preview digest -> upload audio -> publish (or cancel)
- **State:** In-memory digest held in ShowState, prep audio saved as `.prep.mp3`

### 2026-03 — Embedded Dashboard
- **Decision:** Dashboard HTML/JS/CSS embedded directly in main.py
- **Why:** Simplicity — no build step, no static file serving complexity
- **Tradeoff:** main.py is large (~2800 lines), but single-file deployment is convenient

---

## Deployment Notes

- **Platform:** Replit
- **Scheduler:** Built-in background scheduler (configurable UTC hour/minute)
- **External cron:** Supports cron-job.org trigger via `GET /api/cron/generate?secret=`
- **Chrome profile:** Persistent at `~/.noctua-chrome-profile` for NotebookLM sessions
- **GCS:** Optional — episodes uploaded to GCS bucket for permanent hosting

---

## Experiment Log

_Track ideas tried, results, and conclusions._

| Date | Experiment | Result | Notes |
|------|-----------|--------|-------|
| | | | |

---

## Known Issues / TODO

- [ ] Dashboard HTML embedded in main.py — consider extracting to templates if it grows further
- [ ] NotebookLM automation depends on persistent Chrome session — fragile across deployments
- [ ] No automated audio generation in CI (requires browser + Google login)

---

## Weekly Changelog

### Week of 2026-03-02
- Multi-show refactor completed (shows.json, per-show config, isolated databases)
- Preparation workflow with preview/upload/publish cycle
- Topic coverage radar chart with cumulative and latest modes
- Weekly archive system (Monday cleanup, ZIP bundling)
- PRODUCT_OVERVIEW.md documentation added
- 137 tests passing across 11 test modules
