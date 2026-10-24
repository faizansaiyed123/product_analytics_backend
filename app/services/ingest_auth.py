"""Project API key authentication for the public ingestion endpoints (Redis-cached, hashed keys)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from fastapi import Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import APIError, RateLimited, Unauthorized
from app.core.ratelimit import check_rate_limit
from app.core.redis import get_redis
from app.core.security import hash_api_key
from app.core.timeutil import utcnow
from app.models import ApiKey, Project
from app.schemas.projects import ProjectSettings
from app.services import usage
from app.services.projects import API_KEY_CACHE_TTL, api_key_cache_key

logger = logging.getLogger("app.ingest_auth")


@dataclass
class IngestProject:
    project_id: UUID
    key_id: UUID
    org_id: UUID
    settings: ProjectSettings


def extract_key(request: Request, body_key: str | None) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-api-key") or request.query_params.get("api_key") or body_key


async def authenticate_ingest(request: Request, db: AsyncSession, body_key: str | None = None) -> IngestProject:
    raw = extract_key(request, body_key)
    if not raw:
        raise Unauthorized(
            "Missing API key. Send it as 'Authorization: Bearer <key>' or 'X-API-Key'.", code="MISSING_API_KEY"
        )
    key_hash = hash_api_key(raw)
    cache_key = api_key_cache_key(key_hash)
    info: dict | None = None
    try:
        cached = await get_redis().get(cache_key)
        if cached is not None:
            info = json.loads(cached) if cached != "0" else {}
    except Exception as exc:
        logger.warning("api key cache unavailable: %s", exc)
    if info is None:
        row = (
            await db.execute(
                select(ApiKey, Project)
                .join(Project, Project.id == ApiKey.project_id)
                .where(ApiKey.key_hash == key_hash, ApiKey.revoked_at.is_(None), Project.deleted_at.is_(None))
            )
        ).first()
        if row is None:
            info = {}
        else:
            key, project = row
            info = {
                "project_id": str(project.id),
                "key_id": str(key.id),
                "org_id": str(project.org_id),
                "settings": ProjectSettings.model_validate(project.settings or {}).model_dump(),
                "expires_at": key.expires_at.isoformat() if key.expires_at else None,
            }
        try:
            await get_redis().set(cache_key, json.dumps(info) if info else "0", ex=API_KEY_CACHE_TTL if info else 10)
        except Exception:
            pass
    if not info:
        raise Unauthorized("Invalid API key", code="INVALID_API_KEY")
    if info.get("expires_at") and datetime.fromisoformat(info["expires_at"]) <= utcnow():
        raise Unauthorized("API key has expired", code="INVALID_API_KEY")
    project = IngestProject(
        UUID(info["project_id"]),
        UUID(info["key_id"]),
        UUID(info["org_id"]),
        ProjectSettings.model_validate(info["settings"]),
    )
    limit = await check_rate_limit(
        f"proj:{project.project_id}", project.settings.rate_limit_per_second, 1, fallback=True
    )
    if not limit.allowed:
        await usage.record(project.project_id, requests=1, rate_limited=1)
        raise RateLimited(
            f"Rate limit of {limit.limit} requests/second exceeded for this project", headers=limit.headers()
        )
    request.state.rate_headers = limit.headers()
    # throttle last_used_at writes to once a minute per key
    try:
        if await get_redis().set(f"lastused:{project.key_id}", "1", nx=True, ex=60):
            await db.execute(update(ApiKey).where(ApiKey.id == project.key_id).values(last_used_at=utcnow()))
    except Exception:
        pass
    return project


_ = APIError
