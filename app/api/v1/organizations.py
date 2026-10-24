from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import client_ip, current_user, get_org_role
from app.core.errors import Conflict, Forbidden, NotFound
from app.core.permissions import MANAGE, OWNER, READ, Role
from app.db.session import get_session
from app.models import AuditLog, Invitation, Membership, Organization, Project, User
from app.schemas.common import OkResponse
from app.schemas.orgs import (
    AuditLogOut,
    InvitationOut,
    MemberAdd,
    MemberAddResult,
    MemberOut,
    MemberUpdate,
    OrgCreate,
    OrgOut,
    OrgUpdate,
)
from app.services.audit import audit
from app.services.auth import create_organization

router = APIRouter(prefix="/organizations", tags=["organizations"])


def _org_out(o: Organization, role: str) -> OrgOut:
    return OrgOut(id=o.id, name=o.name, slug=o.slug, role=role, created_at=o.created_at)


@router.get("", response_model=list[OrgOut])
async def list_orgs(user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    rows = (
        await db.execute(
            select(Organization, Membership.role)
            .join(Membership, Membership.org_id == Organization.id)
            .where(Membership.user_id == user.id)
            .order_by(Organization.created_at)
        )
    ).all()
    return [_org_out(o, r) for o, r in rows]


@router.post("", response_model=OrgOut, status_code=201)
async def create_org(
    body: OrgCreate, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)
):
    org = await create_organization(db, user.id, body.name.strip())
    await audit(
        db,
        "organization.create",
        user_id=user.id,
        org_id=org.id,
        target_type="organization",
        target_id=org.id,
        ip=client_ip(request),
    )
    return _org_out(org, "owner")


