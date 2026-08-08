"""Build the evidence graph and the blocker list for a dataset.

Blockers are kept out of the temperature score on purpose. A legal hold does not make
data "hotter" -- it makes the question moot. Folding it into a 0-100 number would let a
sufficiently cold dataset out-vote a court order, and would make the explanation
un-auditable. So the score answers "is anyone using this?" and blockers answer
"are we allowed to touch it?", separately, and both are shown.

Two kinds of blocker:

  cutoff-independent   evaluated here, from the dataset context alone
                       (legal hold, no date column, deprecation, unbounded consumers)
  cutoff-dependent     evaluated in plan.py, because they depend on the proposed date
                       (retention floor, consumer overlap)
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from dateutil.relativedelta import relativedelta

from app.domain.models import (
    Blocker,
    DatasetAssessment,
    DatasetContext,
    EvidenceItem,
    Source,
)
from app.services.temperature import TemperatureService

# A dataset scoring at or above this is too warm to consider archiving at all,
# regardless of cutoff. Below it, the cutoff analysis decides.
ARCHIVE_CONSIDERATION_CEILING = 45.0


class EvidenceService:
    @staticmethod
    def retention_floor(context: DatasetContext, now: date | None = None) -> date | None:
        """The oldest cutoff policy permits. Rows on or after this must stay hot.

        Resolved in months, not years. `int(retention_years)` silently truncated
        every fractional setting -- 1.5 years became 1, and four months (0.33)
        became zero, which quietly removed the floor altogether. The property is
        documented as accepting fractions, so it has to honour them.
        """
        if context.retention_years is None:
            return None
        now = now or date.today()
        return now - relativedelta(months=round(float(context.retention_years) * 12))

    @staticmethod
    def build(context: DatasetContext, now: datetime | None = None) -> DatasetAssessment:
        now = now or datetime.now(UTC)
        temperature = TemperatureService.score(context, now=now)

        evidence: list[EvidenceItem] = []
        blockers: list[Blocker] = []

        # -- legal hold: unconditional
        if context.legal_hold:
            matter = context.legal_hold_matter or "matter not recorded"
            blockers.append(
                Blocker(
                    code="LEGAL_HOLD",
                    message=f"Active legal hold ({matter}). Archival and deletion are blocked unconditionally.",
                    provenance=context.policy_provenance,
                )
            )
            evidence.append(
                EvidenceItem(
                    kind="policy",
                    label=f"Active legal hold - {matter}",
                    status="block",
                    provenance=context.policy_provenance,
                )
            )
        elif context.policy_provenance.source == Source.UNAVAILABLE:
            blockers.append(
                Blocker(
                    code="POLICY_UNAVAILABLE",
                    message=(
                        "Retention and legal-hold policy could not be read from DataHub, so "
                        "compliance with them cannot be asserted."
                    ),
                    provenance=context.policy_provenance,
                )
            )
            evidence.append(
                EvidenceItem(
                    kind="policy",
                    label="Policy inputs unavailable",
                    status="block",
                    provenance=context.policy_provenance,
                )
            )
        else:
            evidence.append(
                EvidenceItem(
                    kind="policy",
                    label="No active legal hold",
                    status="pass",
                    provenance=context.policy_provenance,
                )
            )

        # -- retention floor
        floor = EvidenceService.retention_floor(context, now.date())
        if floor is not None:
            evidence.append(
                EvidenceItem(
                    kind="policy",
                    label=(
                        f"Retention floor {context.retention_years:g}y - cutoff must be on or "
                        f"before {floor.isoformat()}"
                    ),
                    status="pass",
                    provenance=context.policy_provenance,
                )
            )
        else:
            evidence.append(
                EvidenceItem(
                    kind="policy",
                    label="No retention floor declared",
                    status="warn",
                    provenance=context.policy_provenance,
                )
            )

        # -- date column
        if not context.date_column:
            blockers.append(
                Blocker(
                    code="NO_DATE_COLUMN",
                    message=(
                        "No DATE or TIMESTAMP column found in the DataHub schema, so a historical "
                        "range cannot be defined. Range archival requires one."
                    ),
                    provenance=context.date_column_provenance,
                )
            )
            evidence.append(
                EvidenceItem(
                    kind="schema",
                    label="No date column - range archival impossible",
                    status="block",
                    provenance=context.date_column_provenance,
                )
            )
        else:
            evidence.append(
                EvidenceItem(
                    kind="schema",
                    label=f"Range column: {context.date_column}",
                    status="pass",
                    provenance=context.date_column_provenance,
                )
            )

        # -- usage
        if context.last_query_at is None:
            evidence.append(
                EvidenceItem(
                    kind="usage",
                    label="No usage telemetry in DataHub - scored as hot",
                    status="warn",
                    provenance=context.usage_provenance,
                )
            )
        else:
            days = (now - context.last_query_at.replace(tzinfo=UTC)).days
            evidence.append(
                EvidenceItem(
                    kind="usage",
                    label=(
                        f"Last query {days}d ago, {context.query_count_30d or 0} queries / 30d, "
                        f"{context.distinct_users_30d or 0} distinct users"
                    ),
                    status="pass" if days > 180 else "warn",
                    provenance=context.usage_provenance,
                )
            )

        # -- lineage and the unbounded-consumer check
        unbounded = [w for w in context.downstream if w.is_unbounded]
        if unbounded:
            names = ", ".join(w.consumer_name for w in unbounded[:3])
            more = f" (+{len(unbounded) - 3} more)" if len(unbounded) > 3 else ""
            blockers.append(
                Blocker(
                    code="UNBOUNDED_CONSUMER",
                    message=(
                        f"{len(unbounded)} downstream consumer(s) read this table without a date "
                        f"predicate: {names}{more}. No cutoff can be proven safe while an "
                        f"unbounded scan exists."
                    ),
                    provenance=unbounded[0].provenance,
                )
            )
            evidence.append(
                EvidenceItem(
                    kind="lineage",
                    label=f"{len(unbounded)} unbounded consumer(s): {names}{more}",
                    status="block",
                    provenance=unbounded[0].provenance,
                )
            )

        bounded = [w for w in context.downstream if not w.is_unbounded]
        if bounded:
            earliest = min(w.earliest_date_read for w in bounded if w.earliest_date_read)
            evidence.append(
                EvidenceItem(
                    kind="lineage",
                    label=(
                        f"{len(bounded)} bounded consumer(s); earliest history read is "
                        f"{earliest.isoformat()}"
                    ),
                    status="pass",
                    provenance=bounded[0].provenance,
                )
            )
        elif not context.downstream:
            evidence.append(
                EvidenceItem(
                    kind="lineage",
                    label="No downstream consumers in DataHub lineage",
                    status="pass",
                    provenance=context.usage_provenance,
                )
            )

        # -- classification
        if context.sensitive:
            evidence.append(
                EvidenceItem(
                    kind="classification",
                    label=(
                        "Sensitive data (" + ", ".join(context.tags[:3]) + ") - access controls "
                        "must be preserved on the archived object"
                    ),
                    status="warn",
                    provenance=context.policy_provenance,
                )
            )

        if context.deprecated:
            evidence.append(
                EvidenceItem(
                    kind="deprecation",
                    label="Already marked deprecated in DataHub",
                    status="warn",
                    provenance=context.policy_provenance,
                )
            )

        # -- temperature is context, NOT a gate.
        #
        # This deliberately does not block. A table-level heat score says whether the
        # *table* is in use; it says nothing about whether its oldest rows are. Blocking
        # range archival because the table is warm would reduce this product to the
        # dataset-level thinking it exists to replace -- and it would reject the most
        # valuable case there is: a heavily-queried table whose first four years no
        # consumer has read in years.
        #
        # What decides is the range verdict: the consumer windows in simulation.py.
        evidence.append(
            EvidenceItem(
                kind="usage",
                label=(
                    f"Table temperature {temperature.score} ({temperature.classification}) - "
                    f"informational; range safety is decided per-cutoff by consumer windows"
                ),
                status="pass" if temperature.score < ARCHIVE_CONSIDERATION_CEILING else "warn",
                provenance=context.usage_provenance,
            )
        )

        eligible = not blockers

        # Confidence reflects how much of the decision rested on real inputs rather than
        # fail-closed defaults. It is a coverage measure, not a probability.
        signals = [
            context.usage_provenance.source != Source.UNAVAILABLE,
            context.policy_provenance.source != Source.UNAVAILABLE,
            context.physical_provenance.source != Source.UNAVAILABLE,
            context.date_column is not None,
            bool(context.downstream),
        ]
        confidence = round(sum(1 for s in signals if s) / len(signals), 2)

        return DatasetAssessment(
            context=context,
            temperature=temperature,
            evidence=evidence,
            blockers=blockers,
            archive_eligible=eligible,
            confidence=confidence,
        )
