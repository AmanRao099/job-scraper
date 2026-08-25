# Tech Job Extraction API

Aggregates Indian entry-level IT jobs and targeted international technology
jobs from Naukri and LinkedIn into a local database, then serves them over a
filterable, paginated REST API.

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
pip install -r requirements-scrape.txt   # API + browser. Read-only API: requirements.txt
playwright install chromium              # required for Naukri, see "Sources" below

cp .env.example .env             # optional, all values have defaults

python main.py scrape --sources linkedin --limit 10   # populate (~5 min)
python main.py serve                                  # API on :8000
```

Then open **<http://localhost:8000/>** — a zero-dependency test console is
served by the API itself (same-origin, so no CORS setup and no build step). It
lets you browse and filter jobs, expand descriptions, trigger a scrape (either
the full sweep or a targeted profile) and watch its live log, and hit any
endpoint by hand.

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
| `employment_type` | string, repeatable | `full_time`, `part_time`, `contract`, `temporary`, `internship`, `unknown` |
| `country` | string, repeatable | Normalized country, e.g. `Germany`; repeated values use OR matching |
| `is_abroad` | bool | International jobs only (`true`) or India/geographically unknown (`false`) |
| `masters_match` | bool | Description contains a recognized Masters-equivalent degree |
| `education_requirement` | string, repeatable | `required`, `preferred`, `accepted`, `mentioned`, `not_stated` |
| `visa_sponsorship` | string, repeatable | `offered`, `not_offered`, `unknown` |
| `relocation_support` | string, repeatable | `offered`, `not_offered`, `unknown` |
| `work_authorization_required` | bool | Whether an explicit right-to-work requirement was detected |
| `location` / `company` | string | Case-insensitive substring |
| `min_experience` / `max_experience` | int | Years |
| `posted_within_days` | int | 1–365 |
| `include_inactive` | bool | Default false |
| `sort` | enum | `posted_at`, `first_seen_at`, `last_seen_at`, `title`, `company`, `experience` |
| `order` | enum | `asc`, `desc` |
| `page` / `page_size` | int | Defaults 1 / 25, max page size 200 |

Repeated filters accept at most 25 values, free text is capped at 200
characters, and page numbers are bounded. Invalid enum values return HTTP 422.

```bash
curl "http://localhost:8000/jobs?category=Backend&skill=Python&skill=Django&max_experience=2&page_size=10"
```

International Masters jobs that explicitly offer sponsorship:

```bash
curl "http://localhost:8000/jobs?is_abroad=true&masters_match=true&visa_sponsorship=offered"
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
| `GET` | `/scrape/profiles` | Targeted scrape presets (see below) |
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

## Targeted scrapes (profiles)

A normal run sweeps ~95 search terms across all of India. A **profile** narrows
that on three axes at once — which terms to search, which city to search them
in, and which postings survive afterwards — so one button produces a listing you
would otherwise assemble by hand.

Both UIs render a button per profile (the purple one, next to "Run scrape"), and
every entry point takes the same key:

```bash
curl -X POST localhost:8000/scrape/run -H 'content-type: application/json' \
     -d '{"profile": "bangalore-fresher-startups"}'

python main.py scrape --profile bangalore-fresher-startups
```

`GET /scrape/profiles` returns the catalogue, including each profile's queries,
so a frontend never hard-codes one.

### `bangalore-fresher-startups`

Entry-level tech roles in Bengaluru at companies that read as startups.

| Stage | What it does |
|---|---|
| Search | 32 fresher-slanted terms (`fresher software engineer`, `founding engineer`, `data analyst fresher`, …) scoped to the city — LinkedIn by `geoId=105214831`, Naukri by URL slug `…-jobs-in-bangalore` |
| Location | Keeps only postings whose location names Bengaluru/Bangalore or one of its tech corridors (Whitefield, Koramangala, HSR Layout, …). An unstated or purely remote location is dropped — it cannot be verified |
| Fresher | Keeps ≤ 1 year of required experience, or a `fresher`/`intern` seniority. A plain title with no stated experience classifies as mid-level and is dropped |
| Startup | Rejects IT-services majors, global captives, banks, the Big Four and staffing agencies outright, then requires the ad to describe itself in startup terms — *early-stage*, *Series A*, *founding team*, *YC-backed*, *bootstrapped*, … |

