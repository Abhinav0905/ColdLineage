"""HTTP surface.

This is also the executor's tool surface: the DataHub Skill in skills/ drives exactly
these endpoints. There is no other way in. The agent plans and explains; the executor
measures, verifies and moves bytes; a human stands between them at /execute.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.datahub.client import DataHubClient, DataHubError
from app.domain.models import ArchiveManifest, DatasetAssessment, Recommendation
from app.models.db import ArchivePlanRecord, ArchiveRun, AuditEvent, DatasetRegistry
from app.services.archive import ArchiveError, ArchiveService, VerificationFailed
from app.services.context import ContextService
from app.services.evidence import EvidenceService
from app.services.plan import PlanService, compute_plan_hash
from app.services.simulation import SimulationService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

VERSION = "2.0.0"

_archive = ArchiveService()


def _client() -> DataHubClient:
    return DataHubClient()


def _context_service() -> ContextService:
    return ContextService(_client())


# --------------------------------------------------------------------------
# Request bodies
# --------------------------------------------------------------------------


class CutoffBody(BaseModel):
    cutoff_date: date


class ExecuteBody(BaseModel):
    plan_hash: str = Field(min_length=64, max_length=64)
    approved_by: str = Field(min_length=1, max_length=128)


class RestoreBody(BaseModel):
    run_id: int
    temporary: bool = True


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _audit(db: Session, event_type: str, urn: str, detail: dict, actor: str = "coldlineage", note: str = "") -> None:
    db.add(AuditEvent(event_type=event_type, dataset_urn=urn, actor=actor, detail=detail, note=note))
    db.commit()


def _register(db: Session, urns: list[str]) -> dict[str, int]:
    """Give each discovered URN a stable numeric id."""
    now = datetime.now(UTC)
    existing = {r.urn: r for r in db.scalars(select(DatasetRegistry)).all()}
    for urn in urns:
        row = existing.get(urn)
        if row is None:
            row = DatasetRegistry(urn=urn, first_seen_at=now, last_seen_at=now)
            db.add(row)
            existing[urn] = row
        else:
            row.last_seen_at = now
    db.commit()
    return {r.urn: r.id for r in db.scalars(select(DatasetRegistry)).all()}


def _archive_state(db: Session, urn: str) -> tuple[str, str | None]:
    run = db.scalars(
        select(ArchiveRun)
        .where(ArchiveRun.dataset_urn == urn, ArchiveRun.status == "VERIFIED")
        .order_by(ArchiveRun.id.desc())
    ).first()
    if run is None:
        return "HOT", None
    if run.restored_at is not None:
        return "HOT", None
    return "PARTIALLY_ARCHIVED", run.cutoff_date.isoformat()


def _summary(assessment: DatasetAssessment, dataset_id: int, state: str, archived_through: str | None) -> dict:
    ctx = assessment.context
    span_min = span_max = None
    # min/max come from the warehouse read in ContextService via physical facts; the
    # context object carries them indirectly through row/size only, so recompute lazily
    # is unnecessary -- the detail endpoint exposes the full context.
    return {
        "id": dataset_id,
        "urn": ctx.urn,
        "name": ctx.name,
        "platform": ctx.platform,
        "domain": ctx.domain,
        "owners": ctx.owners,
        "tags": ctx.tags,
        "sensitive": ctx.sensitive,
        "row_count": ctx.row_count,
        "size_bytes": ctx.size_bytes,
        "date_column": ctx.date_column,
        "min_date": span_min,
        "max_date": span_max,
        "temperature": assessment.temperature.model_dump(mode="json"),
        "archive_eligible": assessment.archive_eligible,
        "blockers": [b.model_dump(mode="json") for b in assessment.blockers],
        "downstream_count": len(ctx.downstream),
        "archive_state": state,
        "archived_through": archived_through,
        "signals_live": ctx.policy_provenance.source.value.startswith("datahub"),
    }


async def _assess(db: Session, urn: str) -> DatasetAssessment:
    service = _context_service()
    context = await service.build(db, urn)
    return EvidenceService.build(context)


def _resolve_urn(db: Session, dataset_id: int) -> str:
    row = db.get(DatasetRegistry, dataset_id)
    if row is None:
        raise HTTPException(404, f"dataset id {dataset_id} is not registered; GET /api/datasets first")
    return row.urn


# --------------------------------------------------------------------------
# Health and discovery
# --------------------------------------------------------------------------


@router.get("/health")
async def health():
    dh = await _client().health()
    return {"ok": True, "service": settings.app_name, "version": VERSION, "datahub": dh.as_dict()}


@router.get("/datasets")
async def datasets(db: Session = Depends(get_db)):
    """Discover the estate from DataHub, then assess each dataset.

    The list of datasets is whatever DataHub knows about -- there is no local catalog to
    fall out of sync with. If DataHub is unreachable this returns 503 rather than a
    plausible-looking empty estate.
    """
    client = _client()
    try:
        found = await client.search_datasets(query="*", count=100)
    except DataHubError as exc:
        raise HTTPException(
            503,
            {
                "message": "DataHub is unreachable, so the estate cannot be enumerated.",
                "detail": str(exc),
                "gms_url": settings.datahub_gms_url,
                "hint": "Start DataHub (datahub docker quickstart) or set DATAHUB_MODE=replay.",
            },
        ) from exc

    # Everything DataHub knows is context; only what we have an executor for is a
    # candidate. The rest still show up as downstream consumers in the verdict.
    platforms = settings.executable_platform_list
    urns = [
        f["urn"]
        for f in found
        if f.get("urn") and any(f"dataPlatform:{p}," in f["urn"] for p in platforms)
    ]
    ids = _register(db, urns)

    out = []
    for urn in urns:
        try:
            assessment = await _assess(db, urn)
        except Exception as exc:  # noqa: BLE001 - one bad dataset must not blank the estate
            logger.warning("assessment failed for %s: %s", urn, exc)
            continue
        state, through = _archive_state(db, urn)
        out.append(_summary(assessment, ids[urn], state, through))

    out.sort(key=lambda d: d["temperature"]["score"])
    return out


@router.get("/datasets/{dataset_id}")
async def dataset_detail(dataset_id: int, db: Session = Depends(get_db)):
    urn = _resolve_urn(db, dataset_id)
    assessment = await _assess(db, urn)
    state, through = _archive_state(db, urn)
    payload = _summary(assessment, dataset_id, state, through)

    ctx = assessment.context
    span = _date_span(db, ctx)
    payload["min_date"] = span[0]
    payload["max_date"] = span[1]
    payload["context"] = ctx.model_dump(mode="json")
    payload["evidence"] = [e.model_dump(mode="json") for e in assessment.evidence]
    payload["confidence"] = assessment.confidence
    payload["datahub_url"] = _client().entity_url(urn)
    return payload


def _date_span(db: Session, ctx) -> tuple[str | None, str | None]:
    if not ctx.date_column:
        return None, None
    from sqlalchemy import text

    try:
        row = db.execute(
            text(f'SELECT min("{ctx.date_column}"), max("{ctx.date_column}") FROM {ctx.qualified_table}')
        ).one()
        return (
            row[0].isoformat() if row[0] else None,
            row[1].isoformat() if row[1] else None,
        )
    except Exception:  # noqa: BLE001
        db.rollback()  # Postgres aborts the transaction on failure; clear it for the next query.
        return None, None


# --------------------------------------------------------------------------
# Simulate and plan
# --------------------------------------------------------------------------


@router.post("/datasets/{dataset_id}/simulate")
async def simulate(dataset_id: int, body: CutoffBody, db: Session = Depends(get_db)):
    urn = _resolve_urn(db, dataset_id)
    service = _context_service()
    context = await service.build(db, urn)
    lineage_ok = await service.lineage_complete(urn)

    verdict = SimulationService.simulate(
        context.downstream, body.cutoff_date, lineage_complete=lineage_ok
    )
    _audit(
        db,
        "SIMULATION",
        urn,
        {"cutoff_date": body.cutoff_date.isoformat(), "recommendation": verdict.recommendation.value},
    )
    return verdict.model_dump(mode="json")


@router.post("/datasets/{dataset_id}/plan")
async def plan(dataset_id: int, body: CutoffBody, db: Session = Depends(get_db)):
    urn = _resolve_urn(db, dataset_id)
    service = _context_service()
    context = await service.build(db, urn)
    assessment = EvidenceService.build(context)
    lineage_ok = await service.lineage_complete(urn)
    verdict = SimulationService.simulate(
        context.downstream, body.cutoff_date, lineage_complete=lineage_ok
    )

    try:
        measurement = _archive.measure(db, context, body.cutoff_date)
    except ArchiveError as exc:
        raise HTTPException(409, {"message": str(exc)}) from exc

    built = PlanService.build(assessment, verdict, body.cutoff_date, measurement)

    record = db.scalar(select(ArchivePlanRecord).where(ArchivePlanRecord.plan_hash == built.plan_hash))
    if record is None:
        record = ArchivePlanRecord(
            plan_hash=built.plan_hash,
            dataset_urn=urn,
            cutoff_date=built.cutoff_date,
            rows_in_scope=built.rows_in_scope,
            bytes_in_scope=built.bytes_in_scope,
            recommendation=verdict.recommendation.value,
            monthly_savings_usd=built.monthly_savings_usd,
            verdict=verdict.model_dump(mode="json"),
            blockers=[b.model_dump(mode="json") for b in built.blockers],
            status="PLANNED",
        )
        db.add(record)
        db.commit()

    _audit(
        db,
        "PLAN_ISSUED",
        urn,
        {
            "plan_hash": built.plan_hash,
            "cutoff_date": built.cutoff_date.isoformat(),
            "rows_in_scope": built.rows_in_scope,
            "recommendation": verdict.recommendation.value,
            "blockers": [b.code for b in built.blockers],
        },
    )

    payload = built.model_dump(mode="json")
    payload["measurement"] = measurement
    payload["executable"], payload["executable_reason"] = PlanService.is_executable(built)
    return payload


# --------------------------------------------------------------------------
# Execute -- the only destructive path
# --------------------------------------------------------------------------


@router.post("/execute")
async def execute(body: ExecuteBody, db: Session = Depends(get_db)):
    record = db.scalar(select(ArchivePlanRecord).where(ArchivePlanRecord.plan_hash == body.plan_hash))
    if record is None:
        raise HTTPException(404, {"message": "unknown plan hash; issue a plan first via POST /api/datasets/{id}/plan"})
    if record.status == "EXECUTED":
        raise HTTPException(409, {"message": f"plan {body.plan_hash[:12]} has already been executed"})

    urn = record.dataset_urn
    cutoff = record.cutoff_date

    # Recompute from live state and require the hash to still match. If the estate moved
    # under us since the plan was shown, refuse rather than execute a stale intent.
    service = _context_service()
    context = await service.build(db, urn)
    assessment = EvidenceService.build(context)
    lineage_ok = await service.lineage_complete(urn)
    verdict = SimulationService.simulate(context.downstream, cutoff, lineage_complete=lineage_ok)

    try:
        measurement = _archive.measure(db, context, cutoff)
    except ArchiveError as exc:
        raise HTTPException(409, {"message": str(exc)}) from exc

    rebuilt = PlanService.build(assessment, verdict, cutoff, measurement)
    if rebuilt.plan_hash != body.plan_hash:
        raise HTTPException(
            409,
            {
                "message": (
                    "Plan is stale: the dataset, its consumers or its policy changed since this "
                    "plan was issued. Re-plan and review before approving."
                ),
                "approved_plan": body.plan_hash,
                "current_plan": rebuilt.plan_hash,
                "current_recommendation": verdict.recommendation.value,
                "current_blockers": [b.code for b in rebuilt.blockers],
            },
        )

    ok, reason = PlanService.is_executable(rebuilt)
    if not ok:
        _audit(db, "EXECUTION_REFUSED", urn, {"plan_hash": body.plan_hash, "reason": reason})
        raise HTTPException(409, {"message": f"refusing to execute: {reason}", "verdict": verdict.model_dump(mode="json")})

    record.status = "APPROVED"
    record.approved_by = body.approved_by
    record.approved_at = datetime.now(UTC)
    db.commit()
    _audit(db, "APPROVAL_RECORDED", urn, {"plan_hash": body.plan_hash}, actor=body.approved_by)

    run_key = body.plan_hash[:12]
    try:
        manifest, verification = _archive.execute(db, context, cutoff, run_key)
    except VerificationFailed as exc:
        db.rollback()
        _audit(db, "VERIFICATION_FAILED", urn, {"plan_hash": body.plan_hash, "error": str(exc)})
        raise HTTPException(500, {"message": str(exc), "source_intact": True}) from exc
    except ArchiveError as exc:
        db.rollback()
        _audit(db, "EXECUTION_FAILED", urn, {"plan_hash": body.plan_hash, "error": str(exc)})
        raise HTTPException(500, {"message": str(exc), "source_intact": True}) from exc

    run = ArchiveRun(
        plan_hash=body.plan_hash,
        dataset_urn=urn,
        dataset_name=context.name,
        cutoff_date=cutoff,
        status="VERIFIED",
        rows_archived=manifest.rows,
        bytes_archived=manifest.bytes,
        object_uri=manifest.object_uri,
        manifest_uri=manifest.manifest_uri,
        checksum=manifest.sha256,
        verified_readback=verification.passed,
        manifest=manifest.model_dump(mode="json"),
        verification=verification.model_dump(mode="json"),
        approved_by=body.approved_by,
        completed_at=datetime.now(UTC),
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    writeback = await _client().write_archive_provenance(
        urn,
        archive_state="PARTIALLY_ARCHIVED",
        archived_through=cutoff.isoformat(),
        object_uri=manifest.object_uri,
        sha256=manifest.sha256,
        restore_sla="on-demand, minutes",
        run_id=str(run.id),
        manifest_uri=manifest.manifest_uri,
        rows=manifest.rows,
    )
    run.datahub_writeback = writeback.as_dict()
    record.status = "EXECUTED"
    db.commit()

    _audit(
        db,
        "ARCHIVE_EXECUTED",
        urn,
        {
            "run_id": run.id,
            "plan_hash": body.plan_hash,
            "rows": manifest.rows,
            "bytes": manifest.bytes,
            "object_uri": manifest.object_uri,
            "sha256": manifest.sha256,
            "verification": verification.model_dump(mode="json"),
            "datahub": writeback.as_dict(),
        },
        actor=body.approved_by,
    )

    return {
        "run_id": run.id,
        "manifest": manifest.model_dump(mode="json"),
        "verification": verification.model_dump(mode="json"),
        "datahub_writeback": writeback.as_dict(),
    }


# --------------------------------------------------------------------------
# Restore
# --------------------------------------------------------------------------


@router.post("/restore")
async def restore(body: RestoreBody, db: Session = Depends(get_db)):
    run = db.get(ArchiveRun, body.run_id)
    if run is None:
        raise HTTPException(404, {"message": f"archive run {body.run_id} not found"})

    service = _context_service()
    context = await service.build(db, run.dataset_urn)
    manifest = ArchiveManifest.model_validate(run.manifest)

    try:
        result = _archive.restore(db, context, manifest, temporary=body.temporary)
    except VerificationFailed as exc:
        _audit(db, "RESTORE_REFUSED", run.dataset_urn, {"run_id": run.id, "error": str(exc)})
        raise HTTPException(409, {"message": str(exc)}) from exc
    except ArchiveError as exc:
        raise HTTPException(500, {"message": str(exc)}) from exc

    if not body.temporary:
        run.restored_at = datetime.now(UTC)
        run.status = "RESTORED"
        db.commit()

    _audit(db, "RESTORE_COMPLETED", run.dataset_urn, {"run_id": run.id, **result})
    return result


# --------------------------------------------------------------------------
# Trail
# --------------------------------------------------------------------------


@router.get("/runs")
def runs(db: Session = Depends(get_db)):
    return [
        {
            "id": r.id,
            "dataset_urn": r.dataset_urn,
            "dataset_name": r.dataset_name,
            "cutoff_date": r.cutoff_date.isoformat(),
            "status": r.status,
            "rows_archived": r.rows_archived,
            "bytes_archived": r.bytes_archived,
            "object_uri": r.object_uri,
            "manifest_uri": r.manifest_uri,
            "checksum": r.checksum,
            "verified_readback": r.verified_readback,
            "approved_by": r.approved_by,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "restored_at": r.restored_at.isoformat() if r.restored_at else None,
            "datahub_writeback": r.datahub_writeback,
        }
        for r in db.scalars(select(ArchiveRun).order_by(ArchiveRun.id.desc())).all()
    ]


@router.get("/audit")
def audit_events(db: Session = Depends(get_db)):
    return [
        {
            "id": a.id,
            "event_type": a.event_type,
            "dataset_urn": a.dataset_urn,
            "actor": a.actor,
            "detail": a.detail,
            "note": a.note,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in db.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(200)).all()
    ]
