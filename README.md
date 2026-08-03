# Tech Job Extraction API

Aggregates entry-level IT and tech job postings from Naukri and LinkedIn into a
local database, and serves them over a filterable, paginated REST API.

Scraping runs on a schedule in the background. Your frontend only ever reads
from the database, so `GET /jobs` returns in milliseconds and no user waits for
a scrape.

```
┌──────────┐   scheduled    ┌───────────┐   normalise    ┌──────────┐
│ Naukri   │ ─────────────► │ pipeline  │ ─────────────► │ SQLite   │
│ LinkedIn │   every 6h     │ (async)   │  filter+dedup  │          │
└──────────┘                └───────────┘                └────┬─────┘
                                                              │ ms reads
                                                        ┌─────▼─────┐
                                                        │ FastAPI   │◄── your frontend
                                                        └───────────┘
```

---

## Quick start

```bash
python -m venv venv
venv\Scripts\activate            # Linux/macOS: source venv/bin/activate
pip install -r requirements.txt
playwright install chromium      # required for Naukri, see "Sources" below

cp .env.example .env             # optional, all values have defaults

python main.py scrape --sources linkedin --limit 10   # populate (~5 min)
python main.py serve                                  # API on :8000
```

Then open **<http://localhost:8000/>** — a zero-dependency test console is
served by the API itself (same-origin, so no CORS setup and no build step). It
lets you browse and filter jobs, expand descriptions, trigger a scrape and
watch its live log, and hit any endpoint by hand.

Interactive API docs: <http://localhost:8000/docs>

Full React dashboard (optional):

```bash
cd UI
npm install
npm run dev                      # http://localhost:5173
```

Note: the React UI runs on a different origin, so its origin must be listed in
`CORS_ORIGINS`. The built-in console at `/` has no such requirement.

### How long a scrape takes

Measured, not estimated:

| Scope | Time | Result |
|---|---|---|
| 4 queries, both sources | ~2.5 min | 303 raw |
| 10 queries, LinkedIn only | ~5 min | 113 raw → 89 stored |
| **All 95 queries, both sources** | **~24 min** | 7,655 raw → 3,352 new |

Naukri dominates the wall clock: it has to render search pages in a browser at
concurrency 4. For quick iteration use `--sources linkedin`, which is pure HTTP.

A full run leaves ~3,400 active postings in the database with zero duplicate
fingerprints.

---

## API

Everything your frontend needs. Full schema at `/docs`.

### `GET /jobs`

Filterable, paginated listing.

| Parameter | Type | Notes |
|---|---|---|
| `q` | string | Free text over title, company, skills, description |
| `category` | string, repeatable | e.g. `Backend`, `Data Science / AI-ML` |
| `source` | string, repeatable | `naukri`, `linkedin` |
| `skill` | string, repeatable | **Conjunctive** — results match *all* listed skills |
| `seniority` | string, repeatable | `intern`, `fresher`, `junior`, `mid`, `senior`, `lead` |
| `work_mode` | string, repeatable | `onsite`, `hybrid`, `remote` |
| `location` / `company` | string | Case-insensitive substring |
| `min_experience` / `max_experience` | int | Years |
| `posted_within_days` | int | 1–365 |
| `include_inactive` | bool | Default false |
| `sort` | enum | `posted_at`, `first_seen_at`, `last_seen_at`, `title`, `company`, `experience` |
| `order` | enum | `asc`, `desc` |
| `page` / `page_size` | int | Defaults 1 / 25, max page size 200 |

```bash
curl "http://localhost:8000/jobs?category=Backend&skill=Python&skill=Django&max_experience=2&page_size=10"
```

