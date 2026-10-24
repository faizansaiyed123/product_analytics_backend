"""API usage counters. Hot-path increments go to Redis (no row contention); a worker flushes hourly buckets to Postgres."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import get_redis

logger = logging.getLogger("app.usage")
FIELDS = ("requests", "events_accepted", "events_rejected", "events_duplicate", "rate_limited")


def _bucket(now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    return now.strftime("%Y%m%d%H")


async def record(project_id: UUID, **counts: int) -> None:
    try:
        r = get_redis()
        key = f"usage:{project_id}:{_bucket()}"
        pipe = r.pipeline()
        for name, value in counts.items():
            if value and name in FIELDS:
                pipe.hincrby(key, name, value)
        pipe.expire(key, 7 * 86400)
        await pipe.execute()
    except Exception as exc:
        logger.warning("usage counter unavailable: %s", exc)


async def flush(db: AsyncSession, project_id: UUID | None = None) -> int:
    """Move Redis counters into api_usage. Safe to call concurrently (counters are decremented, not deleted)."""
    r = get_redis()
    pattern = f"usage:{project_id}:*" if project_id else "usage:*"
    flushed = 0
    async for key in r.scan_iter(match=pattern, count=500):
        data = await r.hgetall(key)
        vals = {f: int(data.get(f, 0)) for f in FIELDS}
        if not any(vals.values()):
            continue
        _, pid, bucket = key.split(":")
        bucket_ts = datetime.strptime(bucket, "%Y%m%d%H").replace(tzinfo=UTC)
        await db.execute(
            text(
                """
                INSERT INTO api_usage (project_id, bucket, requests, events_accepted, events_rejected, events_duplicate, rate_limited)
                VALUES (:pid, :bucket, :requests, :events_accepted, :events_rejected, :events_duplicate, :rate_limited)
                ON CONFLICT (project_id, bucket) DO UPDATE SET
                  requests = api_usage.requests + EXCLUDED.requests,
                  events_accepted = api_usage.events_accepted + EXCLUDED.events_accepted,
                  events_rejected = api_usage.events_rejected + EXCLUDED.events_rejected,
                  events_duplicate = api_usage.events_duplicate + EXCLUDED.events_duplicate,
                  rate_limited = api_usage.rate_limited + EXCLUDED.rate_limited
                """
            ),
            {"pid": UUID(pid), "bucket": bucket_ts, **vals},
        )
        pipe = r.pipeline()
        for f, v in vals.items():
            if v:
                pipe.hincrby(key, f, -v)
        await pipe.execute()
        flushed += 1
    return flushed
