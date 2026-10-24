"""Redis fixed-window rate limiter with an in-process fallback.

Security posture: the limiter FAILS CLOSED by default. If Redis is unreachable the
request is rejected rather than allowed through unbounded -- a Redis outage must
never become a rate-limit bypass (see BUG-003).

When `fallback=True` an in-memory fixed-window counter keeps the app alive during a
transient Redis outage instead of hard-failing every request. This is a circuit
breaker, not a security boundary: it is per-process and resets on restart, so it
cannot be used to enforce a global limit, only to avoid a total outage.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from app.core.redis import get_redis

logger = logging.getLogger("app.ratelimit")

# In-memory fallback: {key: (window, count)}. Bounded by process lifetime only.
_FALLBACK: dict[str, tuple[int, int]] = {}
_FALLBACK_LOCK = asyncio.Lock()
_FALLBACK_MAX_KEYS = 50_000


@dataclass
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    reset_seconds: int
    source: str = "redis"

    def headers(self) -> dict[str, str]:
        h = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(self.remaining, 0)),
            "X-RateLimit-Reset": str(self.reset_seconds),
        }
        if not self.allowed:
            h["Retry-After"] = str(max(self.reset_seconds, 1))
        return h


async def _fallback_check(key: str, limit: int, window_seconds: int) -> RateLimitResult:
    """Per-process fixed-window limiter. Used only as a circuit breaker."""
    now = time.time()
    window = int(now // window_seconds)
    redis_key = f"rl:{key}:{window}"
    async with _FALLBACK_LOCK:
        if len(_FALLBACK) > _FALLBACK_MAX_KEYS:
            # Evict expired windows to keep memory bounded.
            stale = [k for k, (w, _) in _FALLBACK.items() if w < window]
            for k in stale:
                _FALLBACK.pop(k, None)
        cur = _FALLBACK.get(redis_key, (window, 0))
        count = cur[1] + 1
        _FALLBACK[redis_key] = (window, count)
    reset = max(int(window_seconds - (now % window_seconds) + 0.999), 1)
    return RateLimitResult(count <= limit, limit, max(limit - count, 0), reset, source="fallback")


async def check_rate_limit(
    key: str,
    limit: int,
    window_seconds: int = 1,
    *,
    fallback: bool = False,
) -> RateLimitResult:
    """Rate-limit `key` against `limit` per `window_seconds`.

    Args:
        key: unique key (already includes bucket + client identity).
        limit: max requests allowed per window.
        window_seconds: window duration.
        fallback: if True, use an in-process limiter when Redis is down so the
            app stays live. If False (default), fail CLOSED and reject the
            request when Redis is unavailable.

    Returns:
        RateLimitResult with `allowed` flag and response headers.
    """
    now = time.time()
    window = int(now // window_seconds)
    redis_key = f"rl:{key}:{window}"
    reset = max(int(window_seconds - (now % window_seconds) + 0.999), 1)
    try:
        r = get_redis()
        pipe = r.pipeline()
        pipe.incr(redis_key)
        pipe.expire(redis_key, window_seconds + 1)
        count, _ = await pipe.execute()
    except Exception as exc:
        logger.warning("rate limiter unavailable (Redis): %s", exc)
        if fallback:
            logger.warning("using in-process fallback limiter for %s", key)
            return await _fallback_check(key, limit, window_seconds)
        # FAIL CLOSED: a Redis outage must not bypass rate limiting.
        return RateLimitResult(False, limit, 0, reset, source="fail-closed")
    return RateLimitResult(count <= limit, limit, max(limit - count, 0), reset)
