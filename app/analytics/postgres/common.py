from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func

from app.core.errors import APIError

MAX_BUCKETS = 2500


def bucket_expr(interval: str, tz: str, col):  # type: ignore[no-untyped-def]
    """Timestamp truncated to `interval` in the query timezone (a naive local timestamp)."""
    return func.date_trunc(interval, func.timezone(tz, col))


def floor_local(dt: datetime, interval: str) -> datetime:
    dt = dt.replace(minute=0, second=0, microsecond=0)
    if interval == "hour":
        return dt
    dt = dt.replace(hour=0)
    if interval == "day":
        return dt
    if interval == "week":  # ISO week, Monday start (matches date_trunc('week'))
        return dt - timedelta(days=dt.weekday())
    return dt.replace(day=1)


def advance(dt: datetime, interval: str, n: int = 1) -> datetime:
    if interval == "hour":
        return dt + timedelta(hours=n)
    if interval == "day":
        return dt + timedelta(days=n)
    if interval == "week":
        return dt + timedelta(weeks=n)
    month = dt.month - 1 + n
    return dt.replace(year=dt.year + month // 12, month=month % 12 + 1)


def label_for(dt: datetime, interval: str) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M") if interval == "hour" else dt.strftime("%Y-%m-%d")


def bucket_labels(start: datetime, end: datetime, interval: str, tz: str) -> tuple[list[datetime], list[str]]:
    zone = ZoneInfo(tz)
    cur = floor_local(start.astimezone(zone).replace(tzinfo=None), interval)
    last = (end - timedelta(microseconds=1)).astimezone(zone).replace(tzinfo=None)
    buckets: list[datetime] = []
    while cur <= last:
        buckets.append(cur)
        if len(buckets) > MAX_BUCKETS:
            raise APIError(
                "Too many intervals for this date range; choose a larger interval or a shorter range",
                code="TOO_MANY_INTERVALS",
                status_code=422,
            )
        cur = advance(cur, interval)
    return buckets, [label_for(b, interval) for b in buckets]


def to_float(v) -> float | None:  # type: ignore[no-untyped-def]
    return None if v is None else float(v)


def iso(dt: datetime) -> str:
    return dt.isoformat()
