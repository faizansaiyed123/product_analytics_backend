from __future__ import annotations

import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.core.timeutil import is_valid_tz
from app.schemas.common import ORM

DEFAULT_DENYLIST = ["password", "passwd", "secret", "token", "credit_card", "card_number", "cvv", "ssn"]
RETENTION_CHOICES = (7, 30, 90, 180, 365)


class ProjectSettings(BaseModel):
    timezone: str = "UTC"
    session_timeout_minutes: int = Field(30, ge=1, le=1440)
    retention_days: int | None = Field(None, description="null = keep forever")
    ip_mode: Literal["store", "anonymize", "discard"] = "anonymize"
    rate_limit_per_second: int = Field(100, ge=1, le=100_000)
    property_denylist: list[str] = Field(default_factory=lambda: list(DEFAULT_DENYLIST), max_length=200)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        if not is_valid_tz(v):
            raise ValueError(f"Unknown timezone {v!r}")
        return v

    @field_validator("retention_days")
    @classmethod
    def _ret(cls, v: int | None) -> int | None:
        if v is not None and v not in RETENTION_CHOICES:
            raise ValueError(f"retention_days must be null or one of {list(RETENTION_CHOICES)}")
        return v

    @field_validator("property_denylist")
    @classmethod
    def _deny(cls, v: list[str]) -> list[str]:
        return sorted({s.strip().lower() for s in v if s.strip()})


class ProjectSettingsPatch(BaseModel):
    timezone: str | None = None
    session_timeout_minutes: int | None = Field(None, ge=1, le=1440)
    retention_days: int | None = None
    retention_days_set: bool = Field(False, description="set true to apply retention_days (allows null)")
    ip_mode: Literal["store", "anonymize", "discard"] | None = None
    rate_limit_per_second: int | None = Field(None, ge=1, le=100_000)
    property_denylist: list[str] | None = None


class ProjectCreate(BaseModel):
    org_id: UUID
    name: str = Field(min_length=1, max_length=200)
    settings: ProjectSettings | None = None


class ProjectUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    settings: ProjectSettingsPatch | None = None


class ProjectOut(BaseModel):
    id: UUID
    org_id: UUID
    name: str
    settings: ProjectSettings
    role: str
    created_at: datetime


class ApiKeyOut(ORM):
    id: UUID
    name: str
    key_prefix: str
    created_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    expires_at: datetime | None = None


class ApiKeyCreated(ApiKeyOut):
    key: str = Field(description="Full key. Shown exactly once - only a hash is stored.")


class ApiKeyCreate(BaseModel):
    name: str = Field("Default", min_length=1, max_length=200)


class ApiKeyRotate(BaseModel):
    grace_period_hours: int = Field(0, ge=0, le=24 * 30)


class ProjectCreated(ProjectOut):
    api_key: ApiKeyCreated


class UsageDay(BaseModel):
    date: str
    requests: int
    events_accepted: int
    events_rejected: int
    events_duplicate: int
    rate_limited: int


class UsageResponse(BaseModel):
    days: list[UsageDay]
    totals: dict[str, int]
    rate_limit_per_second: int


class PurgeRequest(BaseModel):
    confirm_name: str


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    return _SLUG_RE.sub("-", name.lower()).strip("-")[:60] or "org"
