"""Shared FastAPI dependencies: authentication, CSRF, project/org access checks, client IP."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from fastapi import Depends, Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import APIError, Forbidden, NotFound, Unauthorized
from app.core.permissions import Role
from app.core.security import constant_time_equals, csrf_token_for_session, sha256_hex
from app.core.timeutil import utcnow
from app.db.session import get_session
from app.models import AuthSession, Membership, Project, User
from app.schemas.projects import ProjectSettings

SESSION_COOKIE = "pa_session"
CSRF_COOKIE = "pa_csrf"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def client_ip(request: Request) -> str | None:
    if get_settings().trust_proxy_headers:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else None


@dataclass
class AuthContext:
    user: User
    session: AuthSession


async def current_auth(request: Request, db: AsyncSession = Depends(get_session)) -> AuthContext:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise Unauthorized()
    now = utcnow()
    row = (
        await db.execute(
            select(AuthSession, User)
            .join(User, User.id == AuthSession.user_id)
            .where(AuthSession.token_hash == sha256_hex(token))
        )
    ).first()
    if row is None:
        raise Unauthorized("Invalid session")
    session, user = row
    if session.revoked_at is not None or session.expires_at <= now or not user.is_active:
        raise Unauthorized("Session expired")
    if request.method not in SAFE_METHODS:
        header = request.headers.get("x-csrf-token", "")
        if not header or not constant_time_equals(header, csrf_token_for_session(str(session.id))):
            raise APIError("Missing or invalid CSRF token", code="CSRF_FAILED", status_code=403)
    if session.last_seen_at is None or now - session.last_seen_at > timedelta(minutes=5):
        await db.execute(update(AuthSession).where(AuthSession.id == session.id).values(last_seen_at=now))
    return AuthContext(user=user, session=session)


async def current_user(auth: AuthContext = Depends(current_auth)) -> User:
    return auth.user


@dataclass
class ProjectAccess:
    project: Project
    role: Role
    user: User

    @property
    def settings(self) -> ProjectSettings:
        return ProjectSettings.model_validate(self.project.settings or {})


async def get_project_access(db: AsyncSession, user: User, project_id: UUID, min_role: Role) -> ProjectAccess:
    """Resolve a project for the user. Non-members get 404 (existence is not leaked)."""
    row = (
        await db.execute(
            select(Project, Membership.role)
            .join(Membership, Membership.org_id == Project.org_id)
            .where(Project.id == project_id, Project.deleted_at.is_(None), Membership.user_id == user.id)
        )
    ).first()
    if row is None:
        raise NotFound("Project not found")
    project, role_name = row
    role = Role.parse(role_name)
    if role < min_role:
        raise Forbidden(f"This action requires the {min_role.name} role or higher")
    return ProjectAccess(project=project, role=role, user=user)


async def get_org_role(db: AsyncSession, user: User, org_id: UUID, min_role: Role) -> Role:
    role_name = (
        await db.execute(select(Membership.role).where(Membership.org_id == org_id, Membership.user_id == user.id))
    ).scalar_one_or_none()
    if role_name is None:
        raise NotFound("Organization not found")
    role = Role.parse(role_name)
    if role < min_role:
        raise Forbidden(f"This action requires the {min_role.name} role or higher")
    return role
