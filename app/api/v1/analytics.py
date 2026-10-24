from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.dates import previous_period  # noqa: F401
from app.analytics.queries import (
    AnalyticsQuery,
    DateRange,
    Filter,
    FunnelQuery,
    PathsQuery,
    RetentionQuery,
    SessionsQuery,
    TrendQuery,
)
from app.analytics.results import QueryEnvelope
from app.api.deps import ProjectAccess, current_user, get_project_access
from app.core.errors import APIError
from app.core.permissions import READ
from app.db.session import get_session
from app.models import User
from app.services.analytics_service import AnalyticsService

router = APIRouter(prefix="/analytics", tags=["analytics"])


async def service_for(db: AsyncSession, user: User, project_id: UUID) -> AnalyticsService:
    acc: ProjectAccess = await get_project_access(db, user, project_id, READ)
    return AnalyticsService(db, acc.project.id, acc.settings)


class QueryRequest(BaseModel):
    project_id: UUID
    query: AnalyticsQuery
    refresh: bool = False


class TrendRequest(TrendQuery):
    project_id: UUID
    refresh: bool = False


class FunnelRequest(FunnelQuery):
    project_id: UUID
    refresh: bool = False


class RetentionRequest(RetentionQuery):
    project_id: UUID
    refresh: bool = False


class SessionsRequest(SessionsQuery):
    project_id: UUID
    compare_previous: bool = False
    refresh: bool = False


class PathsRequest(PathsQuery):
    project_id: UUID
    refresh: bool = False


def _strip(model: BaseModel, cls: type[BaseModel]) -> Any:
    data = model.model_dump(by_alias=True, exclude={"project_id", "refresh", "compare_previous"} if cls is SessionsQuery else {"project_id", "refresh"})
    return cls.model_validate(data)


@router.post("/query", response_model=QueryEnvelope, summary="Run any structured analytics query")
async def run_query(body: QueryRequest, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    svc = await service_for(db, user, body.project_id)
    return await svc.run_query(body.query, refresh=body.refresh)


@router.post("/trends", response_model=QueryEnvelope)
async def trends(body: TrendRequest, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    svc = await service_for(db, user, body.project_id)
    return await svc.run_query(_strip(body, TrendQuery), refresh=body.refresh)


@router.post("/funnels", response_model=QueryEnvelope)
async def funnels(body: FunnelRequest, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    svc = await service_for(db, user, body.project_id)
    return await svc.run_query(_strip(body, FunnelQuery), refresh=body.refresh)


@router.post("/retention", response_model=QueryEnvelope)
async def retention(body: RetentionRequest, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    svc = await service_for(db, user, body.project_id)
    return await svc.run_query(_strip(body, RetentionQuery), refresh=body.refresh)


@router.post("/sessions", response_model=QueryEnvelope)
async def sessions(body: SessionsRequest, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    svc = await service_for(db, user, body.project_id)
    return await svc.run_query(_strip(body, SessionsQuery), refresh=body.refresh, compare=body.compare_previous)


@router.post("/paths", response_model=QueryEnvelope)
async def paths(body: PathsRequest, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    svc = await service_for(db, user, body.project_id)
    return await svc.run_query(_strip(body, PathsQuery), refresh=body.refresh)


class SessionListRequest(BaseModel):
    project_id: UUID
    date_range: DateRange = Field(default_factory=lambda: DateRange(preset="last_7_days"))
    filters: list[Filter] = Field(default_factory=list, max_length=20)
    timezone: str | None = None
    limit: int = Field(50, ge=1, le=200)
    offset: int = Field(0, ge=0, le=100_000)


@router.post("/sessions/list", summary="Paginated list of individual sessions")
async def sessions_list(body: SessionListRequest, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    svc = await service_for(db, user, body.project_id)
    cohorts = await svc._load_cohorts(body.filters)
    scope = svc.scope(body.date_range, body.timezone, cohorts)
    items = await svc.repo.sessions_list(scope, body.filters, body.limit + 1, body.offset)
    more = len(items) > body.limit
    return {"items": items[: body.limit], "next_offset": body.offset + body.limit if more else None}


class OverviewRequest(BaseModel):
    project_id: UUID
    date_range: DateRange = Field(default_factory=lambda: DateRange(preset="last_7_days"))
    interval: str | None = Field(None, pattern="^(hour|day|week|month)$")
    compare_previous: bool = True
    timezone: str | None = None
    refresh: bool = False


@router.post("/overview", summary="Dashboard KPIs, volume, growth, top events/pages and retention overview")
async def overview(body: OverviewRequest, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    import hashlib
    import time
    from datetime import UTC, datetime

    from app.analytics.dates import previous_period as prev
    from app.core.config import get_settings
    from app.core.redis import get_redis

    started = time.perf_counter()
    svc = await service_for(db, user, body.project_id)
    scope = svc.scope(body.date_range, body.timezone)
    span_days = (scope.date_to - scope.date_from).days
    interval = body.interval or ("hour" if span_days <= 2 else "day" if span_days <= 120 else "week")
    key = "ov:" + body.project_id.hex + hashlib.sha256(
        f"{scope.date_from}|{scope.date_to}|{scope.timezone}|{interval}|{body.compare_previous}".encode()).hexdigest()
    ttl = get_settings().query_cache_ttl_seconds
    try:
        if ttl and not body.refresh and (hit := await get_redis().get(key)):
            data = json.loads(hit)
            data["cached"] = True
            return data
    except Exception:
        pass
    previous = None
    if body.compare_previous:
        ps, pe = prev(scope.date_from, scope.date_to)
        previous = scope.model_copy(update={"date_from": ps, "date_to": pe})
    data = await svc.repo.overview(scope, interval, previous)
    data.update({"date_from": scope.date_from.isoformat(), "date_to": scope.date_to.isoformat(), "timezone": scope.timezone,
                 "cached": False, "computed_at": datetime.now(UTC).isoformat(),
                 "duration_ms": round((time.perf_counter() - started) * 1000, 1)})
    try:
        if ttl:
            await get_redis().set(key, json.dumps(data, default=str), ex=ttl)
    except Exception:
        pass
    return data


_ = (Query, TypeAdapter, APIError)
