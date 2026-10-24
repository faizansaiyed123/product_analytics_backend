"""Storage-agnostic analytics interface.

Business logic (services/analytics_service.py) only talks to this abstraction with typed query objects and gets typed
results back. PostgreSQL is the first implementation (analytics/postgres); a ClickHouse implementation can be added
by subclassing AnalyticsRepository without touching the API, services or frontend.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from app.analytics import results as R
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


class AnalyticsRepository(ABC):
    # ---- core insights ----
    @abstractmethod
    async def count_events(
        self, scope: Scope, event: str | None = None, filters: list[Filter] | None = None
    ) -> int: ...

    @abstractmethod
    async def unique_users(
        self, scope: Scope, event: str | None = None, filters: list[Filter] | None = None
    ) -> int: ...

    @abstractmethod
    async def trend(self, scope: Scope, query: TrendQuery) -> R.TrendResult: ...

    @abstractmethod
    async def funnel(self, scope: Scope, query: FunnelQuery) -> R.FunnelResult: ...

    @abstractmethod
    async def retention(self, scope: Scope, query: RetentionQuery) -> R.RetentionResult: ...

    @abstractmethod
    async def sessions(
        self, scope: Scope, query: SessionsQuery, *, previous: Scope | None = None
    ) -> R.SessionsResult: ...

    @abstractmethod
    async def sessions_list(
        self, scope: Scope, filters: list[Filter], limit: int, offset: int
    ) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def paths(self, scope: Scope, query: PathsQuery) -> R.PathsResult: ...

    # ---- cohorts ----
    @abstractmethod
    async def cohort_size(self, scope: Scope, definition: CohortDefinition) -> int: ...

    @abstractmethod
    async def cohort_members(
        self, scope: Scope, definition: CohortDefinition, limit: int, offset: int
    ) -> list[dict[str, Any]]: ...

    # ---- dashboards ----
    @abstractmethod
    async def overview(self, scope: Scope, interval: str, previous: Scope | None) -> dict[str, Any]: ...

    # ---- explorer ----
    @abstractmethod
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
    ) -> tuple[list[dict[str, Any]], str | None]: ...

    @abstractmethod
    async def get_event(self, project_id: UUID, event_id: UUID) -> dict[str, Any] | None: ...

    @abstractmethod
    async def list_persons(
        self,
        scope: Scope,
        *,
        filters: list[Filter],
        search: str | None,
        cursor: str | None,
        limit: int,
        sort: str = "last_seen",
    ) -> tuple[list[dict[str, Any]], str | None]: ...

    @abstractmethod
    async def get_person(self, scope: Scope, person_id: UUID) -> dict[str, Any] | None: ...

    @abstractmethod
    async def event_definitions(self, project_id: UUID) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def property_definitions(self, project_id: UUID, kind: str | None) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def property_values(
        self, scope: Scope, name: str, kind: str, event: str | None, search: str | None, limit: int
    ) -> list[dict[str, Any]]: ...

    # ---- export (streaming; never materialises the full result) ----
    @abstractmethod
    def stream_events(
        self, scope: Scope, *, filters: list[Filter], event_names: list[str] | None
    ) -> AsyncIterator[dict[str, Any]]: ...

    @abstractmethod
    def stream_persons(self, scope: Scope, *, filters: list[Filter]) -> AsyncIterator[dict[str, Any]]: ...
