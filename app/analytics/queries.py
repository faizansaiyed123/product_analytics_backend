"""Structured, typed analytics query objects. The frontend sends these; only the backend turns them into SQL."""

from __future__ import annotations

from datetime import date as Date
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Operator = Literal[
    "equals",
    "not_equals",
    "contains",
    "not_contains",
    "gt",
    "gte",
    "lt",
    "lte",
    "is_set",
    "is_not_set",
    "in",
    "not_in",
]
Interval = Literal["hour", "day", "week", "month"]
Preset = Literal[
    "today",
    "yesterday",
    "last_7_days",
    "last_14_days",
    "last_30_days",
    "last_90_days",
    "this_month",
    "last_month",
]


class PropertyFilter(BaseModel):
    """`type` selects where the property lives. Names starting with `$` that match a built-in column
    ($browser, $os, $device_type, $country, $path, $hostname, ...) read that column for event filters."""

    type: Literal["event", "person"] = "event"
    property: str = Field(min_length=1, max_length=200)
    operator: Operator = "equals"
    value: Any = None

    @model_validator(mode="after")
    def _check(self) -> PropertyFilter:
        if self.operator in ("in", "not_in") and not isinstance(self.value, list):
            raise ValueError(f"operator '{self.operator}' needs a list value")
        if self.operator in ("gt", "gte", "lt", "lte"):
            try:
                float(self.value)
            except (TypeError, ValueError):
                raise ValueError(f"operator '{self.operator}' needs a numeric value") from None
        if self.operator not in ("is_set", "is_not_set") and self.value is None:
            raise ValueError(f"operator '{self.operator}' needs a value")
        return self


class CohortFilter(BaseModel):
    type: Literal["cohort"] = "cohort"
    cohort_id: UUID
    negate: bool = False


Filter = PropertyFilter | CohortFilter


class DateRange(BaseModel):
    """Either an explicit inclusive range (`from`/`to`, dates or ISO datetimes) or a preset."""

    model_config = ConfigDict(populate_by_name=True)
    from_: str | None = Field(None, alias="from", description="YYYY-MM-DD or ISO datetime")
    to: str | None = None
    preset: Preset | None = None

    def model_dump(self, **kw: Any) -> dict[str, Any]:  # keep the wire name "from"
        kw.setdefault("by_alias", True)
        return super().model_dump(**kw)


class Breakdown(BaseModel):
    type: Literal["event", "person"] = "event"
    property: str = Field(min_length=1, max_length=200)
    limit: int = Field(10, ge=1, le=25)


TrendMetric = Literal[
    "total_events",
    "unique_users",
    "avg_events_per_user",
    "frequency",
    "sum",
    "avg",
    "min",
    "max",
    "median",
    "p90",
]


class TrendSeries(BaseModel):
    event: str = Field(description="Event name, or '*' for all events")
    label: str | None = None
    filters: list[Filter] = Field(default_factory=list, max_length=20)


class TrendQuery(BaseModel):
    type: Literal["trend"] = "trend"
    event: str | None = None
    series: list[TrendSeries] = Field(default_factory=list, max_length=5)
    metric: TrendMetric = "total_events"
    metric_property: str | None = Field(None, description="Numeric event property for sum/avg/min/max/median/p90")
    date_range: DateRange = Field(default_factory=lambda: DateRange(preset="last_30_days"))
    interval: Interval = "day"
    filters: list[Filter] = Field(default_factory=list, max_length=20)
    breakdown: Breakdown | None = None
    timezone: str | None = None
    compare_previous: bool = False

    @model_validator(mode="after")
    def _check(self) -> TrendQuery:
        if not self.series:
            if not self.event:
                raise ValueError("Provide 'event' or 'series'")
            self.series = [TrendSeries(event=self.event)]
        if self.metric in ("sum", "avg", "min", "max", "median", "p90") and not self.metric_property:
            raise ValueError(f"metric '{self.metric}' requires metric_property")
        if self.breakdown and len(self.series) > 1:
            raise ValueError("breakdown works with a single series")
        return self


class FunnelStep(BaseModel):
    event: str = Field(min_length=1, max_length=200)
    label: str | None = None
    filters: list[Filter] = Field(default_factory=list, max_length=20)


class ConversionWindow(BaseModel):
    value: int = Field(14, ge=1, le=100_000)
    unit: Literal["second", "minute", "hour", "day", "week"] = "day"

    def seconds(self) -> int:
        return self.value * {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800}[self.unit]

    @model_validator(mode="after")
    def _max(self) -> ConversionWindow:
        if self.seconds() > 90 * 86400:
            raise ValueError("conversion window may be at most 90 days")
        return self


