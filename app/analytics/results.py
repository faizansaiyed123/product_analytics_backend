from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class SeriesResult(BaseModel):
    label: str
    event: str | None = None
    breakdown_value: str | None = None
    data: list[float | None]
    total: float | None = None


class TrendResult(BaseModel):
    type: str = "trend"
    kind: str = "time_series"  # or "distribution" (metric=frequency)
    interval: str | None
    labels: list[str]
    series: list[SeriesResult]
    date_from: str
    date_to: str
    timezone: str
    previous: TrendResult | None = None


class FunnelStepResult(BaseModel):
    order: int
    event: str
    label: str
    count: int
    conversion_rate: float | None  # vs previous step
    dropoff_count: int
    dropoff_rate: float
    overall_conversion_rate: float  # vs first step
    avg_time_to_convert_seconds: float | None
    median_time_to_convert_seconds: float | None


class FunnelBreakdown(BaseModel):
    value: str
    steps: list[FunnelStepResult]
    overall_conversion_rate: float


class FunnelResult(BaseModel):
    type: str = "funnel"
    steps: list[FunnelStepResult]
    overall_conversion_rate: float
    breakdowns: list[FunnelBreakdown] | None = None
    conversion_window_seconds: int
    date_from: str
    date_to: str
    timezone: str


class RetentionCell(BaseModel):
    offset: int
    count: int | None
    rate: float | None  # null = period has not happened yet


class RetentionCohort(BaseModel):
    period: str
    size: int
    cells: list[RetentionCell]


class RetentionResult(BaseModel):
    type: str = "retention"
    interval: str
    offsets: list[int]
    cohorts: list[RetentionCohort]
    average: list[RetentionCell]
    date_from: str
    date_to: str
    timezone: str


class SessionSummary(BaseModel):
    sessions: int
    avg_duration_seconds: float | None
    avg_events_per_session: float | None
    avg_pages_per_session: float | None
    bounce_rate: float | None
    unique_users: int


class SessionsResult(BaseModel):
    type: str = "sessions"
    summary: SessionSummary
    interval: str
    labels: list[str]
    sessions_series: list[float]
    avg_duration_series: list[float | None]
    date_from: str
    date_to: str
    timezone: str
    previous: SessionSummary | None = None


class PathEdge(BaseModel):
    step: int
    source: str
    target: str
    count: int


class PathsResult(BaseModel):
    type: str = "paths"
    edges: list[PathEdge]
    sessions: int
    date_from: str
    date_to: str
    timezone: str


class QueryEnvelope(BaseModel):
    """What the API returns for every analytics query."""

    query_type: str
    result: dict[str, Any]
    cached: bool = False
    computed_at: str
    duration_ms: float
