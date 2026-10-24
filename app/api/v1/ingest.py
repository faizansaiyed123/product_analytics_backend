"""Public event ingestion (authenticated by project API key, not by user session)."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import client_ip
from app.core.config import get_settings
from app.core.errors import APIError
from app.core.timeutil import utcnow
from app.db.session import get_session
from app.schemas.events import BatchPayload, EventPayload, IngestResponse, RejectedEvent
from app.services import ingestion, usage
from app.services.ingest_auth import authenticate_ingest
from app.services.ingestion import IngestResult, RequestMeta

router = APIRouter(prefix="/events", tags=["ingestion"])

_EXAMPLE_SINGLE = {
    "event": "checkout_completed",
    "distinct_id": "user_123",
    "properties": {"amount": 99.99, "plan": "pro"},
}
_EXAMPLE_BATCH = {
    "events": [
        {"event": "page_view", "distinct_id": "user_123", "properties": {"path": "/pricing"}},
        {"event": "button_clicked", "distinct_id": "user_123", "properties": {"button": "signup"}},
    ]
}


def _meta(request: Request) -> RequestMeta:
    return RequestMeta(
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        country_header=request.headers.get(get_settings().country_header),
        received_at=utcnow(),
    )


async def _json_body(request: Request) -> Any:
    """Parse the body as JSON regardless of Content-Type (navigator.sendBeacon sends text/plain)."""
    raw = await request.body()
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise APIError("Request body must be valid JSON", code="INVALID_JSON", status_code=400) from None


def _parse_sent_at(value: Any):  # type: ignore[no-untyped-def]
    if not value:
        return None
    try:
        return ingestion._parse_timestamp(value)
    except ingestion.InvalidEvent:
        return None


def _response(result: IngestResult, response: Response, request: Request, *, all_or_nothing: bool) -> IngestResponse:
    for k, v in getattr(request.state, "rate_headers", {}).items():
        response.headers[k] = v
    body = IngestResponse(
        accepted=result.accepted,
        duplicates=result.duplicates,
        rejected=[RejectedEvent(index=r.index, code=r.code, message=r.message) for r in result.rejected],
    )
    if result.rejected and result.accepted == 0 and result.duplicates == 0:
        first = result.rejected[0]
        raise APIError(
            first.message, code=first.code, status_code=400, details=[r.__dict__ for r in result.rejected][:50]
        )
    return body


@router.post(
    "",
    response_model=IngestResponse,
    summary="Ingest a single event",
    openapi_extra={
        "requestBody": {
            "content": {"application/json": {"schema": EventPayload.model_json_schema(), "example": _EXAMPLE_SINGLE}}
        }
    },
)
async def ingest_single(request: Request, response: Response, db: AsyncSession = Depends(get_session)):
    body = await _json_body(request)
    if not isinstance(body, dict):
        raise APIError("Request body must be a JSON object", code="INVALID_EVENT", status_code=400)
    project = await authenticate_ingest(request, db, body.get("api_key") if isinstance(body, dict) else None)
    meta = _meta(request)
    sent_at = _parse_sent_at(body.get("sent_at"))
    result = await ingestion.ingest_events(db, project.project_id, project.settings, [body], meta, sent_at)
    await usage.record(
        project.project_id,
        requests=1,
        events_accepted=result.accepted,
        events_rejected=len(result.rejected),
        events_duplicate=result.duplicates,
    )
    return _response(result, response, request, all_or_nothing=True)


@router.post(
    "/batch",
    response_model=IngestResponse,
    summary="Ingest a batch of events",
    openapi_extra={
        "requestBody": {
            "content": {"application/json": {"schema": BatchPayload.model_json_schema(), "example": _EXAMPLE_BATCH}}
        }
    },
)
async def ingest_batch(request: Request, response: Response, db: AsyncSession = Depends(get_session)):
    body = await _json_body(request)
    if not isinstance(body, dict):
        raise APIError(
            "Request body must be a JSON object with an 'events' array", code="INVALID_BATCH", status_code=400
        )
    events = body.get("events")
    if not isinstance(events, list):
        raise APIError("'events' must be an array", code="INVALID_BATCH", status_code=400)
    if not events:
        raise APIError("'events' must not be empty", code="EMPTY_BATCH", status_code=400)
    limit = get_settings().max_batch_size
    if len(events) > limit:
        raise APIError(f"A batch may contain at most {limit} events", code="BATCH_TOO_LARGE", status_code=413)
    project = await authenticate_ingest(request, db, body.get("api_key"))
    meta = _meta(request)
    result = await ingestion.ingest_events(
        db, project.project_id, project.settings, events, meta, _parse_sent_at(body.get("sent_at"))
    )
    await usage.record(
        project.project_id,
        requests=1,
        events_accepted=result.accepted,
        events_rejected=len(result.rejected),
        events_duplicate=result.duplicates,
    )
    return _response(result, response, request, all_or_nothing=False)
