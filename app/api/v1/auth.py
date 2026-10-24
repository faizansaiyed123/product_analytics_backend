from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthContext, client_ip, current_auth
from app.core.config import get_settings
from app.core.errors import APIError, RateLimited
from app.core.ratelimit import check_rate_limit
from app.core.security import csrf_token_for_session, hash_password, verify_password
from app.db.session import get_session
from app.models import Membership, Organization
from app.schemas.auth import (
    ChangePasswordRequest,
    EmailRequest,
    LoginRequest,
    MembershipOut,
    MeResponse,
    RegisterRequest,
    ResetConfirmRequest,
    TokenRequest,
    UserOut,
)
from app.schemas.common import OkResponse
from app.services import auth as svc
from app.services.audit import audit

router = APIRouter(prefix="/auth", tags=["auth"])


def _user_out(user) -> UserOut:  # type: ignore[no-untyped-def]
    return UserOut(
        id=user.id,
        email=user.email,
        name=user.name,
        email_verified=user.email_verified_at is not None,
        created_at=user.created_at,
    )


async def _limit(request: Request, bucket: str, extra: str = "") -> None:
    res = await check_rate_limit(
        f"auth:{bucket}:{client_ip(request)}:{extra}", get_settings().auth_rate_limit_per_minute, 60
    )
    if not res.allowed:
        raise RateLimited("Too many attempts, please try again shortly", headers=res.headers())


async def _me(db: AsyncSession, user, session) -> MeResponse:  # type: ignore[no-untyped-def]
    rows = (
        await db.execute(
            select(Organization, Membership.role)
            .join(Membership, Membership.org_id == Organization.id)
            .where(Membership.user_id == user.id)
            .order_by(Organization.created_at)
        )
    ).all()
    return MeResponse(
        user=_user_out(user),
        organizations=[MembershipOut(org_id=o.id, org_name=o.name, org_slug=o.slug, role=r) for o, r in rows],
        csrf_token=csrf_token_for_session(str(session.id)),
    )


@router.post("/register", response_model=MeResponse, status_code=201)
async def register(
    body: RegisterRequest, request: Request, response: Response, db: AsyncSession = Depends(get_session)
):
    if not get_settings().allow_registration:
        raise APIError("Registration is disabled", code="REGISTRATION_DISABLED", status_code=403)
    await _limit(request, "register")
    ip = client_ip(request)
    user = await svc.register_user(
        db, email=body.email, password=body.password, name=body.name, organization_name=body.organization_name, ip=ip
    )
    raw, session = await svc.create_session(db, user, user_agent=request.headers.get("user-agent"), ip=ip)
    svc.set_session_cookies(response, raw, session)
    return await _me(db, user, session)


@router.post("/login", response_model=MeResponse)
async def login(body: LoginRequest, request: Request, response: Response, db: AsyncSession = Depends(get_session)):
    await _limit(request, "login", body.email.lower())
    ip = client_ip(request)
    try:
        user = await svc.authenticate(db, body.email, body.password)
    except APIError:
        await audit(db, "user.login_failed", meta={"email": body.email.lower()}, ip=ip)
        await db.commit()  # persist the audit row even though the request fails
        raise
    raw, session = await svc.create_session(db, user, user_agent=request.headers.get("user-agent"), ip=ip)
    svc.set_session_cookies(response, raw, session)
    await audit(db, "user.login", user_id=user.id, ip=ip)
    return await _me(db, user, session)


@router.post("/logout", response_model=OkResponse)
async def logout(
    response: Response, auth: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_session)
):
    from app.core.timeutil import utcnow
    from app.models import AuthSession

    await db.execute(update(AuthSession).where(AuthSession.id == auth.session.id).values(revoked_at=utcnow()))
    svc.clear_session_cookies(response)
    return OkResponse()


@router.get("/me", response_model=MeResponse)
async def me(auth: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_session)):
    return await _me(db, auth.user, auth.session)


@router.post("/change-password", response_model=OkResponse)
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    auth: AuthContext = Depends(current_auth),
    db: AsyncSession = Depends(get_session),
):
    if not verify_password(body.current_password, auth.user.password_hash):
        raise APIError("Current password is incorrect", code="INVALID_CREDENTIALS", status_code=400)
    auth.user.password_hash = hash_password(body.new_password)
    await db.merge(auth.user)
    await svc.revoke_all_sessions(db, auth.user.id, except_id=auth.session.id)
    await audit(db, "user.change_password", user_id=auth.user.id, ip=client_ip(request))
    return OkResponse()


@router.post("/password-reset/request", response_model=OkResponse, status_code=202)
async def password_reset_request(body: EmailRequest, request: Request, db: AsyncSession = Depends(get_session)):
    await _limit(request, "reset")
    await svc.send_password_reset(db, body.email)
    return OkResponse()  # identical response whether or not the account exists


@router.post("/password-reset/confirm", response_model=OkResponse)
async def password_reset_confirm(body: ResetConfirmRequest, db: AsyncSession = Depends(get_session)):
    await svc.reset_password(db, body.token, body.new_password)
    return OkResponse()


@router.post("/verify-email/request", response_model=OkResponse, status_code=202)
async def verify_email_request(auth: AuthContext = Depends(current_auth), db: AsyncSession = Depends(get_session)):
    if auth.user.email_verified_at is None:
        await svc.send_verification_email(db, auth.user)
    return OkResponse()


@router.post("/verify-email/confirm", response_model=OkResponse)
async def verify_email_confirm(body: TokenRequest, db: AsyncSession = Depends(get_session)):
    await svc.confirm_email(db, body.token)
    return OkResponse()
