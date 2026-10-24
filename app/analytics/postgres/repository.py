"""PostgreSQL implementation of AnalyticsRepository."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics import results as R
from app.analytics.postgres import explorer, retention, sessions, trends
from app.analytics.postgres import overview as overview_mod
from app.analytics.postgres.filters import cohort_person_ids, filter_conditions
from app.analytics.postgres.funnels import run_funnel
from app.analytics.queries import (
    CohortDefinition,
    Filter,
    FunnelQuery,
    PathsQuery,
    RetentionQuery,
    Scope,
    SessionsQuery,
    TrendQuery,
)
from app.analytics.repository import AnalyticsRepository
from app.models import Event, Person


class PostgresAnalyticsRepository(AnalyticsRepository):
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _base(self, scope: Scope, event: str | None, filters: list[Filter] | None) -> list[Any]:
        conds: list[Any] = [
            Event.project_id == scope.project_id,
            Event.timestamp >= scope.date_from,
            Event.timestamp < scope.date_to,
        ]
        if event and event != "*":
            conds.append(Event.event == event)
        return conds + filter_conditions(scope, Event, filters or [])

    async def count_events(self, scope: Scope, event: str | None = None, filters: list[Filter] | None = None) -> int:
        return int((await self.db.execute(select(func.count()).where(*self._base(scope, event, filters)))).scalar_one())

    async def unique_users(self, scope: Scope, event: str | None = None, filters: list[Filter] | None = None) -> int:
        return int(
            (
                await self.db.execute(
                    select(func.count(func.distinct(Event.person_id))).where(*self._base(scope, event, filters))
                )
            ).scalar_one()
        )

    async def trend(self, scope: Scope, query: TrendQuery) -> R.TrendResult:
        return await trends.run_trend(self.db, scope, query)

    async def funnel(self, scope: Scope, query: FunnelQuery) -> R.FunnelResult:
        return await run_funnel(self.db, scope, query)

    async def retention(self, scope: Scope, query: RetentionQuery) -> R.RetentionResult:
        return await retention.run_retention(self.db, scope, query)

    async def sessions(self, scope: Scope, query: SessionsQuery, *, previous: Scope | None = None) -> R.SessionsResult:
        return await sessions.run_sessions(self.db, scope, query, previous)

    async def sessions_list(self, scope: Scope, filters: list[Filter], limit: int, offset: int) -> list[dict[str, Any]]:
        return await sessions.list_sessions(self.db, scope, filters, limit, offset)

    async def paths(self, scope: Scope, query: PathsQuery) -> R.PathsResult:
        return await sessions.run_paths(self.db, scope, query)

    async def cohort_size(self, scope: Scope, definition: CohortDefinition) -> int:
        sub = cohort_person_ids(scope, definition).subquery()
        return int((await self.db.execute(select(func.count()).select_from(sub))).scalar_one())

    async def cohort_members(
        self, scope: Scope, definition: CohortDefinition, limit: int, offset: int
    ) -> list[dict[str, Any]]:
        sub = cohort_person_ids(scope, definition)
        persons = (
            (
                await self.db.execute(
                    select(Person)
                    .where(Person.id.in_(sub))
                    .order_by(Person.last_seen.desc(), Person.id)
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
        return await explorer._enrich(self.db, scope, list(persons))

    async def overview(self, scope: Scope, interval: str, previous: Scope | None) -> dict[str, Any]:
        return await overview_mod.overview(self.db, scope, interval, previous)

    async def list_events(
        self,
        scope: Scope,
        *,
        filters: list[Filter],
        event_names: list[str] | None,
        person_id: UUID | None,
        distinct_id: str | None,
        search: str | None,
        cursor: str | None,
        limit: int,
        ascending: bool = False,
    ) -> tuple[list[dict[str, Any]], str | None]:
        return await explorer.list_events(
            self.db,
            scope,
            filters=filters,
            event_names=event_names,
            person_id=person_id,
            distinct_id=distinct_id,
            search=search,
            cursor=cursor,
            limit=limit,
            ascending=ascending,
        )

    async def get_event(self, project_id: UUID, event_id: UUID) -> dict[str, Any] | None:
        return await explorer.get_event(self.db, project_id, event_id)

    async def list_persons(
        self,
        scope: Scope,
        *,
        filters: list[Filter],
        search: str | None,
        cursor: str | None,
        limit: int,
        sort: str = "last_seen",
    ) -> tuple[list[dict[str, Any]], str | None]:
        return await explorer.list_persons(
            self.db, scope, filters=filters, search=search, cursor=cursor, limit=limit, sort=sort
        )

    async def get_person(self, scope: Scope, person_id: UUID) -> dict[str, Any] | None:
        return await explorer.get_person(self.db, scope, person_id)

    async def event_definitions(self, project_id: UUID) -> list[dict[str, Any]]:
        return await explorer.event_definitions(self.db, project_id)

    async def property_definitions(self, project_id: UUID, kind: str | None) -> list[dict[str, Any]]:
        return await explorer.property_definitions(self.db, project_id, kind)

    async def property_values(
        self, scope: Scope, name: str, kind: str, event: str | None, search: str | None, limit: int
    ) -> list[dict[str, Any]]:
        return await explorer.property_values(self.db, scope, name, kind, event, search, limit)

    def stream_events(
        self, scope: Scope, *, filters: list[Filter], event_names: list[str] | None
    ) -> AsyncIterator[dict[str, Any]]:
        return explorer.stream_events(scope, filters=filters, event_names=event_names)

    def stream_persons(self, scope: Scope, *, filters: list[Filter]) -> AsyncIterator[dict[str, Any]]:
        return explorer.stream_persons(scope, filters=filters)
