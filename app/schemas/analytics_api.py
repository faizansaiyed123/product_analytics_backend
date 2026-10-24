from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, TypeAdapter, field_validator

from app.analytics.queries import (
    AnalyticsQuery,
    CohortDefinition,
    DateRange,
    Filter,
    FunnelQuery,
    PathsQuery,
    RetentionQuery,
    SessionsQuery,
    TrendQuery,
)

FILTER_LIST = TypeAdapter(list[Filter])
QUERY_ADAPTER: TypeAdapter[Any] = TypeAdapter(AnalyticsQuery)


def parse_filters_param(raw: str | None) -> list[Any]:
    if not raw:
        return []
    try:
        return FILTER_LIST.validate_python(json.loads(raw))
    except (ValueError, TypeError) as exc:
        from app.core.errors import APIError

        raise APIError(f"Invalid 'filters' parameter: {exc}", code="INVALID_FILTER", status_code=422) from exc


class QueryRequest(BaseModel):
    project_id: UUID
    query: AnalyticsQuery
    refresh: bool = False


class TrendRequest(BaseModel):
    project_id: UUID
    query: TrendQuery
    refresh: bool = False


class FunnelRequest(BaseModel):
    project_id: UUID
    query: FunnelQuery
    refresh: bool = False


class RetentionRequest(BaseModel):
    project_id: UUID
    query: RetentionQuery
    refresh: bool = False


class SessionsRequest(BaseModel):
    project_id: UUID
    query: SessionsQuery
    refresh: bool = False


class PathsRequest(BaseModel):
    project_id: UUID
    query: PathsQuery
    refresh: bool = False


class OverviewRequest(BaseModel):
    project_id: UUID
    date_range: DateRange = Field(default_factory=lambda: DateRange(preset="last_7_days"))
    interval: str | None = None
    compare_previous: bool = True
    timezone: str | None = None


class SessionsListRequest(BaseModel):
    project_id: UUID
    date_range: DateRange = Field(default_factory=lambda: DateRange(preset="last_7_days"))
    filters: list[Filter] = Field(default_factory=list)
    limit: int = Field(50, ge=1, le=200)
    offset: int = Field(0, ge=0, le=100_000)
    timezone: str | None = None


# ---- saved objects -------------------------------------------------------------------------------
class Visualization(BaseModel):
    chart: str = Field("line", pattern="^(line|area|bar|table|kpi|heatmap|funnel|sankey)$")
    options: dict[str, Any] = Field(default_factory=dict)


class ReportCreate(BaseModel):
    project_id: UUID
    name: str = Field(min_length=1, max_length=200)
    description: str = Field("", max_length=2000)
    query: AnalyticsQuery
    visualization: Visualization = Field(default_factory=Visualization)


class ReportUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=2000)
    query: AnalyticsQuery | None = None
    visualization: Visualization | None = None


class ReportOut(BaseModel):
    id: UUID
    project_id: UUID
    name: str
    description: str
    query: dict[str, Any]
    visualization: dict[str, Any]
    created_by: UUID | None
    created_by_name: str | None = None
    created_at: str
    updated_at: str


class WidgetLayout(BaseModel):
    x: int = Field(0, ge=0, le=100)
    y: int = Field(0, ge=0, le=10_000)
    w: int = Field(6, ge=1, le=12)
    h: int = Field(4, ge=1, le=24)


class Widget(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    title: str = Field("", max_length=200)
    report_id: UUID | None = None
    query: AnalyticsQuery | None = None
    visualization: Visualization = Field(default_factory=Visualization)
    layout: WidgetLayout = Field(default_factory=WidgetLayout)

    @field_validator("query", mode="before")
    @classmethod
    def _q(cls, v: Any) -> Any:
        return v

    def check(self) -> Widget:
        if self.report_id is None and self.query is None:
            raise ValueError("A widget needs either report_id or query")
        return self


class DashboardConfig(BaseModel):
    widgets: list[Widget] = Field(default_factory=list, max_length=60)
    date_range: DateRange | None = None

    def validated(self) -> DashboardConfig:
        ids = [w.id for w in self.widgets]
        if len(ids) != len(set(ids)):
            raise ValueError("Widget ids must be unique")
        for w in self.widgets:
            w.check()
        return self


class DashboardCreate(BaseModel):
    project_id: UUID
    name: str = Field(min_length=1, max_length=200)
    description: str = Field("", max_length=2000)
    config: DashboardConfig = Field(default_factory=DashboardConfig)


class DashboardUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=2000)
    config: DashboardConfig | None = None


class DashboardOut(BaseModel):
    id: UUID
    project_id: UUID
    name: str
    description: str
    config: dict[str, Any]
    created_by: UUID | None
    created_at: str
    updated_at: str


class CohortCreate(BaseModel):
    project_id: UUID
    name: str = Field(min_length=1, max_length=200)
    description: str = Field("", max_length=2000)
    definition: CohortDefinition


class CohortUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=2000)
    definition: CohortDefinition | None = None


class CohortPreview(BaseModel):
    project_id: UUID
    definition: CohortDefinition


class CohortOut(BaseModel):
    id: UUID
    project_id: UUID
    name: str
    description: str
    definition: dict[str, Any]
    last_count: int | None
    last_calculated_at: str | None
    created_at: str
    updated_at: str


class AlertQuery(BaseModel):
    event: str = Field(min_length=1, max_length=200)
    metric: str = Field("total_events", pattern="^(total_events|unique_users|sum|avg)$")
    metric_property: str | None = None
    filters: list[Filter] = Field(default_factory=list, max_length=10)


class AlertCondition(BaseModel):
    operator: str = Field(pattern="^(lt|lte|gt|gte|decrease_pct|increase_pct)$")
    value: float


class AlertCreate(BaseModel):
    project_id: UUID
    name: str = Field(min_length=1, max_length=200)
    query: AlertQuery
    condition: AlertCondition
    period: str = Field("day", pattern="^(hour|day|week)$")
    webhook_url: str | None = Field(None, max_length=2000)
    enabled: bool = True


class AlertUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    query: AlertQuery | None = None
    condition: AlertCondition | None = None
    period: str | None = Field(None, pattern="^(hour|day|week)$")
    webhook_url: str | None = None
    webhook_url_set: bool = False
    enabled: bool | None = None


class AlertOut(BaseModel):
    id: UUID
    project_id: UUID
    name: str
    enabled: bool
    query: dict[str, Any]
    condition: dict[str, Any]
    period: str
    webhook_url: str | None
    state: str
    last_value: float | None
    last_evaluated_at: str | None
    last_triggered_at: str | None
    last_error: str | None
    created_at: str


class NotificationOut(BaseModel):
    id: UUID
    project_id: UUID
    alert_id: UUID | None
    kind: str
    title: str
    body: str
    data: dict[str, Any]
    created_at: str
    read_at: str | None


class ExportCreate(BaseModel):
    project_id: UUID
    kind: str = Field(pattern="^(events|users)$")
    format: str = Field("csv", pattern="^(csv|json)$")
    date_range: DateRange = Field(default_factory=lambda: DateRange(preset="last_30_days"))
    event_names: list[str] | None = None
    filters: list[Filter] = Field(default_factory=list, max_length=20)


class ExportOut(BaseModel):
    id: UUID
    project_id: UUID
    kind: str
    format: str
    status: str
    row_count: int | None
    error: str | None
    created_at: str
    finished_at: str | None


class AnalyticsExportRequest(BaseModel):
    project_id: UUID
    query: AnalyticsQuery
    format: str = Field("csv", pattern="^(csv|json)$")
