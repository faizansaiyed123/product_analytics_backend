"""Funnel calculation. Attribution rules are documented in docs/analytics.md:

* A person *enters* the funnel with any step-1 event whose timestamp is inside the date range.
* For each entry (t1) the chain is built greedily: step k is the EARLIEST matching event at or after step k-1
  (strictly after when two consecutive steps use the same event name) and no later than t1 + conversion window.
* Steps must therefore happen in order; other events may occur in between.
* A person is counted once: the entry that reaches the deepest step wins (ties -> earliest entry).
* Later steps may fall after the date range end (up to the conversion window), never before the entry.
* Users are persons after identity resolution (anonymous + identified activity merges into one person).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.analytics import results as R
from app.analytics.postgres.common import iso
from app.analytics.postgres.filters import filter_conditions
from app.analytics.postgres.trends import breakdown_expr
from app.analytics.queries import FunnelQuery, Scope
from app.core.timeutil import utcnow  # noqa: F401
from app.models import Event


def _step_conditions(scope: Scope, ev: Any, q: FunnelQuery, i: int) -> list[Any]:
    step = q.steps[i]
    return [
        ev.project_id == scope.project_id,
        ev.event == step.event,
        *filter_conditions(scope, ev, [*q.filters, *step.filters]),
    ]


async def run_funnel(db: AsyncSession, scope: Scope, q: FunnelQuery) -> R.FunnelResult:
    n = len(q.steps)
    window = timedelta(seconds=q.conversion_window.seconds())
    aliases = [aliased(Event) for _ in range(n)]

    e1 = aliases[0]
    cols: list[Any] = [e1.person_id.label("person_id"), e1.timestamp.label("t1")]
    bkd_label = None
    if q.breakdown:
        # breakdown_expr is written against Event; re-express it on alias e1
        from sqlalchemy.orm import util as _u  # noqa: F401

        bkd_label = _breakdown_on(q, e1).label("bkd")
        cols.append(bkd_label)
    prev = (
        select(*cols)
        .where(*_step_conditions(scope, e1, q, 0), e1.timestamp >= scope.date_from, e1.timestamp < scope.date_to)
        .cte("c1")
    )
    for k in range(2, n + 1):
        ek = aliases[k - 1]
        prev_t = prev.c[f"t{k - 1}"]
        same_event = q.steps[k - 1].event == q.steps[k - 2].event
        ordered = ek.timestamp > prev_t if same_event else ek.timestamp >= prev_t
        keep = [prev.c.person_id, *[prev.c[f"t{j}"] for j in range(1, k)]]
        if q.breakdown:
            keep.append(prev.c.bkd)
        stmt = (
            select(*keep, func.min(ek.timestamp).label(f"t{k}"))
            .select_from(
                prev.outerjoin(
                    ek,
                    and_(
                        *_step_conditions(scope, ek, q, k - 1),
                        ek.person_id == prev.c.person_id,
                        ordered,
                        ek.timestamp <= prev.c.t1 + window,
                    ),
                )
            )
            .group_by(*keep)
        )
        prev = stmt.cte(f"c{k}")

    ts = [prev.c[f"t{k}"] for k in range(1, n + 1)]
    depth = sum((case((t.is_not(None), 1), else_=0) for t in ts[1:]), start=1)  # t1 is never null
    best_cols = [prev.c.person_id, *ts, depth.label("depth")]
    if q.breakdown:
        best_cols.append(prev.c.bkd)
    best = (
        select(*best_cols)
        .distinct(prev.c.person_id)
        .order_by(prev.c.person_id, depth.desc(), prev.c.t1)
        .subquery("best")
    )
    bt = [best.c[f"t{k}"] for k in range(1, n + 1)]
    aggs: list[Any] = [func.count().label("entered")]
    for k in range(2, n + 1):
        aggs.append(func.count().filter(best.c.depth >= k).label(f"n{k}"))
        gap = func.extract("epoch", bt[k - 1] - bt[k - 2])
        aggs.append(func.avg(gap).label(f"avg{k}"))
        aggs.append(func.percentile_cont(0.5).within_group(gap).label(f"med{k}"))
    if q.breakdown:
        rows = (
            await db.execute(
                select(best.c.bkd, *aggs).group_by(best.c.bkd).order_by(func.count().desc()).limit(q.breakdown.limit)
            )
        ).all()
    else:
        rows = (await db.execute(select(*aggs))).all()

    def steps_from(counts: list[int], avg: list[float | None], med: list[float | None]) -> list[R.FunnelStepResult]:
        out = []
        for i, step in enumerate(q.steps):
            c = counts[i]
            prev_c = counts[i - 1] if i else c
            out.append(
                R.FunnelStepResult(
                    order=i + 1,
                    event=step.event,
                    label=step.label or step.event,
                    count=c,
                    conversion_rate=(c / prev_c if prev_c else 0.0) if i else None,
                    dropoff_count=(prev_c - c) if i else 0,
                    dropoff_rate=((prev_c - c) / prev_c if prev_c else 0.0) if i else 0.0,
                    overall_conversion_rate=c / counts[0] if counts[0] else 0.0,
                    avg_time_to_convert_seconds=avg[i],
                    median_time_to_convert_seconds=med[i],
                )
            )
        return out

    def parse(row: Any, offset: int) -> tuple[list[int], list[float | None], list[float | None]]:
        vals = list(row)[offset:]
        counts = [int(vals[0])]
        avg: list[float | None] = [None]
        med: list[float | None] = [None]
        idx = 1
        for _ in range(2, n + 1):
            counts.append(int(vals[idx]))
            avg.append(float(vals[idx + 1]) if vals[idx + 1] is not None else None)
            med.append(float(vals[idx + 2]) if vals[idx + 2] is not None else None)
            idx += 3
        return counts, avg, med

    if q.breakdown:
        breakdowns = []
        totals = [0] * n
        for row in rows:
            counts, avg, med = parse(row, 1)
            st = steps_from(counts, avg, med)
            breakdowns.append(
                R.FunnelBreakdown(value=str(row[0]), steps=st, overall_conversion_rate=st[-1].overall_conversion_rate)
            )
            totals = [a + b for a, b in zip(totals, counts, strict=True)]
        overall = steps_from(totals, [None] * n, [None] * n)
        return R.FunnelResult(
            steps=overall,
            overall_conversion_rate=overall[-1].overall_conversion_rate,
            breakdowns=breakdowns,
            conversion_window_seconds=q.conversion_window.seconds(),
            date_from=iso(scope.date_from),
            date_to=iso(scope.date_to),
            timezone=scope.timezone,
        )
    counts, avg, med = parse(rows[0], 0)
    st = steps_from(counts, avg, med)
    return R.FunnelResult(
        steps=st,
        overall_conversion_rate=st[-1].overall_conversion_rate,
        conversion_window_seconds=q.conversion_window.seconds(),
        date_from=iso(scope.date_from),
        date_to=iso(scope.date_to),
        timezone=scope.timezone,
    )


def _breakdown_on(q: FunnelQuery, ev: Any):  # type: ignore[no-untyped-def]
    """Breakdown expression evaluated on the first step's event alias."""
    from sqlalchemy import select as _select

    from app.analytics.postgres.filters import BUILTIN_COLUMNS, person_prop
    from app.models import Person

    b = q.breakdown
    assert b is not None
    if b.type == "person":
        inner = _select(person_prop(b.property)).where(Person.id == ev.person_id).correlate(ev).scalar_subquery()
        return func.coalesce(inner, "(none)")
    col = (
        getattr(ev, BUILTIN_COLUMNS[b.property]) if b.property in BUILTIN_COLUMNS else ev.properties[b.property].astext
    )
    return func.coalesce(col, "(none)")


_ = breakdown_expr
