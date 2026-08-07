"""Local persistence.

Note what is NOT here: dataset metadata. There is no `owner`, no `retention_years`, no
`downstream_active`, no `metadata_json` blob of hand-typed dependencies. All of that now
comes from DataHub at request time. The previous schema carried those columns, which
meant the product was reading its own seed script and calling it catalog context.

What remains is the state DataHub does not own: the registry of datasets we have
discovered, the plans we have issued, the archive runs we have executed, and the audit
trail. The archive runs in particular are the durable record of where bytes went.
"""

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class DatasetRegistry(Base):
    """Stable numeric ids for URNs discovered in DataHub.

    Exists only so the HTTP API can expose /api/datasets/{id} instead of forcing URN
    escaping through the URL. It holds no metadata of its own.
    """

    __tablename__ = "dataset_registry"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    urn: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ArchivePlanRecord(Base):
    """An issued plan, addressed by its hash.

    Execution requires a plan hash, and the hash binds the dataset, the cutoff, the row
    count and the verdict together. A plan cannot be approved and then executed against
    different data: if anything material changed since the plan was issued, the recomputed
    hash no longer matches and execution is refused.
    """

    __tablename__ = "archive_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    dataset_urn: Mapped[str] = mapped_column(String(512), index=True)
    cutoff_date: Mapped[Date] = mapped_column(Date)
    rows_in_scope: Mapped[int] = mapped_column(BigInteger)
    bytes_in_scope: Mapped[int] = mapped_column(BigInteger, default=0)
    recommendation: Mapped[str] = mapped_column(String(32))
    monthly_savings_usd: Mapped[float] = mapped_column(Float, default=0.0)
    verdict: Mapped[dict] = mapped_column(JSON, default=dict)
    blockers: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(24), default="PLANNED")  # PLANNED|APPROVED|EXECUTED|REJECTED
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ArchiveRun(Base):
    __tablename__ = "archive_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_hash: Mapped[str] = mapped_column(String(64), index=True)
    dataset_urn: Mapped[str] = mapped_column(String(512), index=True)
    dataset_name: Mapped[str] = mapped_column(String(256))
    cutoff_date: Mapped[Date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(32))  # VERIFIED | VERIFICATION_FAILED | RESTORED
    rows_archived: Mapped[int] = mapped_column(BigInteger, default=0)
    bytes_archived: Mapped[int] = mapped_column(BigInteger, default=0)
    object_uri: Mapped[str] = mapped_column(String(1024))
    manifest_uri: Mapped[str] = mapped_column(String(1024), default="")
    checksum: Mapped[str] = mapped_column(String(128))
    verified_readback: Mapped[bool] = mapped_column(Boolean, default=False)
    manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    verification: Mapped[dict] = mapped_column(JSON, default=dict)
    datahub_writeback: Mapped[dict] = mapped_column(JSON, default=dict)
    approved_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    restored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (UniqueConstraint("id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(48), index=True)
    dataset_urn: Mapped[str] = mapped_column(String(512), index=True)
    actor: Mapped[str] = mapped_column(String(128), default="coldlineage")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
