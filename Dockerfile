FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY --chown=app:app . .
RUN mkdir -p /data/exports && chown -R app:app /data
USER app
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s CMD curl -fsS http://localhost:8000/health || exit 1

# API: migrations run first (idempotent), then the server.
# WORKERS env var controls the number of uvicorn worker processes (default 1).
# Set WORKERS=4 for production; the worker process overrides CMD entirely.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*' ${WORKERS:+--workers $WORKERS}"]
