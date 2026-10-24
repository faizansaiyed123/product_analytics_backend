"""Cohorts, dashboards, reports, alerts, notifications."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.queries import AnalyticsQuery, CohortDefinition, DateRange
from app.api.deps import client_ip, current_user, get_project_access
from app.core.errors import APIError, NotFound
from app.core.permissions import READ, WRITE, Role
from app.core.timeutil import utcnow
from app.db.session import get_session
from app.models import Alert, Cohort, Dashboard, Notification, Report, User
from app.schemas.common import OkResponse
from app.services.alerts import validate_webhook_url
from app.services.analytics_service import AnalyticsService
from app.services.audit import audit

router = APIRouter(tags=["saved objects"])
_adapter_cache: dict = {}


async def _acc(db: AsyncSession, user: User, project_id: UUID, role: Role):  # type: ignore[no-untyped-def]
    return await get_project_access(db, user, project_id, role)


async def _load(db: AsyncSession, model: Any, obj_id: UUID, user: User, role: Role):  # type: ignore[no-untyped-def]
    obj = await db.get(model, obj_id)
    if obj is None:
        raise NotFound(f"{model.__name__} not found")
    acc = await _acc(db, user, obj.project_id, role)  # 404 for non-members
    return obj, acc


# =============================================================== cohorts
class CohortIn(BaseModel):
    project_id: UUID
    name: str = Field(min_length=1, max_length=200)
    description: str = Field("", max_length=2000)
    definition: CohortDefinition


class CohortPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=2000)
    definition: CohortDefinition | None = None


class CohortOut(BaseModel):
    id: UUID
    project_id: UUID
    name: str
    description: str
    definition: dict
    last_count: int | None
    last_calculated_at: datetime | None
    created_at: datetime
    updated_at: datetime


def _cohort_out(c: Cohort) -> CohortOut:
    return CohortOut(id=c.id, project_id=c.project_id, name=c.name, description=c.description, definition=c.definition,
                     last_count=c.last_count, last_calculated_at=c.last_calculated_at, created_at=c.created_at, updated_at=c.updated_at)


async def refresh_cohort_count(db: AsyncSession, cohort: Cohort, svc: AnalyticsService) -> None:
    scope = await svc.cohort_scope()
    cohort.last_count = await svc.repo.cohort_size(scope, CohortDefinition.model_validate(cohort.definition))
    cohort.last_calculated_at = utcnow()


@router.get("/cohorts", response_model=list[CohortOut])
async def list_cohorts(project_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    await _acc(db, user, project_id, READ)
    rows = (await db.execute(select(Cohort).where(Cohort.project_id == project_id).order_by(Cohort.name))).scalars().all()
    return [_cohort_out(c) for c in rows]


@router.post("/cohorts", response_model=CohortOut, status_code=201)
async def create_cohort(body: CohortIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    acc = await _acc(db, user, body.project_id, WRITE)
    c = Cohort(project_id=body.project_id, name=body.name.strip(), description=body.description,
               definition=body.definition.model_dump(mode="json"), created_by=user.id)
    db.add(c)
    await db.flush()
    await refresh_cohort_count(db, c, AnalyticsService(db, acc.project.id, acc.settings))
    return _cohort_out(c)


@router.get("/cohorts/{cohort_id}", response_model=CohortOut)
async def get_cohort(cohort_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    c, _ = await _load(db, Cohort, cohort_id, user, READ)
    return _cohort_out(c)


@router.patch("/cohorts/{cohort_id}", response_model=CohortOut)
async def update_cohort(cohort_id: UUID, body: CohortPatch, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    c, acc = await _load(db, Cohort, cohort_id, user, WRITE)
    if body.name is not None:
        c.name = body.name.strip()
    if body.description is not None:
        c.description = body.description
    if body.definition is not None:
        c.definition = body.definition.model_dump(mode="json")
        await refresh_cohort_count(db, c, AnalyticsService(db, acc.project.id, acc.settings))
    c.updated_at = utcnow()
    return _cohort_out(c)


@router.delete("/cohorts/{cohort_id}", response_model=OkResponse)
async def delete_cohort(cohort_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    c, _ = await _load(db, Cohort, cohort_id, user, WRITE)
    await db.delete(c)
    return OkResponse()


@router.post("/cohorts/{cohort_id}/refresh", response_model=CohortOut, summary="Recalculate the member count now")
async def refresh_cohort(cohort_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    c, acc = await _load(db, Cohort, cohort_id, user, READ)
    await refresh_cohort_count(db, c, AnalyticsService(db, acc.project.id, acc.settings))
    return _cohort_out(c)


@router.get("/cohorts/{cohort_id}/members")
async def cohort_members(cohort_id: UUID, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                         user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    c, acc = await _load(db, Cohort, cohort_id, user, READ)
    svc = AnalyticsService(db, acc.project.id, acc.settings)
    items = await svc.repo.cohort_members(await svc.cohort_scope(), CohortDefinition.model_validate(c.definition), limit, offset)
    return {"items": items, "total": await svc.repo.cohort_size(await svc.cohort_scope(), CohortDefinition.model_validate(c.definition))}


class CohortPreview(BaseModel):
    project_id: UUID
    definition: CohortDefinition


@router.post("/cohorts/preview", summary="Count members of an unsaved cohort definition")
async def preview_cohort(body: CohortPreview, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    acc = await _acc(db, user, body.project_id, READ)
    svc = AnalyticsService(db, acc.project.id, acc.settings)
    return {"count": await svc.repo.cohort_size(await svc.cohort_scope(), body.definition)}


# =============================================================== reports
class ReportIn(BaseModel):
    project_id: UUID
    name: str = Field(min_length=1, max_length=200)
    description: str = Field("", max_length=2000)
    query: AnalyticsQuery
    visualization: dict[str, Any] = Field(default_factory=dict)


class ReportPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=2000)
    query: AnalyticsQuery | None = None
    visualization: dict[str, Any] | None = None


class ReportOut(BaseModel):
    id: UUID
    project_id: UUID
    name: str
    description: str
    query: dict
    visualization: dict
    created_by: UUID | None
    created_by_name: str | None = None
    created_at: datetime
    updated_at: datetime


def _report_out(r: Report, creator: str | None = None) -> ReportOut:
    return ReportOut(id=r.id, project_id=r.project_id, name=r.name, description=r.description, query=r.query,
                     visualization=r.visualization, created_by=r.created_by, created_by_name=creator,
                     created_at=r.created_at, updated_at=r.updated_at)


async def _creator_names(db: AsyncSession, ids: set[UUID | None]) -> dict[UUID, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {u.id: (u.name or u.email) for u in (await db.execute(select(User).where(User.id.in_(ids)))).scalars()}


@router.get("/reports", response_model=list[ReportOut])
async def list_reports(project_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    await _acc(db, user, project_id, READ)
    rows = (await db.execute(select(Report).where(Report.project_id == project_id).order_by(Report.updated_at.desc()))).scalars().all()
    names = await _creator_names(db, {r.created_by for r in rows})
    return [_report_out(r, names.get(r.created_by)) for r in rows]  # type: ignore[arg-type]


@router.post("/reports", response_model=ReportOut, status_code=201)
async def create_report(body: ReportIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    await _acc(db, user, body.project_id, WRITE)
    r = Report(project_id=body.project_id, name=body.name.strip(), description=body.description,
               query=body.query.model_dump(mode="json", by_alias=True), visualization=body.visualization, created_by=user.id)
    db.add(r)
    await db.flush()
    return _report_out(r, user.name or user.email)


@router.get("/reports/{report_id}", response_model=ReportOut)
async def get_report(report_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    r, _ = await _load(db, Report, report_id, user, READ)
    return _report_out(r, (await _creator_names(db, {r.created_by})).get(r.created_by))  # type: ignore[arg-type]


@router.patch("/reports/{report_id}", response_model=ReportOut)
async def update_report(report_id: UUID, body: ReportPatch, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    r, _ = await _load(db, Report, report_id, user, WRITE)
    if body.name is not None:
        r.name = body.name.strip()
    if body.description is not None:
        r.description = body.description
    if body.query is not None:
        r.query = body.query.model_dump(mode="json", by_alias=True)
    if body.visualization is not None:
        r.visualization = body.visualization
    r.updated_at = utcnow()
    return _report_out(r)


@router.post("/reports/{report_id}/duplicate", response_model=ReportOut, status_code=201)
async def duplicate_report(report_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    r, _ = await _load(db, Report, report_id, user, WRITE)
    copy = Report(project_id=r.project_id, name=f"{r.name} (copy)"[:200], description=r.description, query=r.query,
                  visualization=r.visualization, created_by=user.id)
    db.add(copy)
    await db.flush()
    return _report_out(copy, user.name or user.email)


@router.delete("/reports/{report_id}", response_model=OkResponse)
async def delete_report(report_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    r, _ = await _load(db, Report, report_id, user, WRITE)
    await db.delete(r)
    return OkResponse()


# =============================================================== dashboards
class Layout(BaseModel):
    x: int = Field(0, ge=0, le=11)
    y: int = Field(0, ge=0, le=10_000)
    w: int = Field(6, ge=1, le=12)
    h: int = Field(4, ge=1, le=20)


class Widget(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    title: str = Field("", max_length=200)
    report_id: UUID | None = Field(None, description="Use a saved report's query (kept in sync)")
    query: AnalyticsQuery | None = Field(None, description="Inline query when no report_id is set")
    visualization: dict[str, Any] = Field(default_factory=dict)
    layout: Layout = Field(default_factory=Layout)

    def check(self) -> Widget:
        if not self.report_id and self.query is None:
            raise ValueError("a widget needs report_id or query")
        return self


class DashboardConfig(BaseModel):
    widgets: list[Widget] = Field(default_factory=list, max_length=60)
    date_range: DateRange | None = None


class DashboardIn(BaseModel):
    project_id: UUID
    name: str = Field(min_length=1, max_length=200)
    description: str = Field("", max_length=2000)
    config: DashboardConfig = Field(default_factory=DashboardConfig)


class DashboardPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=2000)
    config: DashboardConfig | None = None


class DashboardOut(BaseModel):
    id: UUID
    project_id: UUID
    name: str
    description: str
    config: dict
    created_at: datetime
    updated_at: datetime


def _dash_out(d: Dashboard) -> DashboardOut:
    return DashboardOut(id=d.id, project_id=d.project_id, name=d.name, description=d.description, config=d.config,
                        created_at=d.created_at, updated_at=d.updated_at)


async def _validate_config(db: AsyncSession, project_id: UUID, cfg: DashboardConfig) -> dict:
    ids = [w.id for w in cfg.widgets]
    if len(ids) != len(set(ids)):
        raise APIError("Widget ids must be unique", code="INVALID_DASHBOARD", status_code=422)
    report_ids = {w.report_id for w in cfg.widgets if w.report_id}
    if report_ids:
        found = set((await db.execute(select(Report.id).where(Report.project_id == project_id, Report.id.in_(report_ids)))).scalars())
        if found != report_ids:
            raise APIError("A widget references a report that does not exist in this project", code="INVALID_DASHBOARD", status_code=422)
    for w in cfg.widgets:
        try:
            w.check()
        except ValueError as exc:
            raise APIError(str(exc), code="INVALID_DASHBOARD", status_code=422) from exc
    return cfg.model_dump(mode="json", by_alias=True)


@router.get("/dashboards", response_model=list[DashboardOut])
async def list_dashboards(project_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    await _acc(db, user, project_id, READ)
    rows = (await db.execute(select(Dashboard).where(Dashboard.project_id == project_id).order_by(Dashboard.name))).scalars().all()
    return [_dash_out(d) for d in rows]


@router.post("/dashboards", response_model=DashboardOut, status_code=201)
async def create_dashboard(body: DashboardIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    await _acc(db, user, body.project_id, WRITE)
    d = Dashboard(project_id=body.project_id, name=body.name.strip(), description=body.description,
                  config=await _validate_config(db, body.project_id, body.config), created_by=user.id)
    db.add(d)
    await db.flush()
    return _dash_out(d)


@router.get("/dashboards/{dashboard_id}", response_model=DashboardOut)
async def get_dashboard(dashboard_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    d, _ = await _load(db, Dashboard, dashboard_id, user, READ)
    return _dash_out(d)


@router.patch("/dashboards/{dashboard_id}", response_model=DashboardOut)
async def update_dashboard(dashboard_id: UUID, body: DashboardPatch, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    d, _ = await _load(db, Dashboard, dashboard_id, user, WRITE)
    if body.name is not None:
        d.name = body.name.strip()
    if body.description is not None:
        d.description = body.description
    if body.config is not None:
        d.config = await _validate_config(db, d.project_id, body.config)
    d.updated_at = utcnow()
    return _dash_out(d)


@router.post("/dashboards/{dashboard_id}/duplicate", response_model=DashboardOut, status_code=201)
async def duplicate_dashboard(dashboard_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    d, _ = await _load(db, Dashboard, dashboard_id, user, WRITE)
    import copy as _copy
    import uuid as _uuid

    cfg = _copy.deepcopy(d.config)
    for w in cfg.get("widgets", []):
        w["id"] = _uuid.uuid4().hex[:12]
    new = Dashboard(project_id=d.project_id, name=f"{d.name} (copy)"[:200], description=d.description, config=cfg, created_by=user.id)
    db.add(new)
    await db.flush()
    return _dash_out(new)


@router.delete("/dashboards/{dashboard_id}", response_model=OkResponse)
async def delete_dashboard(dashboard_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    d, _ = await _load(db, Dashboard, dashboard_id, user, WRITE)
    await db.delete(d)
    return OkResponse()


# =============================================================== alerts + notifications
class AlertCondition(BaseModel):
    operator: Literal["lt", "lte", "gt", "gte", "decrease_pct", "increase_pct"]
    value: float = Field(ge=0)


class AlertQuery(BaseModel):
    event: str = Field(min_length=1, max_length=200)
    metric: Literal["total_events", "unique_users", "sum"] = "total_events"
    metric_property: str | None = None
    filters: list[Any] = Field(default_factory=list, max_length=10)


class AlertIn(BaseModel):
    project_id: UUID
    name: str = Field(min_length=1, max_length=200)
    query: AlertQuery
    condition: AlertCondition
    period: Literal["hour", "day", "week"] = "day"
    webhook_url: str | None = Field(None, max_length=2000)
    enabled: bool = True


class AlertPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    query: AlertQuery | None = None
    condition: AlertCondition | None = None
    period: Literal["hour", "day", "week"] | None = None
    webhook_url: str | None = None
    webhook_url_set: bool = False
    enabled: bool | None = None


class AlertOut(BaseModel):
    id: UUID
    project_id: UUID
    name: str
    enabled: bool
    query: dict
    condition: dict
    period: str
    webhook_url: str | None
    state: str
    last_value: float | None
    last_evaluated_at: datetime | None
    last_triggered_at: datetime | None
    last_error: str | None
    created_at: datetime


def _alert_out(a: Alert) -> AlertOut:
    return AlertOut(id=a.id, project_id=a.project_id, name=a.name, enabled=a.enabled, query=a.query, condition=a.condition,
                    period=a.period, webhook_url=a.webhook_url, state=a.state, last_value=a.last_value,
                    last_evaluated_at=a.last_evaluated_at, last_triggered_at=a.last_triggered_at, last_error=a.last_error,
                    created_at=a.created_at)


def _check_alert(q: AlertQuery) -> None:
    if q.metric == "sum" and not q.metric_property:
        raise APIError("metric 'sum' requires metric_property", code="INVALID_ALERT", status_code=422)


@router.get("/alerts", response_model=list[AlertOut])
async def list_alerts(project_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    await _acc(db, user, project_id, READ)
    rows = (await db.execute(select(Alert).where(Alert.project_id == project_id).order_by(Alert.name))).scalars().all()
    return [_alert_out(a) for a in rows]


@router.post("/alerts", response_model=AlertOut, status_code=201)
async def create_alert(body: AlertIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    await _acc(db, user, body.project_id, WRITE)
    _check_alert(body.query)
    if body.webhook_url:
        await validate_webhook_url(body.webhook_url)
    a = Alert(project_id=body.project_id, name=body.name.strip(), query=body.query.model_dump(mode="json"),
              condition=body.condition.model_dump(), period=body.period, webhook_url=body.webhook_url or None,
              enabled=body.enabled, created_by=user.id)
    db.add(a)
    await db.flush()
    return _alert_out(a)


@router.patch("/alerts/{alert_id}", response_model=AlertOut)
async def update_alert(alert_id: UUID, body: AlertPatch, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    a, _ = await _load(db, Alert, alert_id, user, WRITE)
    if body.name is not None:
        a.name = body.name.strip()
    if body.query is not None:
        _check_alert(body.query)
        a.query = body.query.model_dump(mode="json")
        a.state = "unknown"
    if body.condition is not None:
        a.condition = body.condition.model_dump()
    if body.period is not None:
        a.period = body.period
    if body.webhook_url_set:
        if body.webhook_url:
            await validate_webhook_url(body.webhook_url)
        a.webhook_url = body.webhook_url or None
    if body.enabled is not None:
        a.enabled = body.enabled
    a.updated_at = utcnow()
    return _alert_out(a)


@router.delete("/alerts/{alert_id}", response_model=OkResponse)
async def delete_alert(alert_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    a, _ = await _load(db, Alert, alert_id, user, WRITE)
    await db.delete(a)
    return OkResponse()


@router.post("/alerts/{alert_id}/evaluate", response_model=AlertOut, summary="Evaluate an alert immediately")
async def evaluate_alert_now(alert_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    from app.services.alerts import evaluate_alert

    a, acc = await _load(db, Alert, alert_id, user, WRITE)
    await evaluate_alert(db, a, acc.settings, deliver_webhook=False)
    return _alert_out(a)


class NotificationOut(BaseModel):
    id: UUID
    alert_id: UUID | None
    kind: str
    title: str
    body: str
    data: dict
    created_at: datetime
    read_at: datetime | None


@router.get("/notifications")
async def list_notifications(project_id: UUID, unread_only: bool = False, limit: int = Query(50, ge=1, le=200),
                             user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    await _acc(db, user, project_id, READ)
    q = select(Notification).where(Notification.project_id == project_id)
    if unread_only:
        q = q.where(Notification.read_at.is_(None))
    rows = (await db.execute(q.order_by(Notification.created_at.desc()).limit(limit))).scalars().all()
    unread = (await db.execute(select(func.count()).select_from(Notification).where(
        Notification.project_id == project_id, Notification.read_at.is_(None)))).scalar_one()
    return {"items": [NotificationOut(id=n.id, alert_id=n.alert_id, kind=n.kind, title=n.title, body=n.body, data=n.data,
                                      created_at=n.created_at, read_at=n.read_at) for n in rows], "unread": unread}


@router.post("/notifications/read", response_model=OkResponse)
async def mark_read(project_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    from sqlalchemy import update

    await _acc(db, user, project_id, READ)
    await db.execute(update(Notification).where(Notification.project_id == project_id, Notification.read_at.is_(None)).values(read_at=utcnow()))
    return OkResponse()


_ = (Request, client_ip, audit)
