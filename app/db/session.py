"""Async SQLAlchemy engine/session management (engine is created lazily, one per event loop)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool, NullPool

from app.core.config import get_settings

_engines: dict[int, tuple[AsyncEngine, async_sessionmaker[AsyncSession]]] = {}


def _make() -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    s = get_settings()
    kwargs: dict = {"pool_pre_ping": True}
    if s.environment == "test":
        kwargs["poolclass"] = NullPool
    else:
        # Size the pool for the number of concurrent worker processes. Each uvicorn
        # worker is an independent event loop with its own engine, so the per-process
        # pool must be sized as `total_connections / num_workers`. Defaults assume a
        # single worker; set DB_POOL_SIZE / DB_MAX_OVERFLOW in production.
        kwargs.update(
            poolclass=AsyncAdaptedQueuePool,
            pool_size=s.db_pool_size,
            max_overflow=s.db_max_overflow,
            pool_timeout=s.db_pool_timeout,
            pool_recycle=s.db_pool_recycle,
        )
    engine = create_async_engine(s.database_url, **kwargs)
    return engine, async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


def get_engine() -> AsyncEngine:
    key = id(asyncio.get_running_loop())
    if key not in _engines:
        _engines[key] = _make()
    return _engines[key][0]


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    key = id(asyncio.get_running_loop())
    if key not in _engines:
        _engines[key] = _make()
    return _engines[key][1]


async def dispose_engines() -> None:
    items = list(_engines.values())
    _engines.clear()
    for engine, _ in items:
        await engine.dispose()


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request; commit on success, rollback on error."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
