# Deploying the API (free tier only)

Goal: a public HTTPS JSON API your own frontend can call, kept fresh
automatically. Three free services, no credit card.

| Piece | Runs on | Why there |
|---|---|---|
| Postgres | **Neon** free tier | Free hosts wipe their disk on every deploy, so SQLite cannot survive one. 0.5GB is roughly 400k postings. |
| Scraper | **GitHub Actions** cron | Needs Chromium and ~1GB RAM for a few minutes, four times a day. A 512MB always-on box cannot render a page; a CI runner has 16GB and costs nothing. |
| API | **Render** free web service | Reads Postgres, serves JSON. No browser in the image, so it builds small and boots fast. |

The scraper and the API never talk to each other — they meet in the database.
That is what makes each piece small enough to fit in a free tier.

```
GitHub Actions (cron, Chromium) ──writes──► Neon Postgres ◄──reads── Render API ◄── your frontend
```

The `UI/` directory in this repo is a local dev dashboard. It is not part of
this deployment; ignore it.

---

## 1. Database — Neon

1. Sign up at <https://neon.tech> → **Create project**. Region: `AWS ap-southeast-1 (Singapore)`.
2. Copy the connection string. It looks like:

   ```
   postgresql://neondb_owner:npg_xxxx@ep-cool-name-123456.ap-southeast-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require
   ```

   Use the **direct** connection, not the `-pooler` one. This app keeps a small
   pool of its own, and PgBouncer's transaction mode breaks asyncpg's prepared
   statements.

Paste that string verbatim wherever `DATABASE_URL` is asked for below — the app
rewrites the scheme and drops the libpq-only parameters at startup
(`app/config.py`). Tables are created automatically on first boot.

> Neon's free compute suspends after 5 minutes idle and wakes in under a second.
> The engine uses `pool_pre_ping` and a 280s recycle, so a suspended connection
> is replaced rather than raising.

## 2. API — Render

You do not need Docker installed, or Docker at all. Render builds on its own
machines, and `render.yaml` uses their native Python runtime — a plain
`pip install -r requirements.txt`. Nothing here needs system packages.

1. <https://render.com> → **New → Blueprint** → connect this repo. It reads
   `render.yaml` and creates `job-scraper-api`.
2. Render prompts for the values marked `sync: false`:
   - `DATABASE_URL` — the Neon string.
   - `CORS_ORIGINS` — your frontend's origin(s), comma separated, scheme
     included, **no trailing slash**:
     `https://app.example.com,http://localhost:5173`
   - `CORS_ORIGIN_REGEX` — leave blank unless your frontend's hostname changes
     per deploy (preview builds). Example: `https://.*\.my-app\.vercel\.app`
3. After the first deploy, open **Environment** and copy the generated
   `ADMIN_TOKEN` — step 3 needs it.
4. Check it:

   ```bash
   curl https://<your-service>.onrender.com/health
   # {"status":"ok","database":true,"scheduler":false,...}
   ```

   OpenAPI schema: `/openapi.json`. Interactive docs: `/docs`. A self-contained
   test console (no build step, same-origin) is at `/ui` — handy for poking
   endpoints by hand while you wire up your frontend.

Two consequences of the free plan, both accounted for in the design:

- **It sleeps after 15 minutes of no traffic.** The request that wakes it waits
  ~40s; everything after is fast. Set your HTTP client's timeout to 60s and,
  if the delay matters, fire a throwaway `GET /health` when your app boots.
- **Scraping from this host is LinkedIn-only.** No Chromium installed, so
  Naukri (which must be rendered) is skipped. The scheduled Actions run is what
  keeps data fresh; `POST /scrape/run` here is only a manual top-up.

The Python version is whatever Render defaults to; the code runs on 3.12–3.14.
Pin it with a `PYTHON_VERSION` environment variable if you ever need to.

## 3. Scraper — GitHub Actions

