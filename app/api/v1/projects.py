from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ProjectAccess, client_ip, current_user, get_org_role, get_project_access
from app.core.errors import APIError, NotFound
from app.core.permissions import MANAGE, OWNER, READ, Role
from app.core.timeutil import utcnow
from app.db.session import get_session
from app.models import ApiKey, ApiUsage, Event, Membership, Person, Project, User
from app.schemas.common import OkResponse
from app.schemas.projects import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyOut,
    ApiKeyRotate,
    ProjectCreate,
    ProjectCreated,
    ProjectOut,
    ProjectSettings,
    ProjectUpdate,
    PurgeRequest,
    UsageDay,
    UsageResponse,
)
from app.services import projects as svc
from app.services import usage as usage_svc
from app.services.audit import audit

router = APIRouter(prefix="/projects", tags=["projects"])


def _out(p: Project, role: Role) -> ProjectOut:
    return ProjectOut(
        id=p.id,
        org_id=p.org_id,
        name=p.name,
        settings=ProjectSettings.model_validate(p.settings or {}),
        role=role.name,
        created_at=p.created_at,
    )


def _key_created(key: ApiKey, raw: str) -> ApiKeyCreated:
    return ApiKeyCreated(
        id=key.id,
        name=key.name,
        key_prefix=key.key_prefix,
        created_at=key.created_at,
        last_used_at=key.last_used_at,
        revoked_at=key.revoked_at,
        expires_at=key.expires_at,
        key=raw,
    )


async def project_access(project_id: UUID, min_role: Role, user: User, db: AsyncSession) -> ProjectAccess:
    return await get_project_access(db, user, project_id, min_role)


@router.get("", response_model=list[ProjectOut])
async def list_projects(
    org_id: UUID | None = None, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)
):
    q = (
        select(Project, Membership.role)
        .join(Membership, Membership.org_id == Project.org_id)
        .where(Membership.user_id == user.id, Project.deleted_at.is_(None))
        .order_by(Project.created_at)
    )
    if org_id:
        q = q.where(Project.org_id == org_id)
    return [_out(p, Role.parse(r)) for p, r in (await db.execute(q)).all()]


