from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analytics.queries import AnalyticsQuery, DateRange
from app.api.deps import current_user, get_project_access
from app.api.v1.analytics import service_for
from app.api.v1.explorer import _range, parse_filters
from app.core.errors import APIError, NotFound
from app.core.permissions import WRITE
from app.db.session import get_session
from app.models import ExportJob, Report, User
from app.services import exporting
from app.services.analytics_service import AnalyticsService

router = APIRouter(prefix="/export", tags=["export"])
jobs_router = APIRouter(tags=["export"])

MEDIA = {"csv": "text/csv; charset=utf-8", "json": "application/json"}


async def _svc(db: AsyncSession, user: User, project_id: UUID) -> AnalyticsService:
    acc = await get_project_access(db, user, project_id, WRITE)  # exporting data needs member+
    return AnalyticsService(db, acc.project.id, acc.settings)


def _response(gen, fmt: str, name: str) -> StreamingResponse:  # type: ignore[no-untyped-def]
    return StreamingResponse(gen, media_type=MEDIA[fmt], headers={"Content-Disposition": f'attachment; filename="{name}.{fmt}"'})


async def event_stream(svc: AnalyticsService, params: dict[str, Any], fmt: str):  # type: ignore[no-untyped-def]
    flt = parse_filters(json.dumps(params.get("filters"))) if params.get("filters") else []
    rng = DateRange.model_validate(params["date_range"]) if params.get("date_range") else DateRange(preset="last_30_days")
    scope = svc.scope(rng, params.get("timezone"), await svc._load_cohorts(flt))
    rows = svc.repo.stream_events(scope, filters=flt, event_names=params.get("events") or None)
    return exporting.to_json(rows) if fmt == "json" else exporting.to_csv(_map(rows, exporting.flatten_event), exporting.EVENT_COLUMNS)


async def user_stream(svc: AnalyticsService, params: dict[str, Any], fmt: str):  # type: ignore[no-untyped-def]
    flt = parse_filters(json.dumps(params.get("filters"))) if params.get("filters") else []
    scope = svc.scope(DateRange(preset="last_7_days"), None, await svc._load_cohorts(flt))
    rows = svc.repo.stream_persons(scope, filters=flt)
    return exporting.to_json(rows) if fmt == "json" else exporting.to_csv(_map(rows, exporting.flatten_user), exporting.USER_COLUMNS)


async def _map(rows, fn):  # type: ignore[no-untyped-def]
    async for r in rows:
        yield fn(r)


@router.get("/events", summary="Stream events as CSV or JSON")
async def export_events(project_id: UUID, format: str = Query("csv", pattern="^(csv|json)$"), event: list[str] | None = Query(None),
                        date_from: str | None = None, date_to: str | None = None, preset: str | None = None,
                        timezone: str | None = None, filters: str | None = None, user: User = Depends(current_user),
                        db: AsyncSession = Depends(get_session)):
    svc = await _svc(db, user, project_id)
    rng = _range(date_from, date_to, preset)
    params = {"events": event, "date_range": rng.model_dump(), "timezone": timezone, "filters": json.loads(filters) if filters else None}
    parse_filters(filters)
    return _response(await event_stream(svc, params, format), format, "events")


