"""Worker process: `python -m app.workers.main`. A lightweight async scheduler (no Celery needed). Each task runs under
a Redis lock so multiple worker replicas never run the same task concurrently."""

from __future__ import annotations

import asyncio
import logging
import signal
import time
import uuid

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.redis import close_redis, get_redis
from app.db.session import dispose_engines
from app.workers import tasks

logger = logging.getLogger("app.worker")

# name, coroutine function, interval seconds
SCHEDULE = [
    ("flush_usage", tasks.flush_usage, 30),
    ("process_exports", tasks.process_exports, 5),
    ("purge_projects", tasks.purge_projects, 20),
    ("evaluate_alerts", tasks.evaluate_alerts, 60),
    ("refresh_cohorts", tasks.refresh_cohorts, 600),
    ("apply_retention", tasks.apply_retention, 3600),
    ("cleanup_auth", tasks.cleanup_auth, 3600),
]


async def run_locked(name: str, fn, ttl: int = 600) -> None:  # type: ignore[no-untyped-def]
    r = get_redis()
    token = uuid.uuid4().hex
    if not await r.set(f"lock:{name}", token, nx=True, ex=ttl):
        return
    try:
        await fn()
    except Exception:
        logger.exception("task failed", extra={"task": name})
    finally:
        if await r.get(f"lock:{name}") == token:
            await r.delete(f"lock:{name}")


async def main() -> None:
    s = get_settings()
    configure_logging(s.log_level, s.log_json)
    logger.info("worker started")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    next_run = {name: 0.0 for name, _, _ in SCHEDULE}
    while not stop.is_set():
        now = time.monotonic()
        for name, fn, every in SCHEDULE:
            if now >= next_run[name]:
                next_run[name] = now + every
                await run_locked(name, fn)
        try:
            await asyncio.wait_for(stop.wait(), timeout=1)
        except TimeoutError:
            pass
    logger.info("worker stopping")
    await close_redis()
    await dispose_engines()


if __name__ == "__main__":
    asyncio.run(main())
