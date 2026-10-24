"""Retention. Definitions (docs/analytics.md):

* first_time cohort: a person belongs to the period of their FIRST EVER initial event, if that falls in the date range.
* recurring cohort: a person belongs to every period in the range in which they performed the initial event.
* A person is "retained at offset k" if they performed the return event in the period exactly k periods after their
  cohort period (exact-period retention, not "on or after").
* Periods are calendar buckets (day / ISO week starting Monday / month) in the query timezone.
* Offsets whose period has not started yet are reported as null, never as 0%.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import Integer, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics import results as R
from app.analytics.postgres.common import advance, bucket_expr, floor_local, iso
from app.analytics.postgres.filters import filter_conditions
from app.analytics.queries import EventRef, RetentionQuery, Scope
from app.models import Event


def _event_conds(scope: Scope, ref: EventRef, extra: list) -> list:  # type: ignore[type-arg]
    conds = [Event.project_id == scope.project_id]
    if ref.event != "*":
        conds.append(Event.event == ref.event)
    conds += filter_conditions(scope, Event, [*extra, *ref.filters])
    return conds


def _offset_expr(interval: str, ret_period, coh_period):  # type: ignore[no-untyped-def]
    if interval == "month":
        return (
            func.extract("year", ret_period) * 12
            + func.extract("month", ret_period)
            - func.extract("year", coh_period) * 12
            - func.extract("month", coh_period)
        )
    days = func.extract("epoch", ret_period - coh_period) / 86400
    return days if interval == "day" else days / 7


async def run_retention(db: AsyncSession, scope: Scope, q: RetentionQuery) -> R.RetentionResult:
    tz = scope.timezone
    offsets = q.offsets if q.offsets is not None else list(range(0, q.periods + 1))
    max_off = max(offsets) if offsets else 0
    now = scope.now or datetime.now(UTC)
    zone = ZoneInfo(tz)
    now_local = now.astimezone(zone).replace(tzinfo=None)
    current_bucket = floor_local(now_local, q.interval)

    init_conds = _event_conds(scope, q.initial_event, q.filters)
    period_of = bucket_expr(q.interval, tz, Event.timestamp)
    if q.cohort_type == "first_time":
        cohort = (
            select(
                Event.person_id.label("person_id"),
                bucket_expr(q.interval, tz, func.min(Event.timestamp)).label("period"),
            )
            .where(*init_conds)
            .group_by(Event.person_id)
            .having(func.min(Event.timestamp) >= scope.date_from, func.min(Event.timestamp) < scope.date_to)
        ).cte("cohort")
    else:
        cohort = (
            select(Event.person_id.label("person_id"), period_of.label("period"))
            .where(*init_conds, Event.timestamp >= scope.date_from, Event.timestamp < scope.date_to)
            .group_by(Event.person_id, period_of)
        ).cte("cohort")

    ret_conds = _event_conds(scope, q.return_event, [])
    returns = (
        select(Event.person_id.label("person_id"), period_of.label("period"))
        .where(*ret_conds, Event.timestamp >= scope.date_from, Event.timestamp <= now)
        .group_by(Event.person_id, period_of)
    ).cte("returns")

    off = cast(_offset_expr(q.interval, returns.c.period, cohort.c.period), Integer).label("off")
    size_rows = (
        await db.execute(select(cohort.c.period, func.count()).group_by(cohort.c.period).order_by(cohort.c.period))
    ).all()
    ret_rows = (
        await db.execute(
            select(cohort.c.period, off, func.count().label("n"))
            .select_from(cohort.join(returns, returns.c.person_id == cohort.c.person_id))
            .where(returns.c.period >= cohort.c.period)
            .group_by(cohort.c.period, off)
        )
    ).all()
    counts: dict[tuple[datetime, int], int] = {(p, o): n for p, o, n in ret_rows}

    cohorts: list[R.RetentionCohort] = []
    tot_num = {o: 0 for o in offsets}
    tot_den = {o: 0 for o in offsets}
    for period, size in size_rows:
        cells = []
        for o in offsets:
            complete = advance(period, q.interval, o) <= current_bucket
            if not complete or o > max_off:
                cells.append(R.RetentionCell(offset=o, count=None, rate=None))
                continue
            c = counts.get((period, o), 0)
            cells.append(R.RetentionCell(offset=o, count=c, rate=c / size if size else 0.0))
            tot_num[o] += c
            tot_den[o] += size
        cohorts.append(R.RetentionCohort(period=period.strftime("%Y-%m-%d"), size=size, cells=cells))
    average = [
        R.RetentionCell(
            offset=o, count=tot_num[o] if tot_den[o] else None, rate=(tot_num[o] / tot_den[o]) if tot_den[o] else None
        )
        for o in offsets
    ]
    return R.RetentionResult(
        interval=q.interval,
        offsets=offsets,
        cohorts=cohorts,
        average=average,
        date_from=iso(scope.date_from),
        date_to=iso(scope.date_to),
        timezone=tz,
    )
