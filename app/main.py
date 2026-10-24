from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1 import health
from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.errors import install_error_handlers
from app.core.logging import configure_logging
from app.core.middleware import (
    BodySizeLimitMiddleware,
    CORSMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from app.core.redis import close_redis, get_redis
from app.db.session import dispose_engines

DESCRIPTION = """
Self-hosted product analytics. **Ingestion** endpoints (`/api/v1/events*`) authenticate with a project API key
(`Authorization: Bearer phc_...`). **Dashboard** endpoints authenticate with an HTTP-only session cookie and require the
`X-CSRF-Token` header on unsafe methods. Errors always look like `{"error": {"code", "message", "details?"}}`.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    settings.assert_production_safe()
    if settings.is_production:
        # Fail fast: the rate limiter and API-key cache depend on Redis. A Redis
        # outage must never silently degrade the app into an open relay.
        try:
            await get_redis().ping()
        except Exception as exc:  # pragma: no cover - exercised in prod only
            logging.getLogger("app").error("Redis unreachable at startup: %s", exc)
            raise RuntimeError(f"Redis unreachable at startup: {exc}") from exc
    os.makedirs(settings.export_dir, exist_ok=True)
    logging.getLogger("app").info("starting", extra={"environment": settings.environment})
    yield
    await close_redis()
    await dispose_engines()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Product Analytics API", version="1.0.0", description=DESCRIPTION, lifespan=lifespan)
    install_error_handlers(app, debug=not settings.is_production)
    # outermost first
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(CORSMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health.router)
    app.include_router(api_router)
    return app


app = create_app()
