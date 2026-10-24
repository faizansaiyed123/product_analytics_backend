"""Orchestrates analytics queries: date/timezone resolution, cohort loading, Redis caching, previous-period comparison.

Caching behaviour (documented in docs/analytics.md): results are cached for QUERY_CACHE_TTL_SECONDS (default 60 s),
keyed by project + canonical query JSON + resolved date range (relative presets therefore roll over naturally).
Cached responses carry `cached: true` and `computed_at`; clients can force a fresh run with refresh=true. Late-arriving
events therefore appear at most one TTL later."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics import results as R
from app.analytics.dates import previous_period, resolve_range
from app.analytics.postgres.repository import PostgresAnalyticsRepository
from app.analytics.queries import (
    CohortDefinition,
    CohortFilter,
    DateRange,
    FunnelQuery,
    PathsQuery,
    RetentionQuery,
    Scope,
    SessionsQuery,
    TrendQuery,
)
from app.analytics.repository import AnalyticsRepository
from app.core.config import get_settings
from app.core.errors import APIError
from app.core.redis import get_redis
from app.core.timeutil import parse_tz
from app.models import Cohort
from app.schemas.projects import ProjectSettings

AnyQuery = TrendQuery | FunnelQuery | RetentionQuery | SessionsQuery | PathsQuery


def get_repository(db: AsyncSession) -> AnalyticsRepository:
    """Single switch point for the analytics backend (PostgreSQL today, ClickHouse later)."""
    return PostgresAnalyticsRepository(db)


def _collect_cohort_ids(filters: list[Any]) -> set[UUID]:
    return {f.cohort_id for f in filters if isinstance(f, CohortFilter)}


def query_filters(q: Any) -> list[Any]:
    out = list(getattr(q, "filters", []) or [])
    for s in getattr(q, "series", []) or []:
        out += s.filters
    for s in getattr(q, "steps", []) or []:
        out += s.filters
    for ref in (getattr(q, "initial_event", None), getattr(q, "return_event", None)):
        if ref is not None:
            out += ref.filters
    return out


class AnalyticsService:
    def __init__(self, db: AsyncSession, project_id: UUID, project_settings: ProjectSettings) -> None:
        self.db = db
        self.project_id = project_id
        self.ps = project_settings
        self.repo = get_repository(db)

    async def _load_cohorts(self, filters: list[Any]) -> dict[UUID, CohortDefinition]:
        ids = _collect_cohort_ids(filters)
        if not ids:
            return {}
        rows = (
            (await self.db.execute(select(Cohort).where(Cohort.project_id == self.project_id, Cohort.id.in_(ids))))
            .scalars()
            .all()
        )
        return {c.id: CohortDefinition.model_validate(c.definition) for c in rows}

    def scope(self, dr: DateRange, tz_name: str | None, cohorts: dict[UUID, CohortDefinition] | None = None) -> Scope:
        try:
            tz = parse_tz(tz_name or self.ps.timezone)
        except ValueError as exc:
            raise APIError(str(exc), code="INVALID_TIMEZONE", status_code=422) from exc
        now = datetime.now(UTC)
        start, end = resolve_range(dr, tz, now)
        return Scope(
            project_id=self.project_id,
            date_from=start,
            date_to=end,
            timezone=tz.key,
            session_timeout_minutes=self.ps.session_timeout_minutes,
            cohorts=cohorts or {},
            now=now,
        )

    async def _run(self, q: AnyQuery, scope: Scope, compare: bool) -> dict[str, Any]:
        prev_scope = None
        if compare:
            ps, pe = previous_period(scope.date_from, scope.date_to)
            prev_scope = scope.model_copy(update={"date_from": ps, "date_to": pe})
        if isinstance(q, TrendQuery):
            res = await self.repo.trend(scope, q)
            if compare and prev_scope:
                res.previous = await self.repo.trend(prev_scope, q)
            return res.model_dump()
        if isinstance(q, FunnelQuery):
            return (await self.repo.funnel(scope, q)).model_dump()
        if isinstance(q, RetentionQuery):
            return (await self.repo.retention(scope, q)).model_dump()
        if isinstance(q, SessionsQuery):
            return (await self.repo.sessions(scope, q, previous=prev_scope)).model_dump()
        if isinstance(q, PathsQuery):
            return (await self.repo.paths(scope, q)).model_dump()
        raise APIError("Unsupported query type", code="INVALID_QUERY", status_code=422)

    async def run_query(self, q: AnyQuery, *, refresh: bool = False, compare: bool | None = None) -> R.QueryEnvelope:
        started = time.perf_counter()
        cohorts = await self._load_cohorts(query_filters(q))
        scope = self.scope(q.date_range, q.timezone, cohorts)
        compare = bool(getattr(q, "compare_previous", False)) if compare is None else compare
        cache_key = (
            "qc:"
            + self.project_id.hex
            + ":"
            + hashlib.sha256(
                json.dumps(
                    {
                        "q": q.model_dump(mode="json", by_alias=True),
                        "from": scope.date_from.isoformat(),
                        "to": scope.date_to.isoformat(),
                        "tz": scope.timezone,
                        "cmp": compare,
                        "st": scope.session_timeout_minutes,
                        "cohorts": {
                            str(k): v.model_dump(mode="json")
                            for k, v in sorted(cohorts.items(), key=lambda kv: str(kv[0]))
                        },
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
        )
        ttl = get_settings().query_cache_ttl_seconds
        redis = None
        if ttl > 0:
            try:
                redis = get_redis()
                if not refresh:
                    hit = await redis.get(cache_key)
                    if hit:
                        env = json.loads(hit)
                        env["cached"] = True
                        return R.QueryEnvelope(**env)
            except Exception:
                redis = None
        result = await self._run(q, scope, compare)
        env = R.QueryEnvelope(
            query_type=q.type,
            result=result,
            cached=False,
            computed_at=datetime.now(UTC).isoformat(),
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        if redis is not None:
            try:
                await redis.set(cache_key, env.model_dump_json(), ex=ttl)
            except Exception:
                pass
        return env

    async def cohort_scope(self) -> Scope:
        now = datetime.now(UTC)
        return Scope(
            project_id=self.project_id,
            date_from=now,
            date_to=now,
            timezone="UTC",
            session_timeout_minutes=self.ps.session_timeout_minutes,
            now=now,
        )