Each stage counts its rejections under its own reason (`outside_target_city`,
`not_fresher`, `not_startup`), so the run log shows exactly where the funnel
narrowed:

```
Profile bangalore-fresher-startups: kept 1 of 22 ({'not_startup': 18, 'not_fresher': 2, 'outside_target_city': 1})
```

Two consequences worth knowing:

* **The startup test is precision-first.** Neither board exposes company size or
  funding stage, so "startup" is inferred from the company name and the ad's own
  words. A real startup whose ad never says so is dropped. The alternative —
  keeping everything not on the blocklist — readmits every mid-size services
  company the list happens not to name, which is most of them. Both lists live in
  `app/startups.py` and are meant to be edited.
* **A profile run skips housekeeping.** It visited one city and a fraction of the
  catalogue, so ageing out postings it never looked for would wrongly deactivate
  live jobs — the same reason a cancelled run skips it.

Adding a profile is one entry in `PROFILES` in `app/profiles.py`; the API, both
UIs and the CLI pick it up with no further changes.

### `worldwide-masters-tech`

International technology roles at any experience level whose actual posting
description mentions a recognized Masters-equivalent qualification.

```bash
python main.py scrape --profile worldwide-masters-tech

curl -X POST localhost:8000/scrape/run -H 'content-type: application/json' \
     -d '{"profile": "worldwide-masters-tech"}'
```

The profile uses LinkedIn, the existing international-capable source, and a
compact catalogue spanning software/backend, AI/ML, data, research, cloud,
cybersecurity, computer vision, and embedded systems. Its search location is
worldwide and LinkedIn's entry-level filter is omitted. It does **not** issue a
query per country and it does not use a country acceptance allowlist: any
posting confidently classified outside India can pass. Coverage is still
limited by LinkedIn's public guest results, query ranking, paging depth, rate
limits, and the locations/descriptions employers publish.

Search wording alone never qualifies a result. After collection, the stored
description must contain one of `Masters degree`, `Master of Science`, `Master
of Engineering`, `MS`, `MSc`, `MEng`, `Postgraduate degree`, `Advanced degree`,
`M.Tech`/`MTech`, or `M.E.` in qualification context. `Scrum Master`, master
data/records/branches, master services agreements, `MS SQL`, and ordinary `me`
are excluded. Context near each degree classifies it as `required`, `preferred`,
`accepted`, or `mentioned`.

Jobs are international when their location identifies a non-India country or
known foreign city/region, or the description explicitly offers worldwide
remote eligibility. A bare `Remote` location remains geographically unknown.
Indian cities and states are recognized even when the location omits `India`.
Country normalization is best-effort; region-level evidence may set
`is_abroad=true` while leaving `country` empty.

The profile includes sponsorship values `offered`, `not_offered`, and `unknown`;
it never filters on them. Negative sponsorship wording takes precedence over a
generic mention. Work-authorization requirements and relocation support are
stored independently.

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
| `ALLOWED_HOSTS` | local hosts | **Must list public API hostnames in production** |
| `ADMIN_TOKEN` | *(empty)* | Protects the scrape and maintenance endpoints |
| `HTTP_CONNECT_TIMEOUT` / `HTTP_READ_TIMEOUT` | `10` / `25` | Separate network timeouts |
| `HTTP_RETRIES` / `HTTP_BACKOFF_MAX` | `3` / `20` | Transient retry policy; honors `Retry-After` |
| `HTTP_MAX_RESPONSE_BYTES` | `5000000` | Decompressed response-size ceiling |
| `DATABASE_URL` | SQLite in `./data` | Set to `postgresql+asyncpg://…` to switch |
| `DATABASE_SSL_MODE` | `require` | Production PostgreSQL must use `verify-full` |

Coverage is driven by `SEARCH_QUERIES` in `app/taxonomy.py` — about 95 role
queries spanning development, data, AI/ML, cloud, QA, security, support,
networking, database, ERP, embedded, and design.

---

## Deployment

**Hosted API, free tier → [DEPLOY.md](DEPLOY.md).** Neon Postgres for storage,
GitHub Actions cron for the scraping (the only free tier with enough RAM to run
Chromium), Render for the read API. The scraper and the API are separate
processes that only meet in the database, which is what keeps each one small
enough to fit in a free tier.