class FunnelQuery(BaseModel):
    type: Literal["funnel"] = "funnel"
    steps: list[FunnelStep] = Field(min_length=2, max_length=10)
    date_range: DateRange = Field(default_factory=lambda: DateRange(preset="last_30_days"))
    conversion_window: ConversionWindow = Field(default_factory=ConversionWindow)
    filters: list[Filter] = Field(default_factory=list, max_length=20)
    breakdown: Breakdown | None = Field(None, description="Property value on the first step")
    timezone: str | None = None


class EventRef(BaseModel):
    event: str = Field("*", description="Event name or '*' for any event")
    filters: list[Filter] = Field(default_factory=list, max_length=20)


class RetentionQuery(BaseModel):
    type: Literal["retention"] = "retention"
    initial_event: EventRef = Field(default_factory=EventRef)
    return_event: EventRef = Field(default_factory=EventRef)
    cohort_type: Literal["first_time", "recurring"] = "first_time"
    interval: Literal["day", "week", "month"] = "day"
    periods: int = Field(8, ge=1, le=90, description="Number of periods after period 0")
    offsets: list[int] | None = Field(None, description="Only these offsets, e.g. [0,1,7,14,30]")
    date_range: DateRange = Field(default_factory=lambda: DateRange(preset="last_30_days"))
    filters: list[Filter] = Field(default_factory=list, max_length=20)
    timezone: str | None = None

    @field_validator("offsets")
    @classmethod
    def _offsets(cls, v: list[int] | None) -> list[int] | None:
        if v is None:
            return v
        if any(o < 0 or o > 365 for o in v) or len(v) > 40:
            raise ValueError("offsets must be between 0 and 365 (max 40 values)")
        return sorted(set(v))


class SessionsQuery(BaseModel):
    type: Literal["sessions"] = "sessions"
    date_range: DateRange = Field(default_factory=lambda: DateRange(preset="last_30_days"))
    interval: Interval = "day"
    filters: list[Filter] = Field(default_factory=list, max_length=20)
    timezone: str | None = None


class PathsQuery(BaseModel):
    type: Literal["paths"] = "paths"
    path_type: Literal["event", "page"] = "event"
    start_event: str | None = Field(
        None, description="Only journeys that reach this event/page; step 1 = first occurrence"
    )
    depth: int = Field(4, ge=2, le=8)
    max_nodes_per_step: int = Field(8, ge=2, le=20)
    date_range: DateRange = Field(default_factory=lambda: DateRange(preset="last_30_days"))
    filters: list[Filter] = Field(default_factory=list, max_length=20)
    timezone: str | None = None


AnalyticsQuery = Annotated[
    TrendQuery | FunnelQuery | RetentionQuery | SessionsQuery | PathsQuery, Field(discriminator="type")
]


# ---- cohorts -------------------------------------------------------------------------------------
class PerformedEvent(BaseModel):
    type: Literal["performed_event"] = "performed_event"
    event: str
    operator: Literal["at_least", "at_most", "exactly"] = "at_least"
    count: int = Field(1, ge=0, le=1_000_000)
    within_days: int | None = Field(None, ge=1, le=3650)
    filters: list[PropertyFilter] = Field(default_factory=list, max_length=10)


class NotPerformedEvent(BaseModel):
    type: Literal["not_performed_event"] = "not_performed_event"
    event: str
    within_days: int | None = Field(None, ge=1, le=3650)
    filters: list[PropertyFilter] = Field(default_factory=list, max_length=10)


class PersonPropertyCriterion(BaseModel):
    type: Literal["person_property"] = "person_property"
    property: str = Field(min_length=1, max_length=200)
    operator: Operator = "equals"
    value: Any = None


class FirstSeenCriterion(BaseModel):
    type: Literal["first_seen"] = "first_seen"
    operator: Literal["within_last_days", "before", "after"] = "within_last_days"
    days: int | None = Field(None, ge=1, le=3650)
    date: Date | None = None

    @model_validator(mode="after")
    def _check(self) -> FirstSeenCriterion:
        if self.operator == "within_last_days" and not self.days:
            raise ValueError("'days' is required for within_last_days")
        if self.operator in ("before", "after") and not self.date:
            raise ValueError("'date' is required for before/after")
        return self


CohortCriterion = Annotated[
    PerformedEvent | NotPerformedEvent | PersonPropertyCriterion | FirstSeenCriterion, Field(discriminator="type")
]


class CohortDefinition(BaseModel):
    match: Literal["all", "any"] = "all"
    criteria: list[CohortCriterion] = Field(min_length=1, max_length=20)


class Scope(BaseModel):
    """Resolved execution context handed to the repository (all instants are UTC)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    project_id: UUID
    date_from: datetime
    date_to: datetime  # exclusive
    timezone: str = "UTC"
    session_timeout_minutes: int = 30
    cohorts: dict[UUID, CohortDefinition] = Field(default_factory=dict)
    now: datetime | None = None
