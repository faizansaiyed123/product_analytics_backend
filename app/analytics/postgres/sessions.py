"""Sessions are computed at query time from raw events (so late-arriving events and identity merges are always
reflected): a new session starts for a person when the gap since their previous event exceeds the project's inactivity
timeout (default 30 min). Sessions belong to the period in which they START. We read a 24 h look-back before the range
start so a session already running at the range start is not counted as a new one."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Interval, case, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics import results as R
from app.analytics.postgres.common import bucket_expr, bucket_labels, iso, to_float
from app.analytics.postgres.filters import PAGEVIEW_EVENTS, filter_conditions
from app.analytics.queries import Filter, PathsQuery, Scope, SessionsQuery
from app.models import Event

LOOKBACK = timedelta(hours=24)


def session_rows(
    scope: Scope,
    filters: list[Filter],
    *,
    person_ids: list | None = None,
    extra_conds: list | None = None,  # type: ignore[type-arg]
    lookback: timedelta | None = LOOKBACK,
    with_rn: bool = False,
):  # type: ignore[no-untyped-def]
    """Subquery of events annotated with a per-person session number `sn`."""
    lo = scope.date_from - lookback if lookback is not None else None
    conds: list[Any] = [Event.project_id == scope.project_id, Event.timestamp < scope.date_to]
    if lo is not None:
        conds.append(Event.timestamp >= lo)
    if person_ids is not None:
        conds.append(Event.person_id.in_(person_ids))
    conds += filter_conditions(scope, Event, filters)
    conds += extra_conds or []
    base = (
        select(Event.person_id, Event.distinct_id, Event.timestamp, Event.event, Event.path).where(*conds).subquery("b")
    )
    w = dict(partition_by=base.c.person_id, order_by=base.c.timestamp)
    lagged = select(*base.c, func.lag(base.c.timestamp).over(**w).label("prev")).subquery("l")
    timeout = literal(timedelta(minutes=scope.session_timeout_minutes), Interval)
    marked = select(
        *lagged.c,
        case((or_(lagged.c.prev.is_(None), lagged.c.timestamp - lagged.c.prev > timeout), 1), else_=0).label("is_new"),
    ).subquery("m")
    return select(
        *marked.c,
        func.sum(marked.c.is_new).over(partition_by=marked.c.person_id, order_by=marked.c.timestamp).label("sn"),
    ).subquery("s")


def sessions_agg(scope: Scope, filters: list[Filter], **kw: Any):  # type: ignore[no-untyped-def]
    s = session_rows(scope, filters, **kw)
    is_pv = s.c.event.in_(PAGEVIEW_EVENTS)
    start = func.min(s.c.timestamp)
    q = (
        select(
            s.c.person_id,
            s.c.sn,
            start.label("start"),
            func.max(s.c.timestamp).label("end"),
            func.count().label("events"),
            func.count().filter(is_pv).label("pages"),
            (func.array_agg(s.c.distinct_id.op("ORDER BY")(s.c.timestamp)))[1].label("distinct_id"),
            (func.array_agg(s.c.path.op("ORDER BY")(s.c.timestamp)).filter(s.c.path.is_not(None)))[1].label(
                "entry_path"
            ),
            (func.array_agg(s.c.path.op("ORDER BY")(s.c.timestamp.desc())).filter(s.c.path.is_not(None)))[1].label(
                "exit_path"
            ),
        )
        .group_by(s.c.person_id, s.c.sn)
        .having(start >= scope.date_from)
    )
    return q.subquery("sess")


def _summary_from(row: Any) -> R.SessionSummary:
    n = int(row[0] or 0)
    return R.SessionSummary(
        sessions=n,
        avg_duration_seconds=to_float(row[1]),
        avg_events_per_session=to_float(row[2]),
        avg_pages_per_session=to_float(row[3]),
        bounce_rate=(int(row[4] or 0) / n if n else None),
        unique_users=int(row[5] or 0),
    )


async def summary(db: AsyncSession, scope: Scope, filters: list[Filter]) -> R.SessionSummary:
    ss = sessions_agg(scope, filters)
    row = (
        await db.execute(
            select(
                func.count(),
                func.avg(func.extract("epoch", ss.c.end - ss.c.start)),
                func.avg(ss.c.events),
                func.avg(ss.c.pages),
                func.count().filter(ss.c.events == 1),
                func.count(func.distinct(ss.c.person_id)),
            )
        )
    ).one()
    return _summary_from(row)


async def run_sessions(db: AsyncSession, scope: Scope, q: SessionsQuery, previous: Scope | None) -> R.SessionsResult:
    ss = sessions_agg(scope, q.filters)
    b = bucket_expr(q.interval, scope.timezone, ss.c.start)
    rows = (
        await db.execute(select(b, func.count(), func.avg(func.extract("epoch", ss.c.end - ss.c.start))).group_by(b))
    ).all()
    buckets, labels = bucket_labels(scope.date_from, scope.date_to, q.interval, scope.timezone)
    pos = {x: i for i, x in enumerate(buckets)}
    counts = [0.0] * len(buckets)
    durs: list[float | None] = [None] * len(buckets)
    for bk, n, avg in rows:
        if bk in pos:
            counts[pos[bk]], durs[pos[bk]] = float(n), to_float(avg)
    return R.SessionsResult(
        summary=await summary(db, scope, q.filters),
        interval=q.interval,
        labels=labels,
        sessions_series=counts,
        avg_duration_series=durs,
        date_from=iso(scope.date_from),
        date_to=iso(scope.date_to),
        timezone=scope.timezone,
        previous=await summary(db, previous, q.filters) if previous else None,
    )


async def list_sessions(
    db: AsyncSession, scope: Scope, filters: list[Filter], limit: int, offset: int
) -> list[dict[str, Any]]:
    ss = sessions_agg(scope, filters)
    rows = (await db.execute(select(ss).order_by(ss.c.start.desc(), ss.c.person_id).limit(limit).offset(offset))).all()
    return [
        {
            "person_id": str(r.person_id),
            "distinct_id": r.distinct_id,
            "start": r.start.isoformat(),
            "end": r.end.isoformat(),
            "duration_seconds": (r.end - r.start).total_seconds(),
            "events": r.events,
            "pages": r.pages,
            "entry_path": r.entry_path,
            "exit_path": r.exit_path,
        }
        for r in rows
    ]


async def person_session_counts(db: AsyncSession, scope: Scope, person_ids: list) -> dict:  # type: ignore[type-arg]
    if not person_ids:
        return {}
    s = session_rows(scope, [], person_ids=person_ids, lookback=None)
    rows = (await db.execute(select(s.c.person_id, func.count(func.distinct(s.c.sn))).group_by(s.c.person_id))).all()
    return {p: n for p, n in rows}


async def run_paths(db: AsyncSession, scope: Scope, q: PathsQuery) -> R.PathsResult:
    extra: list[Any] = []
    if q.path_type == "page":
        extra += [Event.event.in_(PAGEVIEW_EVENTS), Event.path.is_not(None)]
    s = session_rows(scope, q.filters, extra_conds=extra)
    name = s.c.path if q.path_type == "page" else s.c.event
    part = dict(partition_by=[s.c.person_id, s.c.sn], order_by=s.c.timestamp)
    r1 = select(
        s.c.person_id, s.c.sn, s.c.timestamp, name.label("name"), func.row_number().over(**part).label("rn")
    ).subquery("r1")
    if q.start_event:
        first = func.min(r1.c.rn).filter(r1.c.name == q.start_event).over(partition_by=[r1.c.person_id, r1.c.sn])
        r2 = select(r1.c.person_id, r1.c.sn, r1.c.timestamp, r1.c.name, r1.c.rn, first.label("first_rn")).subquery("r2")
        r3 = (
            select(r2.c.person_id, r2.c.sn, r2.c.timestamp, r2.c.name, (r2.c.rn - r2.c.first_rn + 1).label("step"))
            .where(r2.c.first_rn.is_not(None), r2.c.rn >= r2.c.first_rn)
            .subquery("r3")
        )
    else:
        r3 = select(r1.c.person_id, r1.c.sn, r1.c.timestamp, r1.c.name, r1.c.rn.label("step")).subquery("r3")
    nxt = func.lead(r3.c.name).over(partition_by=[r3.c.person_id, r3.c.sn], order_by=r3.c.timestamp)
    r4 = select(r3.c.step, r3.c.name.label("src"), nxt.label("dst"), r3.c.timestamp).subquery("r4")
    counted = (
        select(r4.c.step, r4.c.src, r4.c.dst, func.count().label("n"))
        .where(r4.c.dst.is_not(None), r4.c.step < q.depth, r4.c.timestamp >= scope.date_from)
        .group_by(r4.c.step, r4.c.src, r4.c.dst)
        .subquery("c")
    )
    ranked = select(
        *counted.c, func.row_number().over(partition_by=counted.c.step, order_by=counted.c.n.desc()).label("rk")
    ).subquery("k")
    rows = (
        await db.execute(
            select(ranked.c.step, ranked.c.src, ranked.c.dst, ranked.c.n)
            .where(ranked.c.rk <= q.max_nodes_per_step * 2)
            .order_by(ranked.c.step, ranked.c.n.desc())
        )
    ).all()
    total = (
        await db.execute(
            select(func.count()).select_from(
                select(r3.c.person_id, r3.c.sn).where(r3.c.step == 1, r3.c.timestamp >= scope.date_from).subquery()
            )
        )
    ).scalar_one()
    return R.PathsResult(
        edges=[R.PathEdge(step=int(a), source=b, target=c, count=int(n)) for a, b, c, n in rows],
        sessions=int(total),
        date_from=iso(scope.date_from),
        date_to=iso(scope.date_to),
        timezone=scope.timezone,
    )


_ = UTC, datetime