**One box you control:**

```bash
docker compose up -d --build
```

That runs API, scheduler and Chromium in a single container. The compose file
mounts a named volume at `/app/data` so postings survive redeploys, and sets
`shm_size: 1gb` because Chromium needs more than Docker's default 64MB of
shared memory.

Two images are defined: `Dockerfile` (everything, ~1.1GB) and `Dockerfile.api`
(no browser, ~180MB) for hosts that only serve reads.

Before exposing it publicly:

1. Set `ADMIN_TOKEN` to a long random string.
2. Set `CORS_ORIGINS` to your frontend's real origin (not `*`).
3. Set `ALLOWED_HOSTS` to the API hostname(s).
4. Mount a persistent volume for `/app/data`.

**Run one API worker.** The scheduler and the scrape lock are per-process, so
multiple workers would run concurrent scrapes against the same SQLite file. To
scale horizontally, move to Postgres and run the scraper as a separate
single-instance service.

---

## CLI

```bash
python main.py serve [--port 8000] [--reload]
python main.py scrape [--sources naukri linkedin] [--limit N] [--profile KEY]
python main.py scrape --profile worldwide-masters-tech [--limit N]
python main.py scrape --profile bangalore-fresher-startups [--limit N]
python main.py reindex --dry-run          # report offline changes only
python main.py reindex --batch-size 500   # checkpointed offline backfill
python main.py reindex --start-after-id 10000
python main.py stats
python main.py export output/jobs.jsonl
python main.py import output/jobs.jsonl    # backfill from the pre-2.0 format
```

---

## How postings are processed

1. **Collect** — sources are isolated; query and request concurrency are
   independently bounded. Retries apply only to transient failures. Block,
   rate-limit, malformed-response and repeated-page outcomes are recorded.
2. **Normalise** — skills, category, seniority, work mode, experience,
   qualification context, country, sponsorship, authorization and relocation
   signals are derived deterministically from stored posting text.
3. **Filter** — non-tech roles, over-experienced roles, dead listings and stale
   postings are dropped. Every rejection is counted and reported in the run
   stats, so a coverage regression shows up instead of failing silently.
4. **Dedup** — stable source ID, canonical URL, employer URL and then a
   normalized composite identity are considered in that order. Distinct source
   IDs stay distinct; cross-source matches merge provenance and richer content.
5. **Persist** — bounded bulk identity lookups and checkpointed batches make
   reruns idempotent without per-row queries or commits.
6. **Age out** — postings not re-seen in `STALE_AFTER_DAYS` become inactive;
   after `PURGE_AFTER_DAYS` they are deleted.

`python main.py reindex` recomputes derived classifications from stored columns
without network requests or active-status changes. It traverses by stable
primary-key cursor, commits checkpoints, skips unchanged rows, supports dry-run
and can resume from a printed `last_id`. Profile eligibility is deliberately
not re-applied globally.

Run summaries distinguish complete, partial, cancelled and failed runs and
include per-source pages, accepted responses, blocks, network/parse failures,
duplicates and duration.

### Interpretation limits

* A Masters match means the posting mentioned a broadly equivalent credential;
  it does **not** guarantee that the employer will accept an Indian M.Tech.
* A foreign location does **not** guarantee visa eligibility.
* `visa_sponsorship=unknown` means the posting did not state sponsorship—not
  that sponsorship is available.
* Country, qualification wording, and job-board coverage are best-effort. Open
  the original board/employer apply link to confirm current requirements.

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

242 tests covering qualification/international classification, skill and tech
taxonomy, experience parsing, normalization and deduplication, profiles,
HTTP retry/source recovery, additive migration/reindex, repository filters, and
API validation. They use temporary databases and make no network calls.

Run the reproducible 10,000-job diagnostic with:

```bash
python benchmarks/benchmark.py --size 10000
```

It reports classification/dedup time and peak memory, batch upsert, reindex,
combined filtering and facet generation. Timings are diagnostic, not test gates.

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
├── profiles.py       targeted scrapes: city + fresher + startup presets
├── startups.py       startup vs enterprise heuristic
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
