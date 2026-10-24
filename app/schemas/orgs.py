from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

Role = Literal["owner", "admin", "member", "viewer"]


class OrgCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class OrgUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class OrgOut(BaseModel):
    id: UUID
    name: str
    slug: str
    role: str
    created_at: datetime


class MemberOut(BaseModel):
    user_id: UUID
    email: str
    name: str
    role: str
    joined_at: datetime


class MemberAdd(BaseModel):
    email: EmailStr
    role: Role = "member"


class MemberUpdate(BaseModel):
    role: Role


class InvitationOut(BaseModel):
    id: UUID
    email: str
    role: str
    created_at: datetime


class MemberAddResult(BaseModel):
    status: Literal["added", "invited"]
    member: MemberOut | None = None
    invitation: InvitationOut | None = None


class AuditLogOut(BaseModel):
    id: UUID
    action: str
    user_id: UUID | None
    project_id: UUID | None
    target_type: str | None
    target_id: str | None
    metadata: dict
    ip: str | None
    created_at: datetime
