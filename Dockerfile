# Deriv Market Condition Analyzer — container image.
# Read-only market monitor: it never trades and needs no Deriv token.
# Secrets (Telegram) are passed as environment variables at run time, never baked in.

FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ANALYZER_DRY_RUN=true

# tini: PID 1 that forwards SIGTERM/SIGINT to Python and reaps zombies.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/*

# Dedicated non-root user (fixed uid/gid so volume ownership is predictable).
RUN groupadd --system --gid 10001 analyzer \
 && useradd --system --uid 10001 --gid analyzer --home-dir /app --no-create-home \
            --shell /usr/sbin/nologin analyzer

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code (see .dockerignore: no .env, data, logs, tests, caches).
COPY . .

# Code stays owned by root (read-only for the service user). Only data/ and logs/ are
# writable; Docker copies this ownership into new, empty named volumes on first use.
RUN python -m compileall -q app scripts \
 && mkdir -p /app/data /app/logs \
 && chown analyzer:analyzer /app/data /app/logs \
 && chmod 750 /app/data /app/logs \
 && python -m app.main --validate > /dev/null

USER analyzer

# Healthy = the heartbeat (written every 30 s by the running service) is fresh and the
# Deriv connection is not down for more than 5 minutes. See scripts/healthcheck.py.
HEALTHCHECK --interval=60s --timeout=10s --start-period=180s --retries=3 \
  CMD ["python", "/app/scripts/healthcheck.py"]

STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "app.main"]
