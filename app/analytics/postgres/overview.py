from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.postgres import retention as ret_mod
from app.analytics.postgres import sessions as sess_mod
from app.analytics.postgres.common import bucket_expr, bucket_labels
from app.analytics.postgres.filters import PAGEVIEW_EVENTS
from app.analytics.queries import EventRef, RetentionQuery, Scope
from app.models import Event, Person


async def _kpis(db: AsyncSession, scope: Scope) -> dict[str, Any]:
    in_range = [
        Event.project_id == scope.project_id,
        Event.timestamp >= scope.date_from,
        Event.timestamp < scope.date_to,
    ]
    events, active = (
        await db.execute(select(func.count(), func.count(func.distinct(Event.person_id))).where(*in_range))
    ).one()
    new_users = (
        await db.execute(
            select(func.count())
            .select_from(Person)
            .where(
                Person.project_id == scope.project_id,
                Person.first_seen >= scope.date_from,
                Person.first_seen < scope.date_to,
            )
        )
    ).scalar_one()
    total_users = (
        await db.execute(
            select(func.count())
            .select_from(Person)
            .where(Person.project_id == scope.project_id, Person.first_seen < scope.date_to)
        )
    ).scalar_one()
    sess = await sess_mod.summary(db, scope, [])
    return {
        "total_users": total_users,
        "active_users": active,
        "new_users": new_users,
        "total_events": events,
        "sessions": sess.sessions,
        "events_per_user": (events / active) if active else 0.0,
        "avg_session_duration_seconds": sess.avg_duration_seconds,
    }


async def overview(db: AsyncSession, scope: Scope, interval: str, previous: Scope | None) -> dict[str, Any]:
    in_range = [
        Event.project_id == scope.project_id,
        Event.timestamp >= scope.date_from,
        Event.timestamp < scope.date_to,
    ]
    buckets, labels = bucket_labels(scope.date_from, scope.date_to, interval, scope.timezone)
    pos = {b: i for i, b in enumerate(buckets)}
    b_ev = bucket_expr(interval, scope.timezone, Event.timestamp)
    volume = [0.0] * len(buckets)
    active = [0.0] * len(buckets)
    for b, n, u in (
        await db.execute(
            select(b_ev, func.count(), func.count(func.distinct(Event.person_id))).where(*in_range).group_by(b_ev)
        )
    ).all():
        if b in pos:
            volume[pos[b]], active[pos[b]] = float(n), float(u)
    b_p = bucket_expr(interval, scope.timezone, Person.first_seen)
    new = [0.0] * len(buckets)
    for b, n in (
        await db.execute(
            select(b_p, func.count())
            .where(
                Person.project_id == scope.project_id,
                Person.first_seen >= scope.date_from,
                Person.first_seen < scope.date_to,
            )
            .group_by(b_p)
        )
    ).all():
        if b in pos:
            new[pos[b]] = float(n)
    before = (
        await db.execute(
            select(func.count())
            .select_from(Person)
            .where(Person.project_id == scope.project_id, Person.first_seen < scope.date_from)
        )
    ).scalar_one()
    cumulative, running = [], float(before)
    for n in new:
        running += n
        cumulative.append(running)

    top_events = [
        {"event": e, "count": int(n), "unique_users": int(u)}
        for e, n, u in (
            await db.execute(
                select(Event.event, func.count(), func.count(func.distinct(Event.person_id)))
                .where(*in_range)
                .group_by(Event.event)
                .order_by(func.count().desc())
                .limit(10)
            )
        ).all()
    ]
    pv = [*in_range, Event.event.in_(PAGEVIEW_EVENTS), Event.path.is_not(None)]
    top_pages = [
        {"path": p, "views": int(n), "unique_visitors": int(u), "avg_views_per_user": (n / u) if u else 0.0}
        for p, n, u in (
            await db.execute(
                select(Event.path, func.count(), func.count(func.distinct(Event.person_id)))
                .where(*pv)
                .group_by(Event.path)
                .order_by(func.count().desc())
                .limit(10)
            )
        ).all()
    ]
    retention = await ret_mod.run_retention(
        db,
        scope,
        RetentionQuery(
            initial_event=EventRef(),
            return_event=EventRef(),
            interval="day",
            offsets=[0, 1, 2, 3, 4, 5, 6, 7],
            timezone=scope.timezone,
        ),
    )  # the range comes from `scope`
    return {
        "kpis": await _kpis(db, scope),
        "previous_kpis": await _kpis(db, previous) if previous else None,
        "interval": interval,
        "labels": labels,
        "event_volume": volume,
        "active_users_series": active,
        "new_users_series": new,
        "user_growth_series": cumulative,
        "top_events": top_events,
        "top_pages": top_pages,
        "retention": retention.model_dump(),
    }
