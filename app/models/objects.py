"""Saved analytics objects: dashboards, reports, cohorts, alerts, notifications, export jobs, audit logs."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow_col, uuid_pk


def _project_fk() -> Mapped[uuid.UUID]:
    return mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)


def _user_fk() -> Mapped[uuid.UUID | None]:
    return mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class Dashboard(Base):
    __tablename__ = "dashboards"
    id: Mapped[uuid.UUID] = uuid_pk()
    project_id: Mapped[uuid.UUID] = _project_fk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    created_by: Mapped[uuid.UUID | None] = _user_fk()
    created_at: Mapped[datetime] = utcnow_col()
    updated_at: Mapped[datetime] = utcnow_col()


class Report(Base):
    __tablename__ = "reports"
    id: Mapped[uuid.UUID] = uuid_pk()
    project_id: Mapped[uuid.UUID] = _project_fk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    query: Mapped[dict] = mapped_column(JSONB, nullable=False)
    visualization: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    created_by: Mapped[uuid.UUID | None] = _user_fk()
    created_at: Mapped[datetime] = utcnow_col()
    updated_at: Mapped[datetime] = utcnow_col()


class Cohort(Base):
    __tablename__ = "cohorts"
    id: Mapped[uuid.UUID] = uuid_pk()
    project_id: Mapped[uuid.UUID] = _project_fk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    definition: Mapped[dict] = mapped_column(JSONB, nullable=False)
    last_count: Mapped[int | None] = mapped_column(Integer)
    last_calculated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = _user_fk()
    created_at: Mapped[datetime] = utcnow_col()
    updated_at: Mapped[datetime] = utcnow_col()


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[uuid.UUID] = uuid_pk()
    project_id: Mapped[uuid.UUID] = _project_fk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    query: Mapped[dict] = mapped_column(JSONB, nullable=False)  # trend-style: event, metric, filters
    condition: Mapped[dict] = mapped_column(JSONB, nullable=False)  # {operator, value}
    period: Mapped[str] = mapped_column(String(8), nullable=False, default="day")  # hour | day | week
    webhook_url: Mapped[str | None] = mapped_column(String(2000))
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown", server_default="unknown")
    last_value: Mapped[float | None] = mapped_column()
    last_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = _user_fk()
    created_at: Mapped[datetime] = utcnow_col()
    updated_at: Mapped[datetime] = utcnow_col()


class Notification(Base):
    """Internal notification feed (no email/Slack needed). Read state is shared per project."""

    __tablename__ = "notifications"
    id: Mapped[uuid.UUID] = uuid_pk()
    project_id: Mapped[uuid.UUID] = _project_fk()
    alert_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("alerts.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="alert")
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    created_at: Mapped[datetime] = utcnow_col()
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_notifications_project_created", "project_id", text("created_at DESC")),)


class ExportJob(Base):
    __tablename__ = "export_jobs"
    id: Mapped[uuid.UUID] = uuid_pk()
    project_id: Mapped[uuid.UUID] = _project_fk()
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # events | users
    format: Mapped[str] = mapped_column(String(8), nullable=False)  # csv | json
    params: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")  # pending|running|done|failed
    file_path: Mapped[str | None] = mapped_column(String(500))
    row_count: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = _user_fk()
    created_at: Mapped[datetime] = utcnow_col()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[uuid.UUID] = uuid_pk()
    org_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(40))
    target_id: Mapped[str | None] = mapped_column(String(80))
    meta: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict, server_default="{}")
    ip: Mapped[str | None] = mapped_column(INET)
    created_at: Mapped[datetime] = utcnow_col()
    __table_args__ = (Index("ix_audit_logs_org_created", "org_id", text("created_at DESC")),)
