"""Turn structured filters into SQLAlchemy conditions. All user input is bound as parameters."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Numeric, and_, case, cast, func, literal, not_, or_, select
from sqlalchemy.sql import ColumnElement

from app.analytics.queries import (
    CohortDefinition,
    CohortFilter,
    FirstSeenCriterion,
    NotPerformedEvent,
    PerformedEvent,
    PersonPropertyCriterion,
    PropertyFilter,
    Scope,
)
from app.core.errors import APIError
from app.models import Event, Person

PAGEVIEW_EVENTS = ("$pageview", "page_view")

# Built-in event columns addressed with a leading "$" (everything else is read from the JSONB properties).
BUILTIN_COLUMNS = {
    "$browser": "browser",
    "$browser_version": "browser_version",
    "$os": "os",
    "$os_version": "os_version",
    "$device_type": "device_type",
    "$country": "country",
    "$path": "path",
    "$hostname": "hostname",
    "$distinct_id": "distinct_id",
    "$event": "event",
}


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def as_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def event_prop(ev: Any, name: str) -> ColumnElement:
    col = BUILTIN_COLUMNS.get(name)
    if col:
        return getattr(ev, col)
    return ev.properties[name].astext


def person_prop(name: str) -> ColumnElement:
    return Person.properties[name].astext


def numeric_expr(props_col: Any, name: str) -> ColumnElement:
    """JSON number -> numeric, anything else -> NULL (never raises on non-numeric values)."""
    return case((func.jsonb_typeof(props_col[name]) == "number", cast(props_col[name].astext, Numeric)), else_=None)


def _apply(op: str, text_expr: ColumnElement, num_expr: ColumnElement | None, value: Any) -> ColumnElement:
    if op == "equals":
        return text_expr == as_text(value)
    if op == "not_equals":
        return or_(text_expr != as_text(value), text_expr.is_(None))
    if op == "contains":
        return text_expr.ilike(f"%{_like_escape(str(value))}%", escape="\\")
    if op == "not_contains":
        return or_(not_(text_expr.ilike(f"%{_like_escape(str(value))}%", escape="\\")), text_expr.is_(None))
    if op in ("gt", "gte", "lt", "lte"):
        if num_expr is None:
            raise APIError(
                "Numeric comparison is not supported on this property", code="INVALID_FILTER", status_code=422
            )
        v = float(value)
        return {"gt": num_expr > v, "gte": num_expr >= v, "lt": num_expr < v, "lte": num_expr <= v}[op]
    if op == "is_set":
        return text_expr.is_not(None)
    if op == "is_not_set":
        return text_expr.is_(None)
    if op == "in":
        return text_expr.in_([as_text(v) for v in value])
    if op == "not_in":
        return or_(text_expr.not_in([as_text(v) for v in value]), text_expr.is_(None))
    raise APIError(f"Unknown operator {op}", code="INVALID_FILTER", status_code=422)


def event_property_condition(ev: Any, f: PropertyFilter) -> ColumnElement:
    is_builtin = f.property in BUILTIN_COLUMNS
    num = None if is_builtin else numeric_expr(ev.properties, f.property)
    return _apply(f.operator, event_prop(ev, f.property), num, f.value)


def person_property_condition(name: str, operator: str, value: Any) -> ColumnElement:
    return _apply(operator, person_prop(name), numeric_expr(Person.properties, name), value)


def person_ids_matching(scope: Scope, cond: ColumnElement) -> Any:
    return select(Person.id).where(Person.project_id == scope.project_id, cond)


def filter_conditions(scope: Scope, ev: Any, filters: list[Any]) -> list[ColumnElement]:
    """Conditions for an events alias/table `ev`. Person and cohort filters become semi-joins on person_id."""
    out: list[ColumnElement] = []
    for f in filters:
        if isinstance(f, CohortFilter):
            definition = scope.cohorts.get(f.cohort_id)
            if definition is None:
                raise APIError("Cohort not found", code="COHORT_NOT_FOUND", status_code=404)
            sub = cohort_person_ids(scope, definition)
            out.append(ev.person_id.not_in(sub) if f.negate else ev.person_id.in_(sub))
        elif f.type == "person":
            out.append(
                ev.person_id.in_(person_ids_matching(scope, person_property_condition(f.property, f.operator, f.value)))
            )
        else:
            out.append(event_property_condition(ev, f))
    return out


def person_only_conditions(scope: Scope, filters: list[Any]) -> list[ColumnElement]:
    """Filters expressed against the persons table (used when the query is about people, not events)."""
    out: list[ColumnElement] = []
    for f in filters:
        if isinstance(f, CohortFilter):
            definition = scope.cohorts.get(f.cohort_id)
            if definition is None:
                raise APIError("Cohort not found", code="COHORT_NOT_FOUND", status_code=404)
            sub = cohort_person_ids(scope, definition)
            out.append(Person.id.not_in(sub) if f.negate else Person.id.in_(sub))
        elif f.type == "person":
            out.append(person_property_condition(f.property, f.operator, f.value))
        # event-typed filters do not apply to a persons listing
    return out


# ------------------------------------------------------------------------------------------------
# cohorts
# ------------------------------------------------------------------------------------------------
def _criterion(scope: Scope, c: Any, now: datetime) -> ColumnElement:
    if isinstance(c, PerformedEvent):
        conds = [Event.project_id == scope.project_id, Event.event == c.event]
        if c.within_days:
            conds.append(Event.timestamp >= now - timedelta(days=c.within_days))
        conds += [event_property_condition(Event, f) for f in c.filters]
        base = select(Event.person_id).where(*conds).group_by(Event.person_id)
        if c.operator == "at_least":
            return Person.id.in_(base.having(func.count() >= c.count) if c.count > 1 else base)
        if c.operator == "exactly":
            if c.count == 0:
                return Person.id.not_in(base)
            return Person.id.in_(base.having(func.count() == c.count))
        # at_most n: complement of "more than n" (includes people who never did it)
        return Person.id.not_in(base.having(func.count() > c.count))
    if isinstance(c, NotPerformedEvent):
        conds = [Event.project_id == scope.project_id, Event.event == c.event]
        if c.within_days:
            conds.append(Event.timestamp >= now - timedelta(days=c.within_days))
        conds += [event_property_condition(Event, f) for f in c.filters]
        return Person.id.not_in(select(Event.person_id).where(*conds))
    if isinstance(c, PersonPropertyCriterion):
        return person_property_condition(c.property, c.operator, c.value)
    if isinstance(c, FirstSeenCriterion):
        if c.operator == "within_last_days":
            return Person.first_seen >= now - timedelta(days=c.days or 1)
        cutoff = datetime(c.date.year, c.date.month, c.date.day, tzinfo=UTC)  # type: ignore[union-attr]
        return Person.first_seen < cutoff if c.operator == "before" else Person.first_seen >= cutoff
    raise APIError("Unsupported cohort criterion", code="INVALID_COHORT", status_code=422)


def cohort_person_ids(scope: Scope, definition: CohortDefinition) -> Any:
    now = scope.now or datetime.now(UTC)
    preds = [_criterion(scope, c, now) for c in definition.criteria]
    combined = and_(*preds) if definition.match == "all" else or_(*preds)
    return select(Person.id).where(Person.project_id == scope.project_id, combined)


_ = literal
