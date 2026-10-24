from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.queries import DateRange, Filter
from app.api.deps import current_user
from app.api.v1.analytics import service_for
from app.core.errors import APIError, NotFound
from app.db.session import get_session
from app.models import User

router = APIRouter(tags=["explorer"])
_filters_adapter = TypeAdapter(list[Filter])


def parse_filters(raw: str | None) -> list:  # type: ignore[type-arg]
    if not raw:
        return []
    try:
        return _filters_adapter.validate_python(json.loads(raw))
    except (ValueError, ValidationError) as exc:
        raise APIError(f"Invalid filters: {exc}", code="INVALID_FILTER", status_code=422) from exc


def _range(date_from: str | None, date_to: str | None, preset: str | None) -> DateRange:
    if preset:
        return DateRange(preset=preset)  # type: ignore[arg-type]
    if date_from:
        return DateRange(**{"from": date_from, "to": date_to})
    return DateRange(preset="last_7_days")


@router.get("/events", summary="Search raw events (cursor pagination)")
async def list_events(
    project_id: UUID, event: list[str] | None = Query(None), distinct_id: str | None = None, person_id: UUID | None = None,
    search: str | None = Query(None, max_length=200), date_from: str | None = None, date_to: str | None = None,
    preset: str | None = None, timezone: str | None = None, filters: str | None = Query(None, description="JSON array of filters"),
    cursor: str | None = None, limit: int = Query(50, ge=1, le=200), order: str = Query("desc", pattern="^(asc|desc)$"),
    user: User = Depends(current_user), db: AsyncSession = Depends(get_session),
):
    svc = await service_for(db, user, project_id)
    flt = parse_filters(filters)
    scope = svc.scope(_range(date_from, date_to, preset), timezone, await svc._load_cohorts(flt))
    items, nxt = await svc.repo.list_events(scope, filters=flt, event_names=event, person_id=person_id, distinct_id=distinct_id,
                                            search=search, cursor=cursor, limit=limit, ascending=order == "asc")
    return {"items": items, "next_cursor": nxt}


@router.get("/events/{event_id}")
async def get_event(event_id: UUID, project_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    svc = await service_for(db, user, project_id)
    ev = await svc.repo.get_event(project_id, event_id)
    if ev is None:
        raise NotFound("Event not found")
    return ev


@router.get("/users", summary="List people (cursor pagination)")
async def list_users(
    project_id: UUID, search: str | None = Query(None, max_length=200), filters: str | None = None, cursor: str | None = None,
    limit: int = Query(50, ge=1, le=200), sort: str = Query("last_seen", pattern="^(last_seen|first_seen)$"),
    user: User = Depends(current_user), db: AsyncSession = Depends(get_session),
):
    svc = await service_for(db, user, project_id)
    flt = parse_filters(filters)
    scope = svc.scope(DateRange(preset="last_7_days"), None, await svc._load_cohorts(flt))
    items, nxt = await svc.repo.list_persons(scope, filters=flt, search=search, cursor=cursor, limit=limit, sort=sort)
    return {"items": items, "next_cursor": nxt}


async def _person_id_project(db: AsyncSession, person_id: UUID) -> UUID:
    from sqlalchemy import select

    from app.models import Person

    pid = (await db.execute(select(Person.project_id).where(Person.id == person_id))).scalar_one_or_none()
    if pid is None:
        raise NotFound("User not found")
    return pid


@router.get("/users/{person_id}", summary="User detail")
async def get_user(person_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    project_id = await _person_id_project(db, person_id)
    svc = await service_for(db, user, project_id)  # membership check happens here (404 for outsiders)
    scope = svc.scope(DateRange(preset="last_7_days"), None)
    p = await svc.repo.get_person(scope, person_id)
    if p is None:
        raise NotFound("User not found")
    return p


@router.get("/users/{person_id}/events", summary="Chronological event timeline of a user")
async def user_timeline(
    person_id: UUID, date_from: str | None = None, date_to: str | None = None, preset: str | None = None,
    timezone: str | None = None, cursor: str | None = None, limit: int = Query(100, ge=1, le=500),
    order: str = Query("desc", pattern="^(asc|desc)$"), user: User = Depends(current_user), db: AsyncSession = Depends(get_session),
):
    project_id = await _person_id_project(db, person_id)
    svc = await service_for(db, user, project_id)
    if date_from or preset:
        rng = _range(date_from, date_to, preset)
    else:  # whole history, bounded by the maximum query range
        from datetime import UTC, datetime, timedelta

        from app.core.config import get_settings

        rng = DateRange(**{"from": (datetime.now(UTC) - timedelta(days=get_settings().max_query_range_days - 2)).date().isoformat()})
    scope = svc.scope(rng, timezone)
    items, nxt = await svc.repo.list_events(scope, filters=[], event_names=None, person_id=person_id, distinct_id=None,
                                            search=None, cursor=cursor, limit=limit, ascending=order == "asc")
    return {"items": items, "next_cursor": nxt}


@router.get("/meta/event-definitions")
async def event_definitions(project_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    svc = await service_for(db, user, project_id)
    return await svc.repo.event_definitions(project_id)


@router.get("/meta/property-definitions")
async def property_definitions(project_id: UUID, kind: str | None = Query(None, pattern="^(event|person)$"),
                               user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    svc = await service_for(db, user, project_id)
    return await svc.repo.property_definitions(project_id, kind)


@router.get("/meta/property-values", summary="Top values of a property (for filter autocompletion)")
async def property_values(project_id: UUID, name: str = Query(max_length=200), kind: str = Query("event", pattern="^(event|person)$"),
                          event: str | None = None, search: str | None = Query(None, max_length=100),
                          limit: int = Query(20, ge=1, le=100), user: User = Depends(current_user),
                          db: AsyncSession = Depends(get_session)):
    svc = await service_for(db, user, project_id)
    scope = svc.scope(DateRange(preset="last_90_days"), None)
    return await svc.repo.property_values(scope, name, kind, event, search, limit)
