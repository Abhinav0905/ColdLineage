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
    table cannot be split at all -- some consumer's reach could not be proven, so
    no row is provably unread.

    The test is `earliest_date_read is None`, deliberately NOT `is_unbounded`.
    Those two disagree: `is_unbounded` is True only for NO_DATE_FILTER and
    NO_QUERIES_OBSERVED, while `SimulationService._state_for` treats *every*
    unresolved window as UNKNOWN and therefore blocking -- including
    NOT_A_QUERY_CONSUMER and "predicate present but unresolvable".

    Using `is_unbounded` here would let this chart paint rows green that
    /simulate would refuse, which is the disagreement that matters: the picture
    would be wrong in the unsafe direction. Whatever blocks a cutoff must also
    keep rows out of the archivable band.
    """
    unproven = [w for w in context.downstream if w.earliest_date_read is None]
    if unproven:
        names = ", ".join(w.consumer_name for w in unproven[:3])
        more = f" and {len(unproven) - 3} more" if len(unproven) > 3 else ""
        return None, (
            f"{len(unproven)} consumer(s) cannot be shown to stop reading at any date "
            f"({names}{more}), so no row can be proven unread."
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
    #
    # Two dates decide everything, and either may legitimately be absent:
    #
    #   archive_before  the latest date it is both safe and permitted to archive
    #                   before. None means nothing may move at all.
    #   read_from       the earliest date a consumer can still reach. None means
    #                   nobody reads this table, so no row is "in use".
    #
    # Counting only those two edges and deriving the middle by subtraction means
    # the three bands cannot fail to sum to the total. Three independent FILTER
    # clauses can disagree at a boundary; a subtraction cannot.
    candidates = [d for d in (bound, floor) if d is not None]
    archive_before = min(candidates) if candidates else None
    read_from = bound
    hold_matter: str | None = None

    if context.legal_hold:
        # A legal hold stops the archive dead -- but it does NOT mean a consumer
        # is reading those rows. Reporting them as "in use" would put a false
        # statement on the chart, so they stay in the held band where they belong,
        # and the reason says which kind of hold it is.
        hold_matter = context.legal_hold_matter or "unspecified matter"
        archive_before = None

    if archive_before is None and read_from is None and not context.legal_hold:
        return _all_in_use(
            total,
            "Neither a consumer bound nor a retention floor is known, so nothing is proven safe.",
            "unmeasured", policy, policy_floor=floor,
        )

    binding = (
        "legal_hold"
        if context.legal_hold
        else "evidence"
        if (bound is not None and archive_before == bound)
        else "policy"
    )

    # -- count the two edges -----------------------------------------------
    column, table = context.date_column, context.qualified_table
    try:
        archivable = (
            int(
                db.execute(
                    text(f'SELECT count(*) FROM {table} WHERE "{column}" < :d'),
                    {"d": archive_before},
                ).scalar()
                or 0
            )
            if archive_before is not None
            else 0
        )
        in_use = (
            int(
                db.execute(
                    text(f'SELECT count(*) FROM {table} WHERE "{column}" >= :d'),
                    {"d": read_from},
                ).scalar()
                or 0
            )
            if read_from is not None
            else 0
        )
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.warning("band count failed for %s: %s", context.name, exc)
        return _all_in_use(
            total, f"Could not count rows by date: {exc}", "unmeasured", warehouse,
            evidence_bound=bound, policy_floor=floor,
        )

    policy_held = max(0, total - archivable - in_use)

    if hold_matter:
        reason = (
            f"An active legal hold ({hold_matter}) forbids moving any row, whatever the "
            f"retention setting says. These rows are not being read -- they are frozen."
        )
    elif bound is None:
        reason = (
            f"No consumer reads this table at all, so the retention floor of "
            f"{floor.isoformat() if floor else 'n/a'} alone decides what may move."
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

    cutoff = archive_before
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
