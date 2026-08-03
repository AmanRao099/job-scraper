# Chromium is required: Naukri's JSON API is reCAPTCHA-gated and its search
# page is client-rendered, so that source has to be rendered. If you run
# LinkedIn only, set PLAYWRIGHT_FALLBACK=false and you can drop the browser
# stage entirely for a ~700MB smaller image.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Installs the full Chromium build (needed for Chrome's new headless mode,
# which is what gets past bot detection) plus its system libraries.
RUN playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY app ./app
COPY main.py ./

# SQLite lives here; mount a volume so postings survive redeploys.
RUN mkdir -p /app/data

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# One worker on purpose: the scheduler and the scrape lock are per-process, so
# multiple workers would run concurrent scrapes. Scale by moving to Postgres
# and running the scraper as a separate single-instance service.
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
