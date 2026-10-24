"""Lazy Redis client (one per event loop)."""

from __future__ import annotations

import asyncio

import redis.asyncio as aioredis

from app.core.config import get_settings

_clients: dict[int, aioredis.Redis] = {}


def get_redis() -> aioredis.Redis:
    loop_id = id(asyncio.get_running_loop())
    client = _clients.get(loop_id)
    if client is None:
        client = aioredis.from_url(get_settings().redis_url, decode_responses=True)
        _clients[loop_id] = client
    return client


async def close_redis() -> None:
    clients = list(_clients.values())
    _clients.clear()
    for c in clients:
        try:
            await c.aclose()
        except Exception:  # pragma: no cover - best effort
            pass
