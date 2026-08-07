"""Turn consumer history windows into a verdict on one proposed cutoff date.

The rule is small enough to state in one line:

    a cutoff is safe iff it is no later than the earliest date any active downstream
    consumer still reads.

Archived rows are those with `date_column < cutoff`. A consumer reads rows with
`date_column >= earliest_date_read`. Those sets are disjoint exactly when
`cutoff <= earliest_date_read`, so the headroom is `earliest_date_read - cutoff`.
Positive headroom is margin; negative headroom means the archive would eat rows that
consumer is still reading.

Everything unproven blocks. A consumer whose window we could not establish -- no queries
recorded, an unbounded scan, SQL we failed to parse -- is not evidence of safety, so it
is reported as blocking rather than quietly dropped. That is the difference between this
and a cron job that deletes by age.
"""

from __future__ import annotations

from datetime import date

from app.domain.models import (
    ConsumerImpact,
    ConsumerWindow,
    ImpactState,
    Provenance,
    RangeVerdict,
    Recommendation,
    Source,
    WindowDerivation,
)

# Headroom below this is "tight": legal, but close enough that a consumer widening its
# lookback by one reporting period would start reading archived rows. Surfaced so the
# operator chooses rehydration-on-demand consciously.
TIGHT_MARGIN_DAYS = 90


def _state_for(window: ConsumerWindow, cutoff: date) -> tuple[ImpactState, int | None, str]:
    if window.earliest_date_read is None:
        if window.derivation == WindowDerivation.NO_DATE_FILTER:
            return (
                ImpactState.UNKNOWN,
                None,
                "reads the table with no date predicate -- an unbounded scan, so every cutoff "
                "would truncate data it reads",
            )
        if window.derivation == WindowDerivation.NO_QUERIES_OBSERVED:
            return (
                ImpactState.UNKNOWN,
                None,
                "no SQL recorded in DataHub for this consumer, so its history window cannot be "
                "proven -- treated as unbounded",
            )
        if window.derivation == WindowDerivation.NOT_A_QUERY_CONSUMER:
            return (
                ImpactState.UNKNOWN,
                None,
                "consumer does not expose SQL (no query text in the catalog), so its lookback "
                "cannot be proven",
            )
        return (
            ImpactState.UNKNOWN,
            None,
            "date predicate present but could not be resolved to a concrete date",
        )

    headroom = (window.earliest_date_read - cutoff).days

    if headroom < 0:
        return (
            ImpactState.BLOCKED,
            headroom,
            f"reads back to {window.earliest_date_read.isoformat()}; a cutoff of "
            f"{cutoff.isoformat()} would archive {abs(headroom)} days of rows it still reads",
        )

    if headroom < TIGHT_MARGIN_DAYS:
        return (
            ImpactState.TIGHT,
            headroom,
            f"reads back to {window.earliest_date_read.isoformat()}; clears the cutoff by only "
            f"{headroom} days",
        )

    return (
        ImpactState.SAFE,
        headroom,
        f"reads back to {window.earliest_date_read.isoformat()}; clears the cutoff by "
        f"{_humanise(headroom)}",
    )


def _humanise(days: int) -> str:
    if days >= 730:
        return f"{days // 365} years"
    if days >= 365:
        return f"{days // 30} months"
    if days >= 60:
        return f"{days // 30} months"
    return f"{days} days"


