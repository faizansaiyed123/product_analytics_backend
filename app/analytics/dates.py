"""Date-range resolution. Ranges are interpreted in the query timezone and converted to [start, end) UTC instants."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.analytics.queries import DateRange
from app.core.config import get_settings
from app.core.errors import APIError
from app.core.timeutil import day_range_utc, ensure_utc, local_midnight_utc


def _parse(value: str, tz: ZoneInfo, *, end: bool) -> datetime:
    """Date-only values cover the whole local day; datetimes are exact instants (naive = query tz)."""
    value = value.strip()
    try:
        if len(value) == 10:
            d = date.fromisoformat(value)
            return local_midnight_utc(d + timedelta(days=1 if end else 0), tz)
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        dt = dt.replace(tzinfo=tz) if dt.tzinfo is None else dt
        return ensure_utc(dt)
    except ValueError:
        raise APIError(
            f"Invalid date: {value!r}. Use YYYY-MM-DD or ISO 8601.", code="INVALID_DATE_RANGE", status_code=422
        ) from None


def resolve_range(dr: DateRange, tz: ZoneInfo, now: datetime | None = None) -> tuple[datetime, datetime]:
    now = ensure_utc(now or datetime.now(UTC))
    today = now.astimezone(tz).date()
    if dr.preset:
        p = dr.preset
        if p == "today":
            start, end = day_range_utc(today, today, tz)
        elif p == "yesterday":
            y = today - timedelta(days=1)
            start, end = day_range_utc(y, y, tz)
        elif p in ("last_7_days", "last_14_days", "last_30_days", "last_90_days"):
            n = int(p.split("_")[1])
            start, end = day_range_utc(today - timedelta(days=n - 1), today, tz)
        elif p == "this_month":
            start, end = day_range_utc(today.replace(day=1), today, tz)
        else:  # last_month
            first_this = today.replace(day=1)
            last_prev = first_this - timedelta(days=1)
            start, end = day_range_utc(last_prev.replace(day=1), last_prev, tz)
    else:
        if not dr.from_:
            raise APIError(
                "date_range needs 'from' (and optionally 'to') or a preset", code="INVALID_DATE_RANGE", status_code=422
            )
        start = _parse(dr.from_, tz, end=False)
        end = _parse(dr.to, tz, end=True) if dr.to else local_midnight_utc(today + timedelta(days=1), tz)
    if end <= start:
        raise APIError("date_range end must be after its start", code="INVALID_DATE_RANGE", status_code=422)
    if end - start > timedelta(days=get_settings().max_query_range_days):
        raise APIError(
            f"date_range may span at most {get_settings().max_query_range_days} days",
            code="DATE_RANGE_TOO_LARGE",
            status_code=422,
        )
    return start, end


def previous_period(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    """The equally long period immediately before [start, end)."""
    return start - (end - start), start