@router.post("", response_model=ProjectCreated, status_code=201)
async def create_project(
    body: ProjectCreate, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)
):
    role = await get_org_role(db, user, body.org_id, MANAGE)
    settings = body.settings or ProjectSettings()
    project = Project(org_id=body.org_id, name=body.name.strip(), settings=settings.model_dump())
    db.add(project)
    await db.flush()
    key, raw = await svc.create_api_key(db, project.id, "Default", user.id)
    await audit(
        db,
        "project.create",
        user_id=user.id,
        org_id=project.org_id,
        project_id=project.id,
        target_type="project",
        target_id=project.id,
        ip=client_ip(request),
    )
    return ProjectCreated(**_out(project, role).model_dump(), api_key=_key_created(key, raw))


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(project_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    acc = await project_access(project_id, READ, user, db)
    return _out(acc.project, acc.role)


@router.patch("/{project_id}", response_model=ProjectOut)
async def update_project(
    project_id: UUID,
    body: ProjectUpdate,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    acc = await project_access(project_id, MANAGE, user, db)
    p = acc.project
    if body.name is not None:
        p.name = body.name.strip()
    if body.settings is not None:
        try:
            p.settings = svc.apply_settings_patch(acc.settings, body.settings).model_dump()
        except ValueError as exc:
            raise APIError(str(exc), code="INVALID_SETTINGS", status_code=422) from exc
        await svc.invalidate_project_keys(db, p.id)
    p.updated_at = utcnow()
    await audit(
        db,
        "project.update",
        user_id=user.id,
        org_id=p.org_id,
        project_id=p.id,
        target_type="project",
        target_id=p.id,
        meta=body.model_dump(exclude_none=True),
        ip=client_ip(request),
    )
    return _out(p, acc.role)


@router.delete("/{project_id}", response_model=OkResponse)
async def delete_project(
    project_id: UUID, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)
):
    acc = await project_access(project_id, OWNER, user, db)
    acc.project.deleted_at = utcnow()
    await svc.invalidate_project_keys(db, project_id)
    await svc.push_purge_job(project_id)
    await audit(
        db,
        "project.delete",
        user_id=user.id,
        org_id=acc.project.org_id,
        project_id=project_id,
        target_type="project",
        target_id=project_id,
        ip=client_ip(request),
    )
    return OkResponse()


# ---- API keys ------------------------------------------------------------------------------------
@router.get("/{project_id}/api-keys", response_model=list[ApiKeyOut])
async def list_keys(project_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    await project_access(project_id, MANAGE, user, db)
    return (
        (await db.execute(select(ApiKey).where(ApiKey.project_id == project_id).order_by(ApiKey.created_at.desc())))
        .scalars()
        .all()
    )


@router.post("/{project_id}/api-keys", response_model=ApiKeyCreated, status_code=201)
async def create_key(
    project_id: UUID,
    body: ApiKeyCreate,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    acc = await project_access(project_id, MANAGE, user, db)
    key, raw = await svc.create_api_key(db, project_id, body.name.strip(), user.id)
    await audit(
        db,
        "api_key.create",
        user_id=user.id,
        org_id=acc.project.org_id,
        project_id=project_id,
        target_type="api_key",
        target_id=key.id,
        meta={"name": key.name, "prefix": key.key_prefix},
        ip=client_ip(request),
    )
    return _key_created(key, raw)


@router.delete("/{project_id}/api-keys/{key_id}", response_model=ApiKeyOut)
async def revoke_key(
    project_id: UUID,
    key_id: UUID,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    acc = await project_access(project_id, MANAGE, user, db)
    key = await svc.get_key(db, project_id, key_id)
    await svc.revoke_key(db, key)
    await audit(
        db,
        "api_key.revoke",
        user_id=user.id,
        org_id=acc.project.org_id,
        project_id=project_id,
        target_type="api_key",
        target_id=key.id,
        ip=client_ip(request),
    )
    return key


@router.post("/{project_id}/api-keys/{key_id}/rotate", response_model=ApiKeyCreated, status_code=201)
async def rotate_key(
    project_id: UUID,
    key_id: UUID,
    body: ApiKeyRotate,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    acc = await project_access(project_id, MANAGE, user, db)
    old = await svc.get_key(db, project_id, key_id)
    if old.revoked_at is not None:
        raise APIError("Cannot rotate a revoked key", code="KEY_REVOKED", status_code=409)
    new_key, raw = await svc.rotate_key(db, old, user.id, body.grace_period_hours)
    await audit(
        db,
        "api_key.rotate",
        user_id=user.id,
        org_id=acc.project.org_id,
        project_id=project_id,
        target_type="api_key",
        target_id=new_key.id,
        meta={"replaced": str(old.id)},
        ip=client_ip(request),
    )
    return _key_created(new_key, raw)


# ---- usage & data management --------------------------------------------------------------------
@router.get("/{project_id}/usage", response_model=UsageResponse)
async def usage(
    project_id: UUID,
    days: int = Query(30, ge=1, le=365),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    acc = await project_access(project_id, READ, user, db)
    await usage_svc.flush(db, project_id)  # make sure in-flight Redis counters are visible
    since = utcnow() - timedelta(days=days)
    day = func.date_trunc("day", ApiUsage.bucket)
    rows = (
        await db.execute(
            select(
                day,
                func.sum(ApiUsage.requests),
                func.sum(ApiUsage.events_accepted),
                func.sum(ApiUsage.events_rejected),
                func.sum(ApiUsage.events_duplicate),
                func.sum(ApiUsage.rate_limited),
            )
            .where(ApiUsage.project_id == project_id, ApiUsage.bucket >= since)
            .group_by(day)
            .order_by(day)
        )
    ).all()
    out = [
        UsageDay(
            date=d.date().isoformat(),
            requests=int(a or 0),
            events_accepted=int(b or 0),
            events_rejected=int(c or 0),
            events_duplicate=int(e or 0),
            rate_limited=int(f or 0),
        )
        for d, a, b, c, e, f in rows
    ]
    totals = {
        k: sum(getattr(d, k) for d in out)
        for k in ("requests", "events_accepted", "events_rejected", "events_duplicate", "rate_limited")
    }
    return UsageResponse(days=out, totals=totals, rate_limit_per_second=acc.settings.rate_limit_per_second)


@router.post("/{project_id}/data/purge", response_model=OkResponse, status_code=202)
async def purge_data(
    project_id: UUID,
    body: PurgeRequest,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    """Delete ALL events and people of a project (asynchronously, in batches). Requires typing the project name."""
    acc = await project_access(project_id, OWNER, user, db)
    if body.confirm_name != acc.project.name:
        raise APIError(
            "Confirmation name does not match the project name", code="CONFIRMATION_MISMATCH", status_code=422
        )
    await svc.push_purge_job(project_id)
    await audit(
        db,
        "project.purge_data",
        user_id=user.id,
        org_id=acc.project.org_id,
        project_id=project_id,
        ip=client_ip(request),
    )
    return OkResponse()


@router.delete("/{project_id}/persons/{person_id}", response_model=OkResponse)
async def erase_person(
    project_id: UUID,
    person_id: UUID,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    """Right-to-erasure: remove one person and all of their events."""
    acc = await project_access(project_id, MANAGE, user, db)
    person = (
        await db.execute(select(Person).where(Person.id == person_id, Person.project_id == project_id))
    ).scalar_one_or_none()
    if person is None:
        raise NotFound("Person not found")
    await db.execute(delete(Event).where(Event.project_id == project_id, Event.person_id == person_id))
    await db.delete(person)
    await audit(
        db,
        "person.erase",
        user_id=user.id,
        org_id=acc.project.org_id,
        project_id=project_id,
        target_type="person",
        target_id=person_id,
        ip=client_ip(request),
    )
    return OkResponse()