```json
{
  "items": [
    {
      "id": 42,
      "source": "naukri",
      "title": "Python Developer",
      "company": "Acme",
      "location": "Bengaluru",
      "apply_link": "https://www.naukri.com/job-listings-...",
      "category": "Backend",
      "categories": ["Backend", "Data Engineering / Analytics"],
      "skills": ["Django", "Docker", "PostgreSQL", "Python"],
      "seniority": "fresher",
      "work_mode": "onsite",
      "experience_text": "0-2 Yrs",
      "experience_min": 0,
      "experience_max": 2,
      "salary_text": "",
      "posted_at": "2026-08-01T09:15:00Z",
      "first_seen_at": "2026-08-02T04:00:11Z",
      "is_active": true
    }
  ],
  "meta": { "page": 1, "page_size": 10, "total": 137, "total_pages": 14, "has_next": true, "has_prev": false }
}
```

### Other endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness + database + scheduler status |
| `GET` | `/meta` | Categories, sources, known skills, scheduler state |
| `GET` | `/jobs/{id}` | One posting, including the full description |
| `GET` | `/filters` | Facet counts for building filter UI (cached 3 min) |
| `GET` | `/stats` | Totals, breakdowns, last run summary |
| `POST` | `/scrape/run` | Start a scrape in the background → `202` + `run_id` |
| `GET` | `/scrape/runs` | Recent runs |
| `GET` | `/scrape/runs/{id}` | Status, progress, stats |
| `POST` | `/scrape/runs/{id}/cancel` | Stop a running scrape, keeping partial results |
| `GET` | `/scrape/runs/{id}/stream` | Live SSE log for one run |
| `DELETE` | `/jobs/maintenance/stale` | Deactivate/purge on demand |

`POST /scrape/run` and the maintenance endpoint require an `X-Admin-Token`
header **when `ADMIN_TOKEN` is set**. It is unset by default so local
development works without ceremony — set it before deploying.

`/scrape/runs/{id}/stream` fans out to any number of subscribers and replays
the backlog, so refreshing the page mid-run works and a second tab does not
steal the first tab's output.

### Stopping a run

`POST /scrape/runs/{id}/cancel` is **cooperative**. Sources check the stop flag
between pages and between queries, so the run halts at a clean boundary and
everything it already collected is still normalised, deduped and saved. If it
has not wound down within `HARD_CANCEL_GRACE_SECONDS` (45), the task is
cancelled outright and the run is recorded as `cancelled`.

A cancelled run deliberately **skips housekeeping** — it never reached most of
its queries, so ageing out "unseen" postings would wrongly deactivate live jobs.

Measured: stopping a 48-page run at 41.7% still saved 25 new and 53 updated
postings.

If the row says `running` but no task in this process owns it (the previous
process died), the endpoint reports it as orphaned and clears it, so it stops
blocking future scrapes.

---

## Sources

| Source | Transport | Notes |
|---|---|---|
| **LinkedIn** | Plain HTTP | Public guest endpoints, no login. Fast. Rate limits hard, so descriptions are fetched at concurrency 2. |
| **Naukri** | Headless Chromium | Its JSON API answers `406 {"message":"recaptcha required"}` for any client that cannot mint a reCAPTCHA token, and the search page is client-rendered. |

Naukri notes:

* Requires `playwright install chromium`. The `browser_channel=chromium`
  setting selects Chrome's **new headless mode** — Playwright's default bundled
  *headless shell* is bot-detected and gets served an empty page. New headless
  needs no display server, so this still works in a container.
* The pipeline probes Naukri's JSON API once per run. If the gate is ever
  relaxed, the fast path turns on by itself.
* To run without a browser entirely: `SOURCES_ENABLED=linkedin` and
  `PLAYWRIGHT_FALLBACK=false`.

### Adding a source

Drop a module in `app/sources/` that subclasses `JobSource`, yields `RawJob`s
from `fetch()`, and register it in `app/sources/__init__.py`. Normalisation,
filtering, dedup, storage and the whole API come for free.

---

## Configuration

All settings are environment variables — see `.env.example` for the annotated
list. The ones that matter most:

