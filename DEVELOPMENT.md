# Chat MHC — Developer Guide

Technical reference for running, extending, and operating Chat MHC. For a
product overview see the [README](README.md).

> **Public copy.** This repository omits the members-only MHCO content that the
> private deployment carries: the `kb/` snapshot, the form PDFs in
> `forms_library/`, and the *Update knowledge base* workflow that publishes
> refreshed content. Everything below describes the full system; without that
> content the app starts on its built-in seed data.

## Architecture

- **FastAPI + SQLite** — one process, one file database. No GPU, no vector
  database, no Selenium. Runs on any small host.
- **Retrieval** — SQLite FTS5 (BM25) over content chunks with domain synonym
  expansion, content-type weighting (laws > updates > articles), and recency
  penalties for stale dated guidance.
- **LLM** — Anthropic Claude (`claude-sonnet-5-5` by default), streamed to the
  browser over SSE. Without an API key the app still runs and returns raw
  retrieval results so the pipeline is testable.
- **Auth** — single shared login with a signed session cookie. All API routes and PDF downloads require it.
- **Frontend** — dependency-free single-page app: streaming chat with source
  citations, chat history, an MHCO Forms browser with real PDF downloads, and
  an ORS 90/91 statute browser.

```
app/      FastAPI app (routes, auth, retrieval, Claude integration, SQLite)
ingest/   knowledge-base pipeline (MHCO scraper, official ORS, importer, seed, snapshot)
static/   frontend (index.html / app.js / styles.css)
kb/       committed knowledge-base snapshot baked into the deploy (tracked)
data/     runtime: chatmhc.db + cached form PDFs   (created automatically)
.github/  GitHub Actions: monthly + on-demand content refresh
```

## Quick start

```bash
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

Open http://localhost:8000. On first start the knowledge base is empty, so a small
seed dataset (condensed ORS 90 summaries + sample guidance + form index) is
loaded automatically — replace it with real data below.

### Docker

```bash
docker compose up --build
```

A `render.yaml` blueprint is included for Render.com (**free tier** — no
persistent disk; the knowledge base ships in `kb/`, see below). The Dockerfile
works as-is on Fly.io, Railway, or any VPS.

### Free-tier hosting model (Render free, ephemeral filesystem)

Hosts without a persistent disk wipe `data/` on every restart, so the
knowledge base is **baked into the deploy** as a committed snapshot:

- `python -m ingest export-snapshot` writes `kb/chatmhc.db` (content only — chat
  history is stripped) and `kb/forms/*.pdf`.
- On startup, when `data/` is empty, the app restores from `kb/` (falling back
  to seed content if there's no snapshot). See `ingest/snapshot.py` and the
  `lifespan` handler in `app/main.py`.
- Chat history lives only in `data/` and is intentionally not part of the
  snapshot, so it resets when a free instance sleeps. Switch `render.yaml` to a
  paid plan with a `disk:` to persist it.

## Loading real MHCO content

**Option A — refresh from the web** (requires an MHCO membership):

```bash
python -m ingest restore-snapshot       # start from the published kb/ content
python -m ingest scrape                 # laws (official ORS) + new MHCO articles + members
python -m ingest scrape --only laws     # or any subset: articles,laws,members
python -m ingest scrape --full          # re-read every article (slow, see below)
python -m ingest scrape --limit 5       # quick smoke test
```

- **Laws** come from the Legislature's official chapter pages (ORS 90 and 91,
  configured in `app/config.py` → `ORS_CHAPTERS`), split into one passage per
  section (long sections are split further). A chapter is only replaced when
  the parse looks complete and the text actually changed.
- **Articles** are incremental: only articles not already stored are fetched,
  usually a handful of requests. `--full` re-reads every article (hundreds of
  requests) to pick up edits and removals; it only prunes articles missing from
  the site when it managed to read at least 80% of the stored ones.
- **Nothing is deleted before its replacement is in hand.** mhco.org
  rate-limits aggressive clients (HTTP 429): requests are paced, `Retry-After`
  is honored, and once the site keeps refusing, the run stops contacting it and
  keeps the existing MHCO content (`ingest/polite.py`).
- `scrape` prints a per-source summary and exits 1 only if the laws could not
  be refreshed (whatever did refresh is still saved).

**Option A2 — MHCO via the bookmark button** (how MHCO content arrives in
practice: mhco.org blocks GitHub's servers with HTTP 429). In the app, click
**Update MHCO content** (sidebar) → drag **Grab MHCO updates** to the
bookmarks bar → use it on mhco.org while logged in. It reads the newest (or
all) articles and the members page from the member's own browser
(`static/mhco-bookmarklet.js`, built into a bookmark by
`static/mhco-helper.js`) and saves `mhco-export-*.json.gz`. Uploading that file
to `mhco_imports/` triggers the workflow, which imports it with the same
parsing and never-lose-content rules as the scraper (`ingest/mhco_import.py`)
and then deletes the file. Locally:

```bash
python -m ingest import-mhco                      # reads mhco_imports/
python -m ingest import-mhco --dir ~/Downloads    # or any folder of exports
```

**Option B — import the old ChatMHC JSON files** (from
`F:/ChatMHC_Production_Oregon/output/`):

```bash
python -m ingest import-json /path/to/output/
```

Either path replaces the seed placeholders. Check the result, then bake a
snapshot for deployment:

```bash
python -m ingest stats
python -m ingest export-snapshot   # writes kb/ ; commit it to deploy the content
```

## Tests

```bash
pytest
```

## Security

See **[SECURITY.md](SECURITY.md)** for the full security & privacy overview
(authentication, session handling, security headers/CSP, rate limiting, data
handling with the Anthropic API, and the July 2026 audit summary).

Notes:

- The app intentionally gates everything (including form PDFs) behind login
  because MHCO content is members-only. Never commit MHCO form PDFs or
  articles to this public repository.
- CI (`.github/workflows/ci.yml`) runs the test suite (including security
  regression tests) and `pip-audit` on every push and pull request.
