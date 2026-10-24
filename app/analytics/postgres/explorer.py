from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.postgres.common import iso
from app.analytics.postgres.filters import (
    BUILTIN_COLUMNS,
    PAGEVIEW_EVENTS,
    _like_escape,
    filter_conditions,
    person_only_conditions,
)
from app.analytics.postgres.sessions import person_session_counts
from app.analytics.queries import Filter, Scope
from app.core.errors import APIError
from app.db.session import get_sessionmaker
from app.models import Event, EventDefinition, Person, PersonDistinctId, PropertyDefinition


def encode_cursor(*parts: Any) -> str:
    return base64.urlsafe_b64encode(json.dumps([str(p) for p in parts]).encode()).decode()


def decode_cursor(cursor: str) -> list[str]:
    try:
        return json.loads(base64.urlsafe_b64decode(cursor.encode()))
    except Exception:
        raise APIError("Invalid cursor", code="INVALID_CURSOR", status_code=400) from None


def event_dict(e: Event) -> dict[str, Any]:
    return {
        "id": str(e.id),
        "event": e.event,
        "timestamp": e.timestamp.isoformat(),
        "received_at": e.received_at.isoformat(),
        "distinct_id": e.distinct_id,
        "anonymous_id": e.anonymous_id,
        "person_id": str(e.person_id),
        "insert_id": e.insert_id,
        "properties": e.properties,
        "path": e.path,
        "hostname": e.hostname,
        "device": {
            "browser": e.browser,
            "browser_version": e.browser_version,
            "os": e.os,
            "os_version": e.os_version,
            "device_type": e.device_type,
        },
        "country": e.country,
        "ip": str(e.ip) if e.ip else None,
        "user_agent": e.user_agent,
    }


def _event_conds(scope: Scope, filters: list[Filter], names: list[str] | None) -> list[Any]:
    conds: list[Any] = [
        Event.project_id == scope.project_id,
        Event.timestamp >= scope.date_from,
        Event.timestamp < scope.date_to,
    ]
    if names:
        conds.append(Event.event.in_(names))
    return conds + filter_conditions(scope, Event, filters)


async def list_events(
    db: AsyncSession,
    scope: Scope,
    *,
    filters: list[Filter],
    event_names: list[str] | None,
    person_id: UUID | None,
    distinct_id: str | None,
    search: str | None,
    cursor: str | None,
    limit: int,
    ascending: bool,
) -> tuple[list[dict[str, Any]], str | None]:
    conds = _event_conds(scope, filters, event_names)
    if person_id:
        conds.append(Event.person_id == person_id)
    if distinct_id:
        conds.append(Event.distinct_id == distinct_id)
    if search:
        like = f"%{_like_escape(search)}%"
        conds.append(
            or_(
                Event.event.ilike(like, escape="\\"),
                Event.distinct_id.ilike(like, escape="\\"),
                Event.path.ilike(like, escape="\\"),
            )
        )
    key = tuple_(Event.timestamp, Event.id)
    if cursor:
        ts, eid = decode_cursor(cursor)
        pair = tuple_(datetime.fromisoformat(ts), UUID(eid))
        conds.append(key > pair if ascending else key < pair)
    order = (Event.timestamp.asc(), Event.id.asc()) if ascending else (Event.timestamp.desc(), Event.id.desc())
    rows = (await db.execute(select(Event).where(*conds).order_by(*order).limit(limit + 1))).scalars().all()
    nxt = None
    if len(rows) > limit:
        rows = rows[:limit]
        nxt = encode_cursor(rows[-1].timestamp.isoformat(), rows[-1].id)
    return [event_dict(e) for e in rows], nxt


async def get_event(db: AsyncSession, project_id: UUID, event_id: UUID) -> dict[str, Any] | None:
    e = (
        await db.execute(select(Event).where(Event.project_id == project_id, Event.id == event_id))
    ).scalar_one_or_none()
    if e is None:
        return None
    out = event_dict(e)
    p = await db.get(Person, e.person_id)
    out["person"] = (
        {
            "id": str(p.id),
            "properties": p.properties,
            "first_seen": p.first_seen.isoformat(),
            "last_seen": p.last_seen.isoformat(),
        }
        if p
        else None
    )
    return out


# ---- persons -------------------------------------------------------------------------------------
async def _enrich(db: AsyncSession, scope: Scope, persons: list[Person]) -> list[dict[str, Any]]:
    ids = [p.id for p in persons]
    if not ids:
        return []
    counts = dict(
        (
            await db.execute(
                select(Event.person_id, func.count())
                .where(Event.project_id == scope.project_id, Event.person_id.in_(ids))
                .group_by(Event.person_id)
            )
        ).all()
    )
    latest = {
        r.person_id: r
        for r in (
            await db.execute(
                select(Event.person_id, Event.country, Event.browser, Event.os, Event.device_type)
                .where(Event.project_id == scope.project_id, Event.person_id.in_(ids))
                .distinct(Event.person_id)
                .order_by(Event.person_id, Event.timestamp.desc())
            )
        ).all()
    }
    dids: dict[Any, list[str]] = {}
    for pid, d in (
        await db.execute(
            select(PersonDistinctId.person_id, PersonDistinctId.distinct_id)
            .where(PersonDistinctId.person_id.in_(ids))
            .order_by(PersonDistinctId.created_at)
        )
    ).all():
        dids.setdefault(pid, []).append(d)
    sessions = await person_session_counts(db, scope, ids)
    out = []
    for p in persons:
        last = latest.get(p.id)
        out.append(
            {
                "id": str(p.id),
                "distinct_ids": dids.get(p.id, []),
                "email": (p.properties or {}).get("email"),
                "name": (p.properties or {}).get("name"),
                "is_identified": p.is_identified,
                "properties": p.properties,
                "first_seen": p.first_seen.isoformat(),
                "last_seen": p.last_seen.isoformat(),
                "event_count": int(counts.get(p.id, 0)),
                "sessions": int(sessions.get(p.id, 0)),
                "country": last.country if last else None,
                "browser": last.browser if last else None,
                "os": last.os if last else None,
                "device_type": last.device_type if last else None,
            }
        )
    return out