@router.get("/users", summary="Stream users as CSV or JSON")
async def export_users(project_id: UUID, format: str = Query("csv", pattern="^(csv|json)$"), filters: str | None = None,
                       user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    svc = await _svc(db, user, project_id)
    parse_filters(filters)
    return _response(await user_stream(svc, {"filters": json.loads(filters) if filters else None}, format), format, "users")


@router.get("/reports", summary="Export saved report definitions")
async def export_reports(project_id: UUID, format: str = Query("json", pattern="^(csv|json)$"), user: User = Depends(current_user),
                         db: AsyncSession = Depends(get_session)):
    await _svc(db, user, project_id)
    rows = (await db.execute(select(Report).where(Report.project_id == project_id).order_by(Report.name))).scalars().all()
    data = [{"id": str(r.id), "name": r.name, "description": r.description, "query": r.query, "visualization": r.visualization,
             "created_at": r.created_at.isoformat(), "updated_at": r.updated_at.isoformat()} for r in rows]

    async def gen():  # type: ignore[no-untyped-def]
        for d in data:
            yield d

    if format == "json":
        return _response(exporting.to_json(gen()), "json", "reports")

    async def flat():  # type: ignore[no-untyped-def]
        for d in data:
            yield {**d, "query": json.dumps(d["query"]), "visualization": json.dumps(d["visualization"])}

    return _response(exporting.to_csv(flat(), ["id", "name", "description", "query", "visualization", "created_at", "updated_at"]), "csv", "reports")


class AnalyticsExport(BaseModel):
    project_id: UUID
    query: AnalyticsQuery
    format: str = Field("csv", pattern="^(csv|json)$")


@router.post("/analytics", summary="Run an analytics query and download the result")
async def export_analytics(body: AnalyticsExport, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    svc = await service_for(db, user, body.project_id)
    env = await svc.run_query(body.query, refresh=True)
    if body.format == "json":
        payload = json.dumps(env.result, default=str).encode()
        return StreamingResponse(iter([payload]), media_type=MEDIA["json"],
                                 headers={"Content-Disposition": f'attachment; filename="{env.query_type}.json"'})
    cols, rows = exporting.table_rows(env.query_type, env.result)

    async def gen():  # type: ignore[no-untyped-def]
        for r in rows:
            yield r

    return _response(exporting.to_csv(gen(), cols), "csv", env.query_type)


# ---- asynchronous export jobs (processed by the worker; for very large datasets) -------------------
class JobIn(BaseModel):
    project_id: UUID
    kind: str = Field(pattern="^(events|users)$")
    format: str = Field("csv", pattern="^(csv|json)$")
    params: dict[str, Any] = Field(default_factory=dict)


class JobOut(BaseModel):
    id: UUID
    project_id: UUID
    kind: str
    format: str
    status: str
    row_count: int | None
    error: str | None
    created_at: datetime
    finished_at: datetime | None


def _job_out(j: ExportJob) -> JobOut:
    return JobOut(id=j.id, project_id=j.project_id, kind=j.kind, format=j.format, status=j.status, row_count=j.row_count,
                  error=j.error, created_at=j.created_at, finished_at=j.finished_at)


@jobs_router.post("/jobs", response_model=JobOut, status_code=202)
async def create_job(body: JobIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    await _svc(db, user, body.project_id)
    if body.params.get("filters"):
        parse_filters(json.dumps(body.params["filters"]))
    job = ExportJob(project_id=body.project_id, kind=body.kind, format=body.format, params=body.params, created_by=user.id)
    db.add(job)
    await db.flush()
    return _job_out(job)


@jobs_router.get("/jobs", response_model=list[JobOut])
async def list_jobs(project_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    await _svc(db, user, project_id)
    rows = (await db.execute(select(ExportJob).where(ExportJob.project_id == project_id).order_by(ExportJob.created_at.desc()).limit(50))).scalars()
    return [_job_out(j) for j in rows]


async def _job(db: AsyncSession, user: User, job_id: UUID) -> ExportJob:
    job = await db.get(ExportJob, job_id)
    if job is None:
        raise NotFound("Export job not found")
    await _svc(db, user, job.project_id)
    return job


@jobs_router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job(job_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    return _job_out(await _job(db, user, job_id))


@jobs_router.get("/jobs/{job_id}/download")
async def download_job(job_id: UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_session)):
    job = await _job(db, user, job_id)
    if job.status != "done" or not job.file_path or not os.path.exists(job.file_path):
        raise APIError("Export is not ready (or has expired)", code="EXPORT_NOT_READY", status_code=409)
    return FileResponse(job.file_path, media_type=MEDIA[job.format], filename=f"{job.kind}.{job.format}")

router.include_router(jobs_router)
