from __future__ import annotations

from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics import results as R
from app.analytics.postgres.common import bucket_expr, bucket_labels, iso, to_float
from app.analytics.postgres.filters import BUILTIN_COLUMNS, filter_conditions, numeric_expr, person_prop
from app.analytics.queries import Breakdown, Scope, TrendQuery, TrendSeries
from app.core.errors import APIError
from app.models import Event, Person

COUNT_LIKE = {"total_events", "unique_users", "sum"}
FREQ_LABELS = ["1", "2", "3", "4", "5", "6-10", "11+"]


def base_conditions(scope: Scope, series: TrendSeries, q: TrendQuery) -> list[Any]:
    conds: list[Any] = [
        Event.project_id == scope.project_id,
        Event.timestamp >= scope.date_from,
        Event.timestamp < scope.date_to,
    ]
    if series.event != "*":
        conds.append(Event.event == series.event)
    conds += filter_conditions(scope, Event, [*q.filters, *series.filters])
    return conds


def breakdown_expr(b: Breakdown):  # type: ignore[no-untyped-def]
    if b.type == "person":
        inner = select(person_prop(b.property)).where(Person.id == Event.person_id).correlate(Event).scalar_subquery()
        return func.coalesce(inner, "(none)")
    col = (
        getattr(Event, BUILTIN_COLUMNS[b.property])
        if b.property in BUILTIN_COLUMNS
        else Event.properties[b.property].astext
    )
    return func.coalesce(col, "(none)")


def metric_expr(q: TrendQuery):  # type: ignore[no-untyped-def]
    m = q.metric
    if m == "total_events":
        return func.count()
    if m == "unique_users":
        return func.count(func.distinct(Event.person_id))
    if m == "avg_events_per_user":
        return func.count() * 1.0 / func.nullif(func.count(func.distinct(Event.person_id)), 0)
    num = numeric_expr(Event.properties, q.metric_property or "")
    if m == "sum":
        return func.coalesce(func.sum(num), 0)
    if m == "avg":
        return func.avg(num)
    if m == "min":
        return func.min(num)
    if m == "max":
        return func.max(num)
    if m == "median":
        return func.percentile_cont(0.5).within_group(num)
    if m == "p90":
        return func.percentile_cont(0.9).within_group(num)
    raise APIError(f"Unsupported metric {m}", code="INVALID_QUERY", status_code=422)


def _series_label(s: TrendSeries, q: TrendQuery) -> str:
    name = s.label or ("All events" if s.event == "*" else s.event)
    if q.metric_property and q.metric in ("sum", "avg", "min", "max", "median", "p90"):
        return f"{q.metric} of {q.metric_property} ({name})"
    return name


async def run_trend(db: AsyncSession, scope: Scope, q: TrendQuery) -> R.TrendResult:
    if q.metric == "frequency":
        return await _frequency(db, scope, q)
    buckets, labels = bucket_labels(scope.date_from, scope.date_to, q.interval, scope.timezone)
    pos = {b: i for i, b in enumerate(buckets)}
    fill = 0.0 if q.metric in COUNT_LIKE or q.metric == "avg_events_per_user" else None
    out: list[R.SeriesResult] = []
    b_expr = bucket_expr(q.interval, scope.timezone, Event.timestamp)
    for s in q.series:
        conds = base_conditions(scope, s, q)
        metric = metric_expr(q)
        total = to_float((await db.execute(select(metric).where(*conds))).scalar_one())
        if q.breakdown:
            bkd = breakdown_expr(q.breakdown)
            rank = func.count(func.distinct(Event.person_id)) if q.metric == "unique_users" else func.count()
            top = (
                (
                    await db.execute(
                        select(bkd).where(*conds).group_by(bkd).order_by(rank.desc(), bkd).limit(q.breakdown.limit)
                    )
                )
                .scalars()
                .all()
            )
            rows = (
                await db.execute(select(b_expr, bkd, metric).where(*conds, bkd.in_(top)).group_by(b_expr, bkd))
            ).all()
            per: dict[str, list[float | None]] = {v: [fill] * len(buckets) for v in top}
            for b, v, val in rows:
                if b in pos:
                    per[v][pos[b]] = to_float(val)
            for v in top:
                sub_total = to_float((await db.execute(select(metric).where(*conds, bkd == v))).scalar_one())
                out.append(
                    R.SeriesResult(
                        label=f"{_series_label(s, q)} - {v}",
                        event=s.event,
                        breakdown_value=v,
                        data=per[v],
                        total=sub_total,
                    )
                )
        else:
            rows = (await db.execute(select(b_expr, metric).where(*conds).group_by(b_expr))).all()
            data: list[float | None] = [fill] * len(buckets)
            for b, val in rows:
                if b in pos:
                    data[pos[b]] = to_float(val)
            out.append(R.SeriesResult(label=_series_label(s, q), event=s.event, data=data, total=total))
    return R.TrendResult(
        interval=q.interval,
        labels=labels,
        series=out,
        date_from=iso(scope.date_from),
        date_to=iso(scope.date_to),
        timezone=scope.timezone,
    )


async def _frequency(db: AsyncSession, scope: Scope, q: TrendQuery) -> R.TrendResult:
    """Distribution: how many users performed the event 1, 2, 3, ... times in the range."""
    out: list[R.SeriesResult] = []
    for s in q.series:
        conds = base_conditions(scope, s, q)
        per_user = select(Event.person_id, func.count().label("n")).where(*conds).group_by(Event.person_id).subquery()
        n = per_user.c.n
        bucket = case(
            (n == 1, "1"), (n == 2, "2"), (n == 3, "3"), (n == 4, "4"), (n == 5, "5"), (n <= 10, "6-10"), else_="11+"
        )
        rows = dict((await db.execute(select(bucket, func.count()).group_by(bucket))).all())
        data = [float(rows.get(label, 0)) for label in FREQ_LABELS]
        out.append(R.SeriesResult(label=_series_label(s, q), event=s.event, data=data, total=sum(data)))
    return R.TrendResult(
        kind="distribution",
        interval=None,
        labels=FREQ_LABELS,
        series=out,
        date_from=iso(scope.date_from),
        date_to=iso(scope.date_to),
        timezone=scope.timezone,
    )
