from datetime import datetime
from sqlalchemy import String, Integer, Float, Boolean, DateTime, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db import Base

class Dataset(Base):
    __tablename__ = "datasets"
    id: Mapped[int] = mapped_column(primary_key=True)
    urn: Mapped[str] = mapped_column(String(512), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    domain: Mapped[str] = mapped_column(String(128), default="Clinical Analytics")
    owner: Mapped[str] = mapped_column(String(128), default="Data Platform")
    rows: Mapped[int] = mapped_column(Integer, default=0)
    size_gb: Mapped[float] = mapped_column(Float, default=0)
    date_column: Mapped[str] = mapped_column(String(128), default="event_date")
    pii: Mapped[bool] = mapped_column(Boolean, default=False)
    phi: Mapped[bool] = mapped_column(Boolean, default=False)
    retention_years: Mapped[int] = mapped_column(Integer, default=2)
    legal_hold: Mapped[bool] = mapped_column(Boolean, default=False)
    last_query_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    query_count_90d: Mapped[int] = mapped_column(Integer, default=0)
    downstream_active: Mapped[int] = mapped_column(Integer, default=0)
    business_criticality: Mapped[float] = mapped_column(Float, default=0.5)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)

class ArchiveRun(Base):
    __tablename__ = "archive_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_urn: Mapped[str] = mapped_column(String(512))
    cutoff_date: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(64), default="PLANNED")
    rows_archived: Mapped[int] = mapped_column(Integer, default=0)
    bytes_archived: Mapped[int] = mapped_column(Integer, default=0)
    object_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)
    manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64))
    dataset_urn: Mapped[str] = mapped_column(String(512))
    actor: Mapped[str] = mapped_column(String(128), default="coldlineage-agent")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
