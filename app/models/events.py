from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import uuid7
from app.db.base import Base, utcnow_col


class Person(Base):
    """A resolved user profile. Many distinct_ids can map to one person (identity merging)."""

    __tablename__ = "persons"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    properties: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    is_identified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = utcnow_col()
    updated_at: Mapped[datetime] = utcnow_col()

    __table_args__ = (
        Index("ix_persons_project_last_seen", "project_id", text("last_seen DESC"), text("id DESC")),
        Index("ix_persons_project_first_seen", "project_id", "first_seen"),
        Index(
            "ix_persons_properties_gin",
            "properties",
            postgresql_using="gin",
            postgresql_ops={"properties": "jsonb_path_ops"},
        ),
    )


class PersonDistinctId(Base):
    __tablename__ = "person_distinct_ids"
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    distinct_id: Mapped[str] = mapped_column(String(400), nullable=False)
    person_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("persons.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = utcnow_col()
    __table_args__ = (PrimaryKeyConstraint("project_id", "distinct_id", name="pk_person_distinct_ids"),)


class Event(Base):
    """Raw event. Identity, properties, request metadata, device and timestamps are separate column groups.

    No foreign keys on the hot path (ingestion speed); integrity is enforced by the ingestion service.
    """

    __tablename__ = "events"
    # identity
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    event: Mapped[str] = mapped_column(String(200), nullable=False)
    insert_id: Mapped[str | None] = mapped_column(String(200))  # client idempotency key
    # user identity
    distinct_id: Mapped[str] = mapped_column(String(400), nullable=False)
    anonymous_id: Mapped[str | None] = mapped_column(String(400))
    person_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # timestamps (UTC)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = utcnow_col()
    # properties
    properties: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    # page (denormalised for fast top-pages queries)
    path: Mapped[str | None] = mapped_column(Text)
    hostname: Mapped[str | None] = mapped_column(String(255))
    # device
    browser: Mapped[str | None] = mapped_column(String(80))
    browser_version: Mapped[str | None] = mapped_column(String(40))
    os: Mapped[str | None] = mapped_column(String(80))
    os_version: Mapped[str | None] = mapped_column(String(40))
    device_type: Mapped[str | None] = mapped_column(String(40))
    country: Mapped[str | None] = mapped_column(String(8))
    # request metadata
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(String(500))

    __table_args__ = (
        Index("ix_events_project_ts", "project_id", text("timestamp DESC"), text("id DESC")),
        Index("ix_events_project_event_ts", "project_id", "event", "timestamp"),
        Index("ix_events_project_person_ts", "project_id", "person_id", "timestamp"),
        Index("ix_events_project_distinct", "project_id", "distinct_id"),
        Index(
            "uq_events_project_insert_id",
            "project_id",
            "insert_id",
            unique=True,
            postgresql_where=text("insert_id IS NOT NULL"),
        ),
        Index(
            "ix_events_properties_gin",
            "properties",
            postgresql_using="gin",
            postgresql_ops={"properties": "jsonb_path_ops"},
        ),
    )


class EventDefinition(Base):
    __tablename__ = "event_definitions"
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (PrimaryKeyConstraint("project_id", "name", name="pk_event_definitions"),)


class PropertyDefinition(Base):
    __tablename__ = "property_definitions"
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # event | person
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    value_type: Mapped[str] = mapped_column(String(16), nullable=False)  # string|number|boolean|array|object|null
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (PrimaryKeyConstraint("project_id", "kind", "name", name="pk_property_definitions"),)


class ApiUsage(Base):
    """Hourly ingestion counters per project."""

    __tablename__ = "api_usage"
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    bucket: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    requests: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    events_accepted: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    events_rejected: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    events_duplicate: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    rate_limited: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    __table_args__ = (PrimaryKeyConstraint("project_id", "bucket", name="pk_api_usage"),)