@router.get("/{org_id}", response_model=OrgOut)
async def get_org(org_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    role = await get_org_role(db, user, org_id, READ)
    return _org_out(await db.get(Organization, org_id), role.name)  # type: ignore[arg-type]


@router.patch("/{org_id}", response_model=OrgOut)
async def update_org(
    org_id: UUID,
    body: OrgUpdate,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    role = await get_org_role(db, user, org_id, MANAGE)
    org = await db.get(Organization, org_id)
    org.name = body.name.strip()  # type: ignore[union-attr]
    await audit(db, "organization.update", user_id=user.id, org_id=org_id, ip=client_ip(request))
    return _org_out(org, role.name)  # type: ignore[arg-type]


@router.delete("/{org_id}", response_model=OkResponse)
async def delete_org(
    org_id: UUID, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)
):
    await get_org_role(db, user, org_id, OWNER)
    from app.services.projects import push_purge_job

    for pid in (await db.execute(select(Project.id).where(Project.org_id == org_id))).scalars().all():
        await push_purge_job(pid)
    await audit(db, "organization.delete", user_id=user.id, org_id=org_id, ip=client_ip(request))
    await db.execute(delete(Organization).where(Organization.id == org_id))
    return OkResponse()


# ---- members -------------------------------------------------------------------------------------
def _member_out(m: Membership, u: User) -> MemberOut:
    return MemberOut(user_id=u.id, email=u.email, name=u.name, role=m.role, joined_at=m.created_at)


@router.get("/{org_id}/members", response_model=list[MemberOut])
async def list_members(org_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    await get_org_role(db, user, org_id, READ)
    rows = (
        await db.execute(
            select(Membership, User)
            .join(User, User.id == Membership.user_id)
            .where(Membership.org_id == org_id)
            .order_by(Membership.created_at)
        )
    ).all()
    return [_member_out(m, u) for m, u in rows]


def _assert_can_assign(actor: Role, new_role: Role, target_current: Role | None) -> None:
    """Owners manage everyone. Admins may only manage member/viewer accounts and assign member/viewer."""
    if actor == Role.owner:
        return
    if new_role >= Role.admin or (target_current is not None and target_current >= Role.admin):
        raise Forbidden("Only owners can grant or change admin and owner roles")


async def _owner_count(db: AsyncSession, org_id: UUID) -> int:
    return (
        await db.execute(
            select(func.count()).select_from(Membership).where(Membership.org_id == org_id, Membership.role == "owner")
        )
    ).scalar_one()


@router.post("/{org_id}/members", response_model=MemberAddResult, status_code=201)
async def add_member(
    org_id: UUID,
    body: MemberAdd,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    actor = await get_org_role(db, user, org_id, MANAGE)
    new_role = Role.parse(body.role)
    _assert_can_assign(actor, new_role, None)
    email = body.email.lower()
    target = (await db.execute(select(User).where(func.lower(User.email) == email))).scalar_one_or_none()
    if target is not None:
        if (
            await db.execute(select(Membership.id).where(Membership.org_id == org_id, Membership.user_id == target.id))
        ).first():
            raise Conflict("This user is already a member", code="ALREADY_MEMBER")
        m = Membership(org_id=org_id, user_id=target.id, role=body.role)
        db.add(m)
        await db.flush()
        await audit(
            db,
            "member.add",
            user_id=user.id,
            org_id=org_id,
            target_type="user",
            target_id=target.id,
            meta={"role": body.role},
            ip=client_ip(request),
        )
        return MemberAddResult(status="added", member=_member_out(m, target))
    inv = (
        await db.execute(
            select(Invitation).where(
                Invitation.org_id == org_id, func.lower(Invitation.email) == email, Invitation.accepted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if inv is None:
        inv = Invitation(org_id=org_id, email=email, role=body.role, invited_by=user.id)
        db.add(inv)
    else:
        inv.role = body.role
    await db.flush()
    await audit(
        db,
        "member.invite",
        user_id=user.id,
        org_id=org_id,
        meta={"email": email, "role": body.role},
        ip=client_ip(request),
    )
    return MemberAddResult(
        status="invited", invitation=InvitationOut(id=inv.id, email=inv.email, role=inv.role, created_at=inv.created_at)
    )


@router.patch("/{org_id}/members/{user_id}", response_model=MemberOut)
async def update_member(
    org_id: UUID,
    user_id: UUID,
    body: MemberUpdate,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    actor = await get_org_role(db, user, org_id, MANAGE)
    m = (
        await db.execute(select(Membership).where(Membership.org_id == org_id, Membership.user_id == user_id))
    ).scalar_one_or_none()
    if m is None:
        raise NotFound("Member not found")
    _assert_can_assign(actor, Role.parse(body.role), Role.parse(m.role))
    if m.role == "owner" and body.role != "owner" and await _owner_count(db, org_id) <= 1:
        raise Conflict("An organization must keep at least one owner", code="LAST_OWNER")
    old = m.role
    m.role = body.role
    target = await db.get(User, user_id)
    await audit(
        db,
        "member.role_change",
        user_id=user.id,
        org_id=org_id,
        target_type="user",
        target_id=user_id,
        meta={"from": old, "to": body.role},
        ip=client_ip(request),
    )
    return _member_out(m, target)  # type: ignore[arg-type]


@router.delete("/{org_id}/members/{user_id}", response_model=OkResponse)
async def remove_member(
    org_id: UUID,
    user_id: UUID,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    actor = await get_org_role(db, user, org_id, READ)
    m = (
        await db.execute(select(Membership).where(Membership.org_id == org_id, Membership.user_id == user_id))
    ).scalar_one_or_none()
    if m is None:
        raise NotFound("Member not found")
    if user_id != user.id:  # removing someone else needs management rights over them
        if actor < MANAGE:
            raise Forbidden("Only admins can remove other members")
        _assert_can_assign(actor, Role.viewer, Role.parse(m.role))
    if m.role == "owner" and await _owner_count(db, org_id) <= 1:
        raise Conflict("An organization must keep at least one owner", code="LAST_OWNER")
    await db.delete(m)
    await audit(
        db,
        "member.remove",
        user_id=user.id,
        org_id=org_id,
        target_type="user",
        target_id=user_id,
        ip=client_ip(request),
    )
    return OkResponse()


@router.get("/{org_id}/invitations", response_model=list[InvitationOut])
async def list_invitations(org_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    await get_org_role(db, user, org_id, MANAGE)
    rows = (
        (
            await db.execute(
                select(Invitation)
                .where(Invitation.org_id == org_id, Invitation.accepted_at.is_(None))
                .order_by(Invitation.created_at)
            )
        )
        .scalars()
        .all()
    )
    return [InvitationOut(id=i.id, email=i.email, role=i.role, created_at=i.created_at) for i in rows]


@router.delete("/{org_id}/invitations/{invitation_id}", response_model=OkResponse)
async def revoke_invitation(
    org_id: UUID, invitation_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)
):
    await get_org_role(db, user, org_id, MANAGE)
    res = await db.execute(delete(Invitation).where(Invitation.id == invitation_id, Invitation.org_id == org_id))
    if res.rowcount == 0:
        raise NotFound("Invitation not found")
    return OkResponse()


@router.get("/{org_id}/audit-logs", response_model=list[AuditLogOut])
async def audit_logs(
    org_id: UUID,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_session),
):
    await get_org_role(db, user, org_id, MANAGE)
    rows = (
        (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.org_id == org_id)
                .order_by(AuditLog.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return [
        AuditLogOut(
            id=r.id,
            action=r.action,
            user_id=r.user_id,
            project_id=r.project_id,
            target_type=r.target_type,
            target_id=r.target_id,
            metadata=r.meta,
            ip=str(r.ip) if r.ip else None,
            created_at=r.created_at,
        )
        for r in rows
    ]