class SimulationService:
    """Evaluates a proposed cutoff against every downstream consumer."""

    @staticmethod
    def simulate(
        windows: list[ConsumerWindow],
        cutoff: date,
        *,
        lineage_complete: bool = True,
        lineage_provenance: Provenance | None = None,
    ) -> RangeVerdict:
        impacts: list[ConsumerImpact] = []
        for window in windows:
            state, headroom, reason = _state_for(window, cutoff)
            impacts.append(
                ConsumerImpact(window=window, state=state, headroom_days=headroom, reason=reason)
            )

        # If we could not enumerate lineage at all, we know nothing about who reads this
        # table. That is the least safe state there is, and it must not look like "no
        # consumers found".
        if not lineage_complete:
            return RangeVerdict(
                cutoff_date=cutoff,
                recommendation=Recommendation.DO_NOT_ARCHIVE,
                consumers=impacts,
                binding_constraint=None,
                headroom_days=None,
                rationale=(
                    "Downstream lineage could not be read from DataHub, so the set of consumers "
                    "is unknown. Refusing to archive: absence of evidence is not evidence of "
                    "safety."
                    + (f" ({lineage_provenance.detail})" if lineage_provenance and lineage_provenance.detail else "")
                ),
            )

        blocking = [i for i in impacts if i.state in (ImpactState.BLOCKED, ImpactState.UNKNOWN)]
        tight = [i for i in impacts if i.state == ImpactState.TIGHT]
        safe = [i for i in impacts if i.state == ImpactState.SAFE]

        if blocking:
            # Report the hardest constraint first: a proven overlap beats an unproven one.
            # Prefer a proven overlap over an unproven one, then the deepest overlap, then
            # a consumer whose window came from parsed SQL over one that inherited a bound
            # through lineage -- naming the directly evidenced consumer is both more
            # honest and more useful, since an operator can go and read its query.
            binding = sorted(
                blocking,
                key=lambda i: (
                    i.state != ImpactState.BLOCKED,
                    i.headroom_days if i.headroom_days is not None else 0,
                    i.window.derivation != WindowDerivation.SQL_PREDICATE,
                ),
            )[0]
            if binding.state == ImpactState.BLOCKED:
                rationale = (
                    f"Blocked by {binding.window.consumer_name} "
                    f"({binding.window.consumer_type.lower()}): it reads back to "
                    f"{binding.window.earliest_date_read.isoformat()}, which is "
                    f"{abs(binding.headroom_days)} days inside the proposed cutoff of "
                    f"{cutoff.isoformat()}. Move the cutoff to "
                    f"{binding.window.earliest_date_read.isoformat()} or earlier."
                )
            else:
                rationale = (
                    f"Blocked by {binding.window.consumer_name} "
                    f"({binding.window.consumer_type.lower()}): {binding.reason}. "
                    f"No cutoff can be proven safe while an unbounded consumer exists. "
                    f"Bound its query with a date predicate, or archive with rehydration on demand."
                )
            return RangeVerdict(
                cutoff_date=cutoff,
                recommendation=Recommendation.DO_NOT_ARCHIVE,
                consumers=impacts,
                binding_constraint=binding,
                headroom_days=binding.headroom_days,
                rationale=rationale,
            )

        if not impacts:
            return RangeVerdict(
                cutoff_date=cutoff,
                recommendation=Recommendation.SAFE_TO_ARCHIVE,
                consumers=[],
                binding_constraint=None,
                headroom_days=None,
                rationale=(
                    "DataHub lineage reports no downstream consumers for this dataset, so no "
                    "query window constrains the cutoff."
                ),
            )

        # Tightest consumer governs. On a tie, prefer one whose window came from parsed
        # SQL over one that merely inherited a bound through lineage -- the directly
        # evidenced consumer is the more honest thing to name as the constraint, and it
        # is the one an operator can go and look at.
        bounded = [i for i in impacts if i.headroom_days is not None]
        binding = (
            min(
                bounded,
                key=lambda i: (
                    i.headroom_days,
                    i.window.derivation != WindowDerivation.SQL_PREDICATE,
                ),
            )
            if bounded
            else None
        )
        headroom = binding.headroom_days if binding else None

        if tight:
            return RangeVerdict(
                cutoff_date=cutoff,
                recommendation=Recommendation.ARCHIVE_WITH_REHYDRATION,
                consumers=impacts,
                binding_constraint=binding,
                headroom_days=headroom,
                rationale=(
                    f"Safe today, but {binding.window.consumer_name} clears the cutoff by only "
                    f"{binding.headroom_days} days. Archive with rehydration available on demand "
                    f"so that consumer can recover the range if it widens its lookback."
                ),
            )

        return RangeVerdict(
            cutoff_date=cutoff,
            recommendation=Recommendation.SAFE_TO_ARCHIVE,
            consumers=impacts,
            binding_constraint=binding,
            headroom_days=headroom,
            rationale=(
                f"All {len(safe)} downstream consumers read strictly newer than "
                f"{cutoff.isoformat()}. Tightest is {binding.window.consumer_name}, which clears "
                f"the cutoff by {_humanise(binding.headroom_days)}."
            ),
        )

    @staticmethod
    def latest_safe_cutoff(windows: list[ConsumerWindow]) -> date | None:
        """The most aggressive cutoff that is still provably safe.

        None when any consumer is unbounded -- there is no safe cutoff in that case, which
        is precisely the finding that a table-level 'this is cold' check would miss.
        """
        bounds: list[date] = []
        for window in windows:
            if window.earliest_date_read is None:
                return None
            bounds.append(window.earliest_date_read)
        return min(bounds) if bounds else None
