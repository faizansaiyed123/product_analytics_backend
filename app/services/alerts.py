"""Alert evaluation (run by the worker; also callable on demand).

An alert looks at the most recent COMPLETED period (hour/day/week in the project timezone) of a metric. Operators:
lt/lte/gt/gte compare that value with the threshold; decrease_pct/increase_pct compare it with the period before.
An in-app notification (and an optional webhook) is created only when the alert changes from ok to firing."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.queries import Scope, TrendQuery
from app.analytics.repository import AnalyticsRepository
from app.core.config import get_settings
from app.core.errors import APIError
from app.core.timeutil import utcnow
from app.models import Alert, Notification
from app.schemas.projects import ProjectSettings
from app.services.analytics_service import get_repository

logger = logging.getLogger("app.alerts")


async def validate_webhook_url(url: str) -> None:
    """Reject non-http(s) URLs and (unless WEBHOOK_ALLOW_PRIVATE) hosts that resolve to private/loopback addresses (SSRF)."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise APIError("webhook_url must be an http(s) URL", code="INVALID_WEBHOOK", status_code=422)
    if get_settings().webhook_allow_private:
        return
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(p.hostname, p.port or 443, type=socket.SOCK_STREAM)
    except OSError:
        raise APIError("webhook host could not be resolved", code="INVALID_WEBHOOK", status_code=422) from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise APIError("webhook host resolves to a private address", code="INVALID_WEBHOOK", status_code=422)


def period_bounds(period: str, tz: ZoneInfo, now: datetime) -> tuple[datetime, datetime, datetime, datetime]:
    """(cur_start, cur_end, prev_start, prev_end) for the last two COMPLETED periods, in UTC."""
    local = now.astimezone(tz)
    if period == "hour":
        end = local.replace(minute=0, second=0, microsecond=0)
        step = timedelta(hours=1)
    elif period == "day":
        end = local.replace(hour=0, minute=0, second=0, microsecond=0)
        step = timedelta(days=1)
    else:
        end = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        step = timedelta(weeks=1)
    s = end - step
    ps = end - 2 * step
    return s.astimezone(UTC), end.astimezone(UTC), ps.astimezone(UTC), s.astimezone(UTC)


async def _value(repo: AnalyticsRepository, alert: Alert, scope: Scope, start: datetime, end: datetime) -> float:
    q = alert.query
    scope = scope.model_copy(update={"date_from": start, "date_to": end})
    tq = TrendQuery.model_validate({"event": q["event"], "metric": q.get("metric", "total_events"),
                                    "metric_property": q.get("metric_property"), "filters": q.get("filters", [])})
    res = await repo.trend(scope, tq)
    return float(res.series[0].total or 0)


def is_firing(op: str, threshold: float, value: float, previous: float | None) -> bool:
    if op == "lt":
        return value < threshold
    if op == "lte":
        return value <= threshold
    if op == "gt":
        return value > threshold
    if op == "gte":
        return value >= threshold
    if previous is None or previous == 0:
        return False  # no baseline to compare with
    change = (value - previous) / previous * 100
    return change <= -threshold if op == "decrease_pct" else change >= threshold


async def evaluate_alert(db: AsyncSession, alert: Alert, ps: ProjectSettings, *, deliver_webhook: bool = True) -> bool:
    now = utcnow()
    tz = ZoneInfo(ps.timezone)
    cs, ce, pst, pe = period_bounds(alert.period, tz, now)
    repo = get_repository(db)
    scope = Scope(project_id=alert.project_id, date_from=cs, date_to=ce, timezone=tz.key, now=now)
    try:
        value = await _value(repo, alert, scope, cs, ce)
        op = alert.condition["operator"]
        prev = await _value(repo, alert, scope, pst, pe) if op in ("decrease_pct", "increase_pct") else None
        firing = is_firing(op, float(alert.condition["value"]), value, prev)
        alert.last_error = None
    except Exception as exc:
        logger.exception("alert evaluation failed", extra={"alert_id": str(alert.id)})
        alert.last_error = str(exc)[:500]
        alert.last_evaluated_at = now
        return False
    was = alert.state
    alert.last_value = value
    alert.last_evaluated_at = now
    alert.state = "firing" if firing else "ok"
    if firing and was != "firing":
        alert.last_triggered_at = now
        title = f"Alert: {alert.name}"
        body = (f"{alert.query['metric']} of '{alert.query['event']}' was {value:g} in the last completed {alert.period} "
                f"(condition: {alert.condition['operator']} {alert.condition['value']:g}"
                + (f", previous {prev:g}" if prev is not None else "") + ")")
        db.add(Notification(project_id=alert.project_id, alert_id=alert.id, kind="alert", title=title, body=body,
                            data={"value": value, "previous": prev, "period_start": cs.isoformat(), "period_end": ce.isoformat()}))
        if deliver_webhook and alert.webhook_url:
            await _post_webhook(alert, title, body, value)
    return firing


async def _post_webhook(alert: Alert, title: str, body: str, value: float) -> None:
    try:
        await validate_webhook_url(alert.webhook_url or "")
        async with httpx.AsyncClient(timeout=5, follow_redirects=False) as c:
            r = await c.post(alert.webhook_url, json={"alert_id": str(alert.id), "name": alert.name, "title": title,
                                                       "message": body, "value": value, "state": "firing"})
            if r.status_code >= 400:
                alert.last_error = f"webhook returned HTTP {r.status_code}"
    except Exception as exc:
        alert.last_error = f"webhook failed: {str(exc)[:200]}"