async def list_persons(
    db: AsyncSession,
    scope: Scope,
    *,
    filters: list[Filter],
    search: str | None,
    cursor: str | None,
    limit: int,
    sort: str,
) -> tuple[list[dict[str, Any]], str | None]:
    col = Person.first_seen if sort == "first_seen" else Person.last_seen
    conds: list[Any] = [Person.project_id == scope.project_id, *person_only_conditions(scope, filters)]
    if search:
        like = f"%{_like_escape(search)}%"
        conds.append(
            or_(
                Person.properties["email"].astext.ilike(like, escape="\\"),
                Person.properties["name"].astext.ilike(like, escape="\\"),
                Person.id.in_(
                    select(PersonDistinctId.person_id).where(
                        PersonDistinctId.project_id == scope.project_id,
                        PersonDistinctId.distinct_id.ilike(like, escape="\\"),
                    )
                ),
            )
        )
    if cursor:
        ts, pid = decode_cursor(cursor)
        conds.append(tuple_(col, Person.id) < tuple_(datetime.fromisoformat(ts), UUID(pid)))
    rows = (
        (await db.execute(select(Person).where(*conds).order_by(col.desc(), Person.id.desc()).limit(limit + 1)))
        .scalars()
        .all()
    )
    nxt = None
    if len(rows) > limit:
        rows = rows[:limit]
        nxt = encode_cursor(
            (rows[-1].first_seen if sort == "first_seen" else rows[-1].last_seen).isoformat(), rows[-1].id
        )
    return await _enrich(db, scope, list(rows)), nxt


async def get_person(db: AsyncSession, scope: Scope, person_id: UUID) -> dict[str, Any] | None:
    p = (
        await db.execute(select(Person).where(Person.id == person_id, Person.project_id == scope.project_id))
    ).scalar_one_or_none()
    if p is None:
        return None
    return (await _enrich(db, scope, [p]))[0]


# ---- definitions ---------------------------------------------------------------------------------
async def event_definitions(db: AsyncSession, project_id: UUID) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(EventDefinition).where(EventDefinition.project_id == project_id).order_by(EventDefinition.name)
        )
    ).scalars()
    return [{"name": r.name, "first_seen": iso(r.first_seen), "last_seen": iso(r.last_seen)} for r in rows]


async def property_definitions(db: AsyncSession, project_id: UUID, kind: str | None) -> list[dict[str, Any]]:
    q = select(PropertyDefinition).where(PropertyDefinition.project_id == project_id).order_by(PropertyDefinition.name)
    if kind:
        q = q.where(PropertyDefinition.kind == kind)
    out = [{"name": r.name, "kind": r.kind, "value_type": r.value_type} for r in (await db.execute(q)).scalars()]
    if kind in (None, "event"):
        out += [{"name": n, "kind": "event", "value_type": "string", "builtin": True} for n in sorted(BUILTIN_COLUMNS)]
    return out


async def property_values(
    db: AsyncSession, scope: Scope, name: str, kind: str, event: str | None, search: str | None, limit: int
) -> list[dict[str, Any]]:
    if kind == "person":
        col = Person.properties[name].astext
        q = select(col, func.count()).where(Person.project_id == scope.project_id, col.is_not(None)).group_by(col)
    else:
        col = getattr(Event, BUILTIN_COLUMNS[name]) if name in BUILTIN_COLUMNS else Event.properties[name].astext
        sample = select(Event.id).where(
            Event.project_id == scope.project_id, Event.timestamp >= scope.date_from, Event.timestamp < scope.date_to
        )
        if event:
            sample = sample.where(Event.event == event)
        q = select(col, func.count()).where(Event.id.in_(sample.limit(20000)), col.is_not(None)).group_by(col)
    if search:
        q = q.where(col.ilike(f"%{_like_escape(search)}%", escape="\\"))
    rows = (await db.execute(q.order_by(func.count().desc()).limit(limit))).all()
    return [{"value": v, "count": int(n)} for v, n in rows]


# ---- streaming export ----------------------------------------------------------------------------
async def stream_events(
    scope: Scope, *, filters: list[Filter], event_names: list[str] | None
) -> AsyncIterator[dict[str, Any]]:
    async with get_sessionmaker()() as db:
        q = select(Event).where(*_event_conds(scope, filters, event_names)).order_by(Event.timestamp, Event.id)
        result = await db.stream(q.execution_options(yield_per=2000))
        async for (e,) in result:
            yield event_dict(e)


async def stream_persons(scope: Scope, *, filters: list[Filter]) -> AsyncIterator[dict[str, Any]]:
    async with get_sessionmaker()() as db:
        q = (
            select(Person)
            .where(Person.project_id == scope.project_id, *person_only_conditions(scope, filters))
            .order_by(Person.first_seen, Person.id)
        )
        result = await db.stream(q.execution_options(yield_per=2000))
        async for (p,) in result:
            yield {
                "id": str(p.id),
                "email": (p.properties or {}).get("email"),
                "is_identified": p.is_identified,
                "first_seen": p.first_seen.isoformat(),
                "last_seen": p.last_seen.isoformat(),
                "properties": p.properties,
            }


_ = PAGEVIEW_EVENTS
