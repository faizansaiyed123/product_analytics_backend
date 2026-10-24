from __future__ import annotations

import asyncio
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.api.v1.projects import project_access
from app.core.errors import APIError, NotFound
from app.core.permissions import READ, WRITE
from app.core.timeutil import utcnow
from app.db.session import get_session
from app.models import Alert, Notification, Project, User
from app.schemas.analytics_api import AlertCreate, AlertOut, AlertUpdate, NotificationOut
from app.schemas.common import OkResponse
from app.services import alerts as svc

router = APIRouter(tags=["alerts"])


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


def _out(a: Alert) -> AlertOut:
    return AlertOut(
        id=a.id,
        project_id=a.project_id,
        name=a.name,
        enabled=a.enabled,
        query=a.query,
        condition=a.condition,
        period=a.period,
        webhook_url=a.webhook_url,
        state=a.state,
        last_value=a.last_value,
        last_evaluated_at=_iso(a.last_evaluated_at),
        last_triggered_at=_iso(a.last_triggered_at),
        last_error=a.last_error,
        created_at=a.created_at.isoformat(),
    )


async def _check_url(url: str | None) -> None:
    if url:
        err = await asyncio.to_thread(svc.validate_webhook_url, url)
        if err:
            raise APIError(err, code="INVALID_WEBHOOK", status_code=422)


async def _get(db: AsyncSession, alert_id: UUID) -> Alert:
    a = await db.get(Alert, alert_id)
    if a is None:
        raise NotFound("Alert not found")
    return a


@router.get("/alerts", response_model=list[AlertOut])
async def list_alerts(project_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    await project_access(project_id, READ, user, db)
    return [
        _out(a)
        for a in (await db.execute(select(Alert).where(Alert.project_id == project_id).order_by(Alert.name))).scalars()
    ]


@router.post("/alerts", response_model=AlertOut, status_code=201)
async def create_alert(body: AlertCreate, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    await project_access(body.project_id, WRITE, user, db)
    if body.query.metric in ("sum", "avg") and not body.query.metric_property:
        raise APIError("metric_property is required for sum/avg", code="INVALID_ALERT", status_code=422)
    await _check_url(body.webhook_url)
    a = Alert(
        project_id=body.project_id,
        name=body.name.strip(),
        enabled=body.enabled,
        query=body.query.model_dump(mode="json"),
        condition=body.condition.model_dump(),
        period=body.period,
        webhook_url=body.webhook_url,
        created_by=user.id,
    )
    db.add(a)
    await db.flush()
    return _out(a)


@router.patch("/alerts/{alert_id}", response_model=AlertOut)
async def update_alert(
    alert_id: UUID, body: AlertUpdate, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)
):
    a = await _get(db, alert_id)
    await project_access(a.project_id, WRITE, user, db)
    if body.name is not None:
        a.name = body.name.strip()
    if body.query is not None:
        a.query = body.query.model_dump(mode="json")
    if body.condition is not None:
        a.condition = body.condition.model_dump()
    if body.period is not None:
        a.period = body.period
    if body.enabled is not None:
        a.enabled = body.enabled
    if body.webhook_url_set:
        await _check_url(body.webhook_url)
        a.webhook_url = body.webhook_url
    a.updated_at = utcnow()
    return _out(a)


@router.post("/alerts/{alert_id}/evaluate", summary="Evaluate an alert right now")
async def evaluate_now(alert_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    a = await _get(db, alert_id)
    acc = await project_access(a.project_id, WRITE, user, db)
    result = await svc.evaluate_alert(db, a, acc.project)
    return {**result, "alert": _out(a)}


@router.delete("/alerts/{alert_id}", response_model=OkResponse)
async def delete_alert(alert_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    a = await _get(db, alert_id)
    await project_access(a.project_id, WRITE, user, db)
    await db.delete(a)
    return OkResponse()


# ---- notifications ------------------------------------------------------------------------------
def _n(n: Notification) -> NotificationOut:
    return NotificationOut(
        id=n.id,
        project_id=n.project_id,
        alert_id=n.alert_id,
        kind=n.kind,
        title=n.title,
        body=n.body,
        data=n.data,
        created_at=n.created_at.isoformat(),
        read_at=_iso(n.read_at),
    )


@router.get("/notifications")
async def list_notifications(
    project_id: UUID,
    unread_only: bool = False,
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    await project_access(project_id, READ, user, db)
    q = select(Notification).where(Notification.project_id == project_id)
    if unread_only:
        q = q.where(Notification.read_at.is_(None))
    rows = (await db.execute(q.order_by(Notification.created_at.desc()).limit(limit))).scalars().all()
    unread = (
        await db.execute(
            select(Notification.id).where(Notification.project_id == project_id, Notification.read_at.is_(None))
        )
    ).all()
    return {"items": [_n(n) for n in rows], "unread_count": len(unread)}


@router.post("/notifications/read-all", response_model=OkResponse)
async def read_all(project_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    await project_access(project_id, READ, user, db)
    await db.execute(
        update(Notification)
        .where(Notification.project_id == project_id, Notification.read_at.is_(None))
        .values(read_at=utcnow())
    )
    return OkResponse()


@router.post("/notifications/{notification_id}/read", response_model=OkResponse)
async def read_one(notification_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    n = await db.get(Notification, notification_id)
    if n is None:
        raise NotFound("Notification not found")
    await project_access(n.project_id, READ, user, db)
    n.read_at = n.read_at or utcnow()
    return OkResponse()


_ = Project