| Variable | Default | Effect |
|---|---|---|
| `MAX_EXPERIENCE_YEARS` | `3` | Postings whose *minimum* required experience exceeds this are dropped at ingest. Raise it to widen beyond entry level. |
| `SOURCES_ENABLED` | `naukri,linkedin` | Which boards to scrape |
| `SCRAPE_INTERVAL_HOURS` | `6` | Background refresh cadence |
| `NAUKRI_PAGES` / `LINKEDIN_PAGES` | `3` / `4` | Depth per search query (20 / 10 results per page) |
| `CORS_ORIGINS` | localhost:5173 | **Must list your real frontend origin in production** |
| `ADMIN_TOKEN` | *(empty)* | Protects the scrape and maintenance endpoints |
| `DATABASE_URL` | SQLite in `./data` | Set to `postgresql+asyncpg://…` to switch |

Coverage is driven by `SEARCH_QUERIES` in `app/taxonomy.py` — about 95 role
queries spanning development, data, AI/ML, cloud, QA, security, support,
networking, database, ERP, embedded, and design.

---

## Deployment

```bash
docker compose up -d --build
```

The compose file mounts a named volume at `/app/data` so postings survive
redeploys, and sets `shm_size: 1gb` because Chromium needs more than Docker's
default 64MB of shared memory.

Before exposing it publicly:

1. Set `ADMIN_TOKEN` to a long random string.
2. Set `CORS_ORIGINS` to your frontend's real origin (not `*`).
3. Mount a persistent volume for `/app/data`.

**Run one API worker.** The scheduler and the scrape lock are per-process, so
multiple workers would run concurrent scrapes against the same SQLite file. To
scale horizontally, move to Postgres and run the scraper as a separate
single-instance service.

---

## CLI

```bash
python main.py serve [--port 8000] [--reload]
python main.py scrape [--sources naukri linkedin] [--limit N]
python main.py stats
python main.py export output/jobs.jsonl
python main.py import output/jobs.jsonl    # backfill from the pre-2.0 format
```

---

## How postings are processed

1. **Collect** — every source runs concurrently; within a source, every search
   query runs concurrently under a shared semaphore.
2. **Normalise** — skills, category, seniority, work mode and an experience
   range are derived from the title, description and recruiter tags.
3. **Filter** — non-tech roles, over-experienced roles, dead listings and stale
   postings are dropped. Every rejection is counted and reported in the run
   stats, so a coverage regression shows up instead of failing silently.
4. **Dedup** — a fingerprint of `title|company|city` collapses the same posting
   found under several search terms or on both boards; the richer record wins.
5. **Persist** — one bulk fingerprint lookup, then a single batched write.
6. **Age out** — postings not re-seen in `STALE_AFTER_DAYS` become inactive;
   after `PURGE_AFTER_DAYS` they are deleted.

### Matching is word-boundary aware

The taxonomy matches whole tokens, treating `+`, `#` and `.` as word
characters. `Node.js` matches as one token, `C` does not match inside `C++`,
and a trailing sentence period does not block a match. Regression tests for
each of these live in `tests/test_taxonomy.py`.

---

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

77 tests covering skill extraction, tech classification, categorisation,
experience parsing, normalisation and dedup, the query layer, and the HTTP
endpoints. They use a temporary database and make no network calls.

---

## Project layout

```
app/
├── api.py            FastAPI routes
├── config.py         env-driven settings
├── db.py             async engine, session factory, SQLite pragmas
├── models.py         ORM models + fingerprinting
├── schemas.py        request/response contract
├── repository.py     all SQL: filters, pagination, upsert, facets
├── pipeline.py       scrape orchestration
├── enrich.py         RawJob -> stored row, quality filters
├── taxonomy.py       skills, categories, role detection, query catalogue
├── events.py         per-run pub/sub for SSE
├── scheduler.py      APScheduler background refresh
├── http_client.py    bounded-concurrency HTTP with retry/backoff
├── utils.py          HTML/date/salary parsing
└── sources/
    ├── base.py       JobSource contract + RawJob
    ├── browser.py    Playwright renderer
    ├── naukri.py
    └── linkedin.py
UI/                   React + Vite dashboard
tests/
main.py               CLI
```

---

## Disclaimer

For educational and research purposes. You are responsible for complying with
the terms of service of any site this tool accesses.
