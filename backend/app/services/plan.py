"""Plan construction and the approval gate.

A plan is a signed statement of intent: *this dataset, this cutoff, this many rows, this
verdict*. The signature is a SHA-256 over exactly those facts.

Execution takes a plan hash rather than a dataset and a date. The service recomputes the
plan from live state and compares hashes, so if anything material has drifted since the
operator looked at the screen -- rows arrived, a consumer's query widened, a legal hold
landed, lineage changed -- the hash no longer matches and execution is refused rather
than silently proceeding against different data.

That closes the gap in the previous version, where /execute accepted a cutoff and an
`approved_by` string that defaulted to "hackathon-judge". An approval that anyone can
forge by POSTing a default value is not an approval gate; it is a comment.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime

from app.domain.models import (
    ArchivePlan,
    Blocker,
    DatasetAssessment,
    RangeVerdict,
    Recommendation,
)
from app.services.evidence import EvidenceService


def compute_plan_hash(
    dataset_urn: str,
    cutoff: date,
    rows_in_scope: int,
    recommendation: str,
    blocker_codes: list[str],
) -> str:
    payload = json.dumps(
        {
            "dataset_urn": dataset_urn,
            "cutoff_date": cutoff.isoformat(),
            "rows_in_scope": rows_in_scope,
            "recommendation": recommendation,
            "blockers": sorted(blocker_codes),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class PlanService:
    @staticmethod
    def cutoff_blockers(
        assessment: DatasetAssessment, cutoff: date, now: date | None = None
    ) -> list[Blocker]:
        """Blockers that depend on the proposed cutoff, evaluated on top of the
        cutoff-independent ones already in the assessment."""
        now = now or date.today()
        blockers: list[Blocker] = []
        context = assessment.context

        floor = EvidenceService.retention_floor(context, now)
        if floor is not None and cutoff > floor:
            overshoot = (cutoff - floor).days
            blockers.append(
                Blocker(
                    code="RETENTION_FLOOR",
                    message=(
                        f"Retention policy requires {context.retention_years:g} years of history "
                        f"to stay hot (on or after {floor.isoformat()}). The proposed cutoff of "
                        f"{cutoff.isoformat()} breaches it by {overshoot} days. Move the cutoff "
                        f"to {floor.isoformat()} or earlier."
                    ),
                    provenance=context.policy_provenance,
                )
            )

        if context.date_column:
            # A cutoff outside the data's own span is a no-op, not an error, but say so.
            pass

        return blockers

    @staticmethod
    def build(
        assessment: DatasetAssessment,
        verdict: RangeVerdict,
        cutoff: date,
        measurement: dict,
        now: datetime | None = None,
    ) -> ArchivePlan:
        now = now or datetime.now(UTC)
        blockers = list(assessment.blockers) + PlanService.cutoff_blockers(
            assessment, cutoff, now.date()
        )

        rows = int(measurement.get("rows", 0))
        plan_hash = compute_plan_hash(
            assessment.context.urn,
            cutoff,
            rows,
            verdict.recommendation.value,
            [b.code for b in blockers],
        )

        return ArchivePlan(
            plan_hash=plan_hash,
            dataset_urn=assessment.context.urn,
            cutoff_date=cutoff,
            rows_in_scope=rows,
            bytes_in_scope=int(measurement.get("estimated_source_bytes", 0)),
            verdict=verdict,
            blockers=blockers,
            monthly_savings_usd=float(measurement.get("monthly_savings_usd", 0.0)),
            requires_approval=True,
            created_at=now,
        )

    @staticmethod
    def is_executable(plan: ArchivePlan) -> tuple[bool, str]:
        if plan.blockers:
            codes = ", ".join(b.code for b in plan.blockers)
            return False, f"blocked by: {codes}"
        if plan.verdict.recommendation == Recommendation.DO_NOT_ARCHIVE:
            return False, plan.verdict.rationale
        if plan.rows_in_scope <= 0:
            return False, f"no rows older than {plan.cutoff_date.isoformat()}"
        return True, "executable"
