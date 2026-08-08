"""Split a table's rows into the three states that decide its fate.

This is the estate view's whole argument in one computation. Every row of every
table sits in exactly one of three states, and the reason it is there differs:

    ARCHIVABLE    older than every consumer's reach AND past the retention floor
    POLICY_HELD   provably unread by anyone -- but inside the retention window
    IN_USE        a consumer can still reach it

The distinction that matters: **only the middle band responds to configuration.**
Lower `io.coldlineage.policy.retentionYears` and POLICY_HELD shrinks into
ARCHIVABLE. Lower it to nothing and IN_USE does not move by a single row, because
it is fixed by what consumers' SQL actually reads. Configuration is a floor, not a
permission slip, and a picture of these three bands is the fastest way to see it.

Fail-closed, exactly as everywhere else: anything unproven lands in IN_USE, never
in ARCHIVABLE. A single unbounded consumer, an active legal hold, a missing date
column or an unmeasurable table all put the entire table in IN_USE. Absence of
evidence is not evidence of safety, and that rule does not get relaxed because
the output happens to be a chart.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.domain.models import DatasetContext, Provenance, Source
from app.services.evidence import EvidenceService

logger = logging.getLogger(__name__)


@dataclass
class RowBands:
    """Three disjoint row counts that sum to the measured total."""

    archivable: int
    policy_held: int
    in_use: int

    # The two dates that draw the boundaries, so the UI can label them rather
    # than making the viewer infer where the lines came from.
    evidence_bound: date | None
    policy_floor: date | None
    cutoff: date | None
    binding: str  # "evidence" | "policy" | "legal_hold" | "unbounded" | "unmeasured"
    reason: str
    provenance: Provenance

    @property
    def total(self) -> int:
        return self.archivable + self.policy_held + self.in_use

    def as_dict(self) -> dict:
        return {
            "archivable": self.archivable,
            "policy_held": self.policy_held,
            "in_use": self.in_use,
            "total": self.total,
            "evidence_bound": self.evidence_bound.isoformat() if self.evidence_bound else None,
            "policy_floor": self.policy_floor.isoformat() if self.policy_floor else None,
            "cutoff": self.cutoff.isoformat() if self.cutoff else None,
            "binding": self.binding,
            "reason": self.reason,
            "provenance": self.provenance.model_dump(mode="json"),
        }


def _all_in_use(total: int, reason: str, binding: str, provenance: Provenance,
                evidence_bound: date | None = None,
                policy_floor: date | None = None) -> RowBands:
    return RowBands(
        archivable=0,
        policy_held=0,
        in_use=total,
        evidence_bound=evidence_bound,
        policy_floor=policy_floor,
        cutoff=None,
        binding=binding,
        reason=reason,
        provenance=provenance,
    )


def evidence_bound(context: DatasetContext) -> tuple[date | None, str | None]:
    """The earliest date any downstream consumer can still reach.

    Returns (bound, blocking_reason). A bound of None with a reason means the
    table cannot be split at all -- some consumer reads without a date bound, so
    no row is provably unread.
    """
    unbounded = [w for w in context.downstream if w.is_unbounded]
    if unbounded:
        names = ", ".join(w.consumer_name for w in unbounded[:3])
        more = f" and {len(unbounded) - 3} more" if len(unbounded) > 3 else ""
        return None, (
            f"{len(unbounded)} consumer(s) read this table with no date bound "
            f"({names}{more}), so no row can be shown to be unread."
        )

    bounded = [w for w in context.downstream if w.earliest_date_read]
    if not bounded:
        # No consumers at all. Nothing reads it, so nothing constrains the range;
        # policy alone decides. (Usage-based coldness is scored elsewhere.)
        return None, None

    return min(w.earliest_date_read for w in bounded if w.earliest_date_read), None


def compute(
    db: Session,
    context: DatasetContext,
    *,
    now: date | None = None,
    executable: bool = True,
) -> RowBands:
    """Measure the three bands. Counts are counted, never apportioned."""
    now = now or date.today()
    total = context.row_count or 0
    warehouse = Provenance(
        source=Source.WAREHOUSE,
        detail=f"counted in the warehouse against {context.date_column}",
        observed_at=None,
    )
    policy = context.policy_provenance

    floor = EvidenceService.retention_floor(context, now)

    # -- the fail-closed gates, cheapest first ----------------------------
    if context.legal_hold:
        matter = context.legal_hold_matter or "unspecified matter"
        return _all_in_use(
            total, f"An active legal hold ({matter}) forbids moving any row.", "legal_hold",
            policy, policy_floor=floor,
        )

    if not context.date_column:
        return _all_in_use(
            total,
            "No date column is declared for this dataset, so a date range cannot be scoped.",
            "unmeasured", context.date_column_provenance, policy_floor=floor,
        )

    if not total or not executable:
        return _all_in_use(
            total,
            "This dataset is not measurable from the warehouse this service can reach."
            if not executable
            else "No rows measured.",
            "unmeasured", warehouse, policy_floor=floor,
        )

    bound, blocked = evidence_bound(context)
    if blocked:
        return _all_in_use(total, blocked, "unbounded", context.usage_provenance,
                           policy_floor=floor)

    # -- boundaries --------------------------------------------------------
    # cutoff = the latest date it is safe AND permitted to archive before.
    candidates = [d for d in (bound, floor) if d is not None]
    cutoff = min(candidates) if candidates else None
    if cutoff is None:
        return _all_in_use(
            total,
            "Neither a consumer bound nor a retention floor is known, so nothing is proven safe.",
            "unmeasured", policy, policy_floor=floor,
        )

    binding = "evidence" if (bound is not None and cutoff == bound) else "policy"

    # -- count, in one pass ------------------------------------------------
    column, table = context.date_column, context.qualified_table
    try:
        row = db.execute(
            text(
                f'SELECT count(*) FILTER (WHERE "{column}" < :cutoff) AS archivable, '
                f'count(*) FILTER (WHERE "{column}" >= :cutoff'
                + (f' AND "{column}" < :bound' if bound is not None else " AND false")
                + ") AS policy_held, "
                + (
                    f'count(*) FILTER (WHERE "{column}" >= :bound) AS in_use'
                    if bound is not None
                    else f'count(*) FILTER (WHERE "{column}" >= :cutoff) AS in_use'
                )
                + f" FROM {table}"
            ),
            {"cutoff": cutoff, "bound": bound} if bound is not None else {"cutoff": cutoff},
        ).one()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.warning("band count failed for %s: %s", context.name, exc)
        return _all_in_use(
            total, f"Could not count rows by date: {exc}", "unmeasured", warehouse,
            evidence_bound=bound, policy_floor=floor,
        )

    archivable, policy_held, in_use = int(row[0]), int(row[1]), int(row[2])

    if bound is None:
        reason = (
            f"No consumer reads this table, so the retention floor of "
            f"{floor.isoformat() if floor else 'n/a'} alone decides the cutoff."
        )
    elif binding == "evidence":
        reason = (
            f"The earliest date any consumer still reads is {bound.isoformat()}. "
            f"Loosening retention cannot move it."
        )
    else:
        reason = (
            f"Retention requires history on or after {floor.isoformat()} to stay hot, "
            f"which is earlier than the {bound.isoformat()} any consumer reads -- so "
            f"policy is what holds the middle band, not the consumers."
        )

    return RowBands(
        archivable=archivable,
        policy_held=policy_held,
        in_use=in_use,
        evidence_bound=bound,
        policy_floor=floor,
        cutoff=cutoff,
        binding=binding,
        reason=reason,
        provenance=warehouse,
    )
