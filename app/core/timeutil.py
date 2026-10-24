"""UTC helpers. Everything is stored and computed in UTC; timezones only matter for bucketing/display."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(dt: datetime) -> datetime:
    """Naive datetimes are interpreted as UTC."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def parse_tz(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        raise ValueError(f"Unknown timezone: {name!r}") from None


def is_valid_tz(name: str) -> bool:
    try:
        ZoneInfo(name)
        return True
    except Exception:
        return False


def local_midnight_utc(d: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(d, time.min, tzinfo=tz).astimezone(UTC)


def day_range_utc(d_from: date, d_to: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Inclusive local dates -> [start, end) UTC instants."""
    return local_midnight_utc(d_from, tz), local_midnight_utc(d_to + timedelta(days=1), tz)
