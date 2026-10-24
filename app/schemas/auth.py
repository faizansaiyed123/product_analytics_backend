from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.schemas.common import ORM


def _check_password(v: str) -> str:
    if len(v) < 10:
        raise ValueError("Password must be at least 10 characters")
    if len(v) > 128:
        raise ValueError("Password must be at most 128 characters")
    if v.lower() == v or v.upper() == v or not any(c.isdigit() for c in v):
        raise ValueError("Password must mix upper and lower case letters and include a digit")
    return v


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    name: str = Field("", max_length=200)
    organization_name: str | None = Field(None, min_length=1, max_length=200)

    _pw = field_validator("password")(_check_password)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class UserOut(ORM):
    id: UUID
    email: str
    name: str
    email_verified: bool = False
    created_at: datetime


class MembershipOut(BaseModel):
    org_id: UUID
    org_name: str
    org_slug: str
    role: str


class MeResponse(BaseModel):
    user: UserOut
    organizations: list[MembershipOut]
    csrf_token: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str

    _pw = field_validator("new_password")(_check_password)


class EmailRequest(BaseModel):
    email: EmailStr


class ResetConfirmRequest(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    new_password: str

    _pw = field_validator("new_password")(_check_password)


class TokenRequest(BaseModel):
    token: str = Field(min_length=10, max_length=200)
