from __future__ import annotations

import ipaddress
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog

_SECRET_KEYS = {"password", "token", "key", "secret", "api_key"}


def _clean(meta: dict[str, Any]) -> dict[str, Any]:
    return {k: ("[redacted]" if k.lower() in _SECRET_KEYS else v) for k, v in meta.items()}


async def audit(
    db: AsyncSession,
    action: str,
    *,
    user_id: UUID | None = None,
    org_id: UUID | None = None,
    project_id: UUID | None = None,
    target_type: str | None = None,
    target_id: object | None = None,
    meta: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    try:
        ip_val = str(ipaddress.ip_address(ip)) if ip else None
    except ValueError:
        ip_val = None
    db.add(
        AuditLog(
            action=action,
            user_id=user_id,
            org_id=org_id,
            project_id=project_id,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            meta=_clean(meta or {}),
            ip=ip_val,
        )
    )
