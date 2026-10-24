"""Background tasks. Each is idempotent and safe to run concurrently from several workers (guarded by a Redis lock)."""

from __future__ import annotations

import json
import logging
import os
from datetime import timedelta

from sqlalchemy import delete, select, text, update

from app.analytics.queries import CohortDefinition
from app.core.config import get_settings
from app.core.redis import get_redis
from app.core.timeutil import utcnow
from app.db.session import get_sessionmaker
from app.models import Alert, AuthSession, AuthToken, Cohort, Event, ExportJob, Person, Project
from app.schemas.projects import ProjectSettings
from app.services import usage
from app.services.alerts import evaluate_alert
from app.services.analytics_service import AnalyticsService

logger = logging.getLogger("app.worker")
BATCH = 10_000


async def delete_events_batched(db_factory, where_sql: str, params: dict) -> int:  # type: ignore[no-untyped-def]
    """Delete in small transactions so a huge purge never holds long locks or bloats WAL in one go."""
    total = 0
    while True:
        async with db_factory() as db:
            res = await db.execute(text(f"DELETE FROM events WHERE id IN (SELECT id FROM events WHERE {where_sql} LIMIT {BATCH})"), params)
            await db.commit()
        total += res.rowcount or 0
        if (res.rowcount or 0) < BATCH:
            return total


async def flush_usage() -> None:
    async with get_sessionmaker()() as db:
        n = await usage.flush(db)
        await db.commit()
    if n:
        logger.info("flushed usage counters", extra={"buckets": n})


async def evaluate_alerts() -> None:
    sm = get_sessionmaker()
    async with sm() as db:
        alerts = (await db.execute(select(Alert).where(Alert.enabled))).scalars().all()
        for a in alerts:
            interval = {"hour": timedelta(minutes=5), "day": timedelta(minutes=30), "week": timedelta(hours=2)}[a.period]
            if a.last_evaluated_at and utcnow() - a.last_evaluated_at < interval:
                continue
            p = await db.get(Project, a.project_id)
            if p is None or p.deleted_at:
                continue
            await evaluate_alert(db, a, ProjectSettings.model_validate(p.settings or {}))
            await db.commit()


async def refresh_cohorts() -> None:
    async with get_sessionmaker()() as db:
        for c in (await db.execute(select(Cohort))).scalars().all():
            p = await db.get(Project, c.project_id)
            if p is None or p.deleted_at:
                continue
            svc = AnalyticsService(db, p.id, ProjectSettings.model_validate(p.settings or {}))
            try:
                c.last_count = await svc.repo.cohort_size(await svc.cohort_scope(), CohortDefinition.model_validate(c.definition))
                c.last_calculated_at = utcnow()
                await db.commit()
            except Exception:
                await db.rollback()
                logger.exception("cohort refresh failed", extra={"cohort_id": str(c.id)})


async def apply_retention() -> None:
    """Delete events (and people left with no events) older than each project's retention window."""
    sm = get_sessionmaker()
    async with sm() as db:
        projects = (await db.execute(select(Project).where(Project.deleted_at.is_(None)))).scalars().all()
    for p in projects:
        days = ProjectSettings.model_validate(p.settings or {}).retention_days
        if not days:
            continue
        cutoff = utcnow() - timedelta(days=days)
        n = await delete_events_batched(sm, "project_id = :pid AND timestamp < :cutoff", {"pid": p.id, "cutoff": cutoff})
        async with sm() as db:
            res = await db.execute(text(
                "DELETE FROM persons p WHERE p.project_id = :pid AND p.last_seen < :cutoff "
                "AND NOT EXISTS (SELECT 1 FROM events e WHERE e.project_id = p.project_id AND e.person_id = p.id)"), {"pid": p.id, "cutoff": cutoff})
            await db.commit()
        if n or res.rowcount:
            logger.info("retention applied", extra={"project_id": str(p.id), "events_deleted": n, "persons_deleted": res.rowcount})


async def purge_projects() -> None:
    """Hard-delete soft-deleted projects and process explicit data-purge requests."""
    sm = get_sessionmaker()
    r = get_redis()
    while raw := await r.lpop("jobs:purge_project_data"):
        pid = json.loads(raw)["project_id"]
        await delete_events_batched(sm, "project_id = :pid", {"pid": pid})
        async with sm() as db:
            await db.execute(delete(Person).where(Person.project_id == pid))
            for table in ("event_definitions", "property_definitions", "api_usage"):
                await db.execute(text(f"DELETE FROM {table} WHERE project_id = :pid"), {"pid": pid})
            await db.commit()
        logger.info("project data purged", extra={"project_id": pid})
    async with sm() as db:
        for p in (await db.execute(select(Project).where(Project.deleted_at.is_not(None)))).scalars().all():
            if (await db.execute(select(Event.id).where(Event.project_id == p.id).limit(1))).first() is None:
                await db.execute(delete(Person).where(Person.project_id == p.id))
                await db.delete(p)
        await db.commit()


async def cleanup_auth() -> None:
    async with get_sessionmaker()() as db:
        now = utcnow()
        await db.execute(delete(AuthSession).where(AuthSession.expires_at < now - timedelta(days=7)))
        await db.execute(delete(AuthToken).where(AuthToken.expires_at < now - timedelta(days=7)))
        await db.commit()


async def process_exports() -> None:
    from app.api.v1.exports import event_stream, user_stream
    from app.schemas.projects import ProjectSettings as PS

    s = get_settings()
    os.makedirs(s.export_dir, exist_ok=True)
    sm = get_sessionmaker()
    async with sm() as db:  # claim one job at a time, atomically
        job = (await db.execute(select(ExportJob).where(ExportJob.status == "pending").order_by(ExportJob.created_at)
                                .limit(1).with_for_update(skip_locked=True))).scalar_one_or_none()
        if job is None:
            expired = (await db.execute(select(ExportJob).where(ExportJob.finished_at < utcnow() - timedelta(hours=s.export_ttl_hours)))).scalars().all()
            for j in expired:
                if j.file_path and os.path.exists(j.file_path):
                    os.remove(j.file_path)
                await db.delete(j)
            await db.commit()
            return
        job.status, job.started_at = "running", utcnow()
        await db.commit()
        jid, pid, kind, fmt, params = job.id, job.project_id, job.kind, job.format, job.params
    path = os.path.join(s.export_dir, f"{jid}.{fmt}")
    try:
        async with sm() as db:
            p = await db.get(Project, pid)
            svc = AnalyticsService(db, pid, PS.model_validate(p.settings or {}))  # type: ignore[union-attr]
            gen = await (event_stream if kind == "events" else user_stream)(svc, params, fmt)
            lines = 0
            with open(path, "wb") as f:
                async for chunk in gen:
                    f.write(chunk)
                    lines += chunk.count(b"\n")
        async with sm() as db:
            await db.execute(update(ExportJob).where(ExportJob.id == jid).values(
                status="done", file_path=path, row_count=max(lines - 1, 0), finished_at=utcnow()))
            await db.commit()
    except Exception as exc:
        logger.exception("export failed")
        async with sm() as db:
            await db.execute(update(ExportJob).where(ExportJob.id == jid).values(status="failed", error=str(exc)[:500], finished_at=utcnow()))
            await db.commit()
