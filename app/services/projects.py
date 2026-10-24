from __future__ import annotations

import json
from datetime import timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFound
from app.core.redis import get_redis
from app.core.security import generate_api_key
from app.core.timeutil import utcnow
from app.models import ApiKey, Project
from app.schemas.projects import ProjectSettings, ProjectSettingsPatch

API_KEY_CACHE_TTL = 60


def api_key_cache_key(key_hash: str) -> str:
    return f"apikey:{key_hash}"


async def invalidate_key_cache(key_hashes: list[str]) -> None:
    if not key_hashes:
        return
    try:
        await get_redis().delete(*[api_key_cache_key(h) for h in key_hashes])
    except Exception:  # cache invalidation is best effort; TTL bounds staleness to API_KEY_CACHE_TTL
        pass


async def invalidate_project_keys(db: AsyncSession, project_id: UUID) -> None:
    hashes = (await db.execute(select(ApiKey.key_hash).where(ApiKey.project_id == project_id))).scalars().all()
    await invalidate_key_cache(list(hashes))


async def create_api_key(db: AsyncSession, project_id: UUID, name: str, user_id: UUID | None) -> tuple[ApiKey, str]:
    raw, prefix, key_hash = generate_api_key()
    key = ApiKey(project_id=project_id, name=name, key_prefix=prefix, key_hash=key_hash, created_by=user_id)
    db.add(key)
    await db.flush()
    return key, raw


async def get_key(db: AsyncSession, project_id: UUID, key_id: UUID) -> ApiKey:
    key = (
        await db.execute(select(ApiKey).where(ApiKey.id == key_id, ApiKey.project_id == project_id))
    ).scalar_one_or_none()
    if key is None:
        raise NotFound("API key not found")
    return key


async def revoke_key(db: AsyncSession, key: ApiKey, *, at=None) -> None:  # type: ignore[no-untyped-def]
    key.revoked_at = key.revoked_at or (at or utcnow())
    await invalidate_key_cache([key.key_hash])


async def rotate_key(db: AsyncSession, key: ApiKey, user_id: UUID | None, grace_hours: int) -> tuple[ApiKey, str]:
    new_key, raw = await create_api_key(db, key.project_id, key.name, user_id)
    if grace_hours > 0:
        key.expires_at = utcnow() + timedelta(hours=grace_hours)
        await invalidate_key_cache([key.key_hash])
    else:
        await revoke_key(db, key)
    return new_key, raw


def apply_settings_patch(current: ProjectSettings, patch: ProjectSettingsPatch) -> ProjectSettings:
    data = current.model_dump()
    for field in ("timezone", "session_timeout_minutes", "ip_mode", "rate_limit_per_second", "property_denylist"):
        val = getattr(patch, field)
        if val is not None:
            data[field] = val
    if patch.retention_days_set:
        data["retention_days"] = patch.retention_days
    return ProjectSettings.model_validate(data)


async def push_purge_job(project_id: UUID) -> None:
    await get_redis().rpush("jobs:purge_project_data", json.dumps({"project_id": str(project_id)}))


_ = Project
