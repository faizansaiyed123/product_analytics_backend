from __future__ import annotations

from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from app.core.config import get_settings
from app.core.redis import get_redis
from app.db.session import get_sessionmaker

router = APIRouter(tags=["ops"])


@router.get("/health", summary="Liveness probe")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready", summary="Readiness probe (database and Redis reachable)")
async def ready(response: Response) -> dict:
    checks: dict[str, str] = {}
    try:
        async with get_sessionmaker()() as s:
            await s.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {type(exc).__name__}"
    try:
        await get_redis().ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {type(exc).__name__}"
    ok = all(v == "ok" for v in checks.values())
    if not ok:
        response.status_code = 503
    return {"status": "ok" if ok else "unavailable", "checks": checks}


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    token = get_settings().metrics_token
    if token and request.headers.get("authorization") != f"Bearer {token}":
        return Response(status_code=401)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
