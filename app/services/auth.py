from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from fastapi import Response
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import APIError, Conflict, Unauthorized
from app.core.security import (
    DUMMY_PASSWORD_HASH,
    csrf_token_for_session,
    hash_password,
    new_token,
    password_needs_rehash,
    sha256_hex,
    verify_password,
)
from app.core.timeutil import utcnow
from app.models import AuthSession, AuthToken, Invitation, Membership, Organization, User
from app.schemas.projects import slugify
from app.services import mailer
from app.services.audit import audit

SESSION_COOKIE = "pa_session"
CSRF_COOKIE = "pa_csrf"


async def unique_slug(db: AsyncSession, name: str) -> str:
    base = slugify(name)
    slug, n = base, 1
    while (await db.execute(select(Organization.id).where(Organization.slug == slug))).first():
        n += 1
        slug = f"{base}-{n}"
    return slug


async def create_organization(db: AsyncSession, user_id: UUID, name: str) -> Organization:
    org = Organization(name=name, slug=await unique_slug(db, name))
    db.add(org)
    await db.flush()
    db.add(Membership(org_id=org.id, user_id=user_id, role="owner"))
    return org


async def register_user(
    db: AsyncSession, *, email: str, password: str, name: str, organization_name: str | None, ip: str | None
) -> User:
    email = email.strip().lower()
    exists = (await db.execute(select(User.id).where(func.lower(User.email) == email))).first()
    if exists:
        raise Conflict("An account with this email already exists", code="EMAIL_TAKEN")
    user = User(email=email, password_hash=hash_password(password), name=name.strip())
    db.add(user)
    await db.flush()
    # Apply pending invitations for this email.
    invites = (
        (
            await db.execute(
                select(Invitation).where(func.lower(Invitation.email) == email, Invitation.accepted_at.is_(None))
            )
        )
        .scalars()
        .all()
    )
    for inv in invites:
        db.add(Membership(org_id=inv.org_id, user_id=user.id, role=inv.role))
        inv.accepted_at = utcnow()
    if organization_name:
        org = await create_organization(db, user.id, organization_name.strip())
        await audit(
            db,
            "organization.create",
            user_id=user.id,
            org_id=org.id,
            target_type="organization",
            target_id=org.id,
            ip=ip,
        )
    await audit(db, "user.register", user_id=user.id, ip=ip)
    await send_verification_email(db, user)
    return user


async def _issue_token(db: AsyncSession, user: User, purpose: str, ttl: timedelta) -> str:
    raw = new_token()
    db.add(AuthToken(user_id=user.id, purpose=purpose, token_hash=sha256_hex(raw), expires_at=utcnow() + ttl))
    return raw


async def send_verification_email(db: AsyncSession, user: User) -> None:
    s = get_settings()
    raw = await _issue_token(db, user, "email_verify", timedelta(hours=s.email_verification_ttl_hours))
    link = f"{s.public_app_url}/verify-email?token={raw}"
    await mailer.send_email(db, user.email, "Verify your email", f"Open this link to verify your email:\n{link}\n")


async def send_password_reset(db: AsyncSession, email: str) -> None:
    s = get_settings()
    user = (
        await db.execute(select(User).where(func.lower(User.email) == email.lower(), User.is_active))
    ).scalar_one_or_none()
    if user is None:
        return  # never reveal whether the account exists
    raw = await _issue_token(db, user, "password_reset", timedelta(minutes=s.password_reset_ttl_minutes))
    link = f"{s.public_app_url}/reset-password?token={raw}"
    await mailer.send_email(db, user.email, "Reset your password", f"Open this link to reset your password:\n{link}\n")


async def _consume_token(db: AsyncSession, raw: str, purpose: str) -> AuthToken:
    tok = (
        await db.execute(
            select(AuthToken)
            .where(AuthToken.token_hash == sha256_hex(raw), AuthToken.purpose == purpose)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if tok is None or tok.used_at is not None or tok.expires_at <= utcnow():
        raise APIError("This link is invalid or has expired", code="INVALID_TOKEN", status_code=400)
    tok.used_at = utcnow()
    return tok


async def confirm_email(db: AsyncSession, raw: str) -> None:
    tok = await _consume_token(db, raw, "email_verify")
    await db.execute(
        update(User).where(User.id == tok.user_id, User.email_verified_at.is_(None)).values(email_verified_at=utcnow())
    )


async def reset_password(db: AsyncSession, raw: str, new_password: str) -> None:
    tok = await _consume_token(db, raw, "password_reset")
    await db.execute(update(User).where(User.id == tok.user_id).values(password_hash=hash_password(new_password)))
    await revoke_all_sessions(db, tok.user_id)
    await audit(db, "user.password_reset", user_id=tok.user_id)


async def revoke_all_sessions(db: AsyncSession, user_id: UUID, *, except_id: UUID | None = None) -> None:
    q = update(AuthSession).where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
    if except_id:
        q = q.where(AuthSession.id != except_id)
    await db.execute(q.values(revoked_at=utcnow()))


async def authenticate(db: AsyncSession, email: str, password: str) -> User:
    user = (await db.execute(select(User).where(func.lower(User.email) == email.strip().lower()))).scalar_one_or_none()
    if user is None:
        verify_password(password, DUMMY_PASSWORD_HASH)
        raise Unauthorized("Invalid email or password", code="INVALID_CREDENTIALS")
    if not verify_password(password, user.password_hash) or not user.is_active:
        raise Unauthorized("Invalid email or password", code="INVALID_CREDENTIALS")
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    user.last_login_at = utcnow()
    return user


async def create_session(
    db: AsyncSession, user: User, *, user_agent: str | None, ip: str | None
) -> tuple[str, AuthSession]:
    raw = new_token(32)
    sess = AuthSession(
        user_id=user.id,
        token_hash=sha256_hex(raw),
        user_agent=(user_agent or "")[:500] or None,
        expires_at=utcnow() + timedelta(days=get_settings().session_ttl_days),
        last_seen_at=utcnow(),
        ip=ip if ip else None,
    )
    db.add(sess)
    await db.flush()
    return raw, sess


def set_session_cookies(response: Response, raw_token: str, session: AuthSession) -> str:
    s = get_settings()
    max_age = s.session_ttl_days * 86400
    csrf = csrf_token_for_session(str(session.id))
    response.set_cookie(
        SESSION_COOKIE, raw_token, max_age=max_age, httponly=True, secure=s.cookie_secure, samesite="lax", path="/"
    )
    response.set_cookie(
        CSRF_COOKIE, csrf, max_age=max_age, httponly=False, secure=s.cookie_secure, samesite="lax", path="/"
    )
    return csrf


def clear_session_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