Push the repo to GitHub, then **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value |
|---|---|
| `DATABASE_URL` | the Neon string from step 1 |
| `API_URL` | your Render URL. Optional — enables stale-posting cleanup. |
| `ADMIN_TOKEN` | the token Render generated. Optional, same reason. |

`.github/workflows/scrape.yml` then runs at 02:17 / 08:17 / 14:17 / 20:17 UTC.

Verify it before waiting six hours: **Actions → scrape → Run workflow**, set
**limit** to `5`. A ~3 minute smoke test that should end with a non-zero job
count from `python main.py stats`. Then re-run without the limit to fill the
database properly.

Minutes: unmetered on a public repo. On a private repo you get 2,000/month and
this schedule uses roughly 1,200 — if you add sources or raise page counts,
change the cron to `17 2,14 * * *`.

---

## Order of operations

1. Neon → `DATABASE_URL`.
2. Render → deploy. Get the API URL and the generated `ADMIN_TOKEN`.
3. GitHub secrets → run the workflow manually with `limit=5` to seed data.
4. Point your frontend at the API URL; confirm `CORS_ORIGINS` matches its origin exactly.

---

## Calling it from your frontend

All read endpoints are public GETs with no auth. Full parameter list at `/docs`.

```
GET  /jobs?q=react&skill=Python&skill=AWS&max_experience=2&page=1&page_size=25
GET  /jobs/{id}
GET  /filters          facet counts for building dropdowns (cached 3 min)
GET  /stats            totals, breakdowns, last run
GET  /meta             categories, seniorities, work modes, known skills
GET  /health
```

Notes that matter when wiring this up:

- **Array filters repeat the key**, they are not comma separated:
  `?skill=Python&skill=AWS` means *both*, not either.
- **Pagination is in the body**, under `meta`: `{page, page_size, total,
  total_pages, has_next, has_prev}`. `page_size` is capped at `MAX_PAGE_SIZE`
  (200); asking for more silently clamps.
- `/jobs` returns active postings only. `include_inactive=true` to see retired ones.
- Sort with `sort=posted_at|first_seen_at|last_seen_at|title|company|experience`
  and `order=asc|desc`.

Mutating endpoints require `X-Admin-Token: <ADMIN_TOKEN>`:

```
POST   /scrape/run                      start a scrape, returns 202 + run_id
POST   /scrape/runs/{id}/cancel
DELETE /jobs/maintenance/stale
GET    /scrape/runs/{id}/stream         SSE: live log + progress (no auth)
```

**Never put `ADMIN_TOKEN` in frontend code.** Anything shipped to a browser is
readable. If you want a scrape button, proxy it through your own backend. The
cron keeps data fresh without it.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `TypeError: connect() got an unexpected keyword argument 'sslmode'` | `DATABASE_URL` bypassed normalization. Confirm the deployed `app/config.py` is current. |
| CORS error in the browser | `CORS_ORIGINS` does not exactly match the browser's origin. Scheme included, no trailing slash, no path. |
| First request of the day hangs ~40s | Render free instance cold start. Expected. |
| `/health` says `"database": false` | Wrong `DATABASE_URL`, or you used the `-pooler` host. |
| Render build fails on a package wheel | Pin `PYTHON_VERSION` to `3.12` in the dashboard and redeploy. |
| `/jobs` returns `total: 0` | The scrape workflow has not run yet, or it failed. Check the Actions tab. |
| Scrape workflow fails, `stats` shows 0 jobs | The boards changed their markup. Re-run with `limit=5` and read the step log. |
| `409 A scrape is already running` | A previous run died mid-flight. Re-run the workflow; `--stale-after 45` clears it after 45 minutes. |

## Self-hosting instead

If you ever have a box that stays on, `docker compose up` runs everything in one
container — API, scheduler and Chromium, against SQLite on a named volume. Two
images are defined for that path: `Dockerfile` (everything) and `Dockerfile.api`
(no browser, for container hosts that only serve reads). Neither is used by the
free deployment above.
