from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class EventPayload(BaseModel):
    """Documentation model for a single event. Validation is done per event by the ingestion service."""

    event: str = Field(examples=["checkout_completed"])
    distinct_id: str | None = Field(None, examples=["user_123"])
    anonymous_id: str | None = Field(None, examples=["anon_456"])
    timestamp: str | float | None = Field(None, examples=["2026-09-29T12:30:00Z"])
    uuid: str | None = Field(None, description="Idempotency key: an event with the same uuid is stored once")
    properties: dict[str, Any] = Field(default_factory=dict, examples=[{"amount": 99.99, "plan": "pro"}])
    context: dict[str, Any] = Field(default_factory=dict, examples=[{"user_agent": "...", "country": "IN"}])


class BatchPayload(BaseModel):
    events: list[dict[str, Any]] = Field(description="Up to MAX_BATCH_SIZE events; each is validated independently")
    api_key: str | None = Field(None, description="Alternative to the Authorization header (e.g. for sendBeacon)")
    sent_at: str | None = Field(None, description="Client clock at send time; used to correct timestamp skew")


class RejectedEvent(BaseModel):
    index: int
    code: str
    message: str


class IngestResponse(BaseModel):
    status: str = "ok"
    accepted: int
    duplicates: int = 0
    rejected: list[RejectedEvent] = Field(default_factory=list)
