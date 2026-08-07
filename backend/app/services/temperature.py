"""Deterministic temperature scoring.

Higher is hotter. The weights are fixed and published so the number can be argued with:

    42%  access recency        how long since anyone queried it
    28%  query frequency       how often, over the trailing 30 days
    18%  active downstream     how many consumers depend on it
    12%  business criticality  owner-declared, read from DataHub, never inferred

No model, no embedding, no LLM. A tiering decision that a data owner cannot reproduce by
hand is a tiering decision they will not sign off on, and the score is only an input --
legal hold and retention are separate hard blockers rather than terms hidden inside it.

MISSING SIGNALS SCORE HOT, NOT COLD.
Absent usage data means we do not know whether anyone is reading this table. Treating
"unknown" as "nobody has touched it" is exactly the bug that deletes production data, so
an unavailable signal contributes its maximum value and the affected input is named in
`inputs` so the gap is visible on screen.
"""

from __future__ import annotations

from datetime import UTC, datetime
from math import log1p

from app.domain.models import DatasetContext, Source, TemperatureBreakdown

W_RECENCY = 0.42
W_FREQUENCY = 0.28
W_DOWNSTREAM = 0.18
W_CRITICALITY = 0.12

RECENCY_HORIZON_DAYS = 730  # two years without a query scores fully cold
FREQUENCY_SATURATION = 1000  # queries per 30 days at which frequency saturates
DOWNSTREAM_SATURATION = 5


def classify(score: float) -> str:
    if score >= 75:
        return "HOT"
    if score >= 45:
        return "WARM"
    if score >= 20:
        return "COOL"
    if score >= 8:
        return "COLD"
    return "FROZEN"


class TemperatureService:
    @staticmethod
    def score(context: DatasetContext, now: datetime | None = None) -> TemperatureBreakdown:
        now = now or datetime.now(UTC)
        inputs: dict[str, str] = {}

        # -- recency
        if context.last_query_at is None and context.usage_observed:
            # DataHub measured this dataset and recorded no activity. That is evidence of
            # coldness, not a gap -- and it is exactly the signal that makes an idle table
            # a candidate. Scoring it as UNKNOWN here would bury the finding.
            recency = 0.0
            inputs["access_recency"] = (
                "no queries in the observed 30-day window -> 0.00 "
                f"[{context.usage_provenance.source.value}]"
            )
        elif context.last_query_at is None:
            recency = 1.0
            inputs["access_recency"] = (
                f"UNKNOWN -> scored as hot (1.00). {context.usage_provenance.detail or 'no usage aspect in DataHub'}"
            )
        else:
            last = context.last_query_at
            if last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            days = max((now - last).days, 0)
            recency = max(0.0, 1.0 - min(days, RECENCY_HORIZON_DAYS) / RECENCY_HORIZON_DAYS)
            inputs["access_recency"] = (
                f"last query {days}d ago -> {recency:.2f} [{context.usage_provenance.source.value}]"
            )

        # -- frequency
        if context.query_count_30d is None and context.usage_observed:
            frequency = 0.0
            inputs["query_frequency"] = (
                f"0 queries/30d (measured) -> 0.00 [{context.usage_provenance.source.value}]"
            )
        elif context.query_count_30d is None:
            frequency = 1.0
            inputs["query_frequency"] = (
                f"UNKNOWN -> scored as hot (1.00). {context.usage_provenance.detail or 'no usage aspect in DataHub'}"
            )
        else:
            count = max(context.query_count_30d, 0)
            frequency = min(log1p(count) / log1p(FREQUENCY_SATURATION), 1.0)
            inputs["query_frequency"] = (
                f"{count} queries/30d -> {frequency:.2f} [{context.usage_provenance.source.value}]"
            )

        # -- downstream
        active = len(context.downstream)
        lineage_known = any(
            w.provenance.source != Source.UNAVAILABLE for w in context.downstream
        ) or active > 0
        if not lineage_known and active == 0:
            downstream = 1.0
            inputs["downstream_dependencies"] = "UNKNOWN -> scored as hot (1.00). lineage unavailable"
        else:
            downstream = min(active / DOWNSTREAM_SATURATION, 1.0)
            unbounded = sum(1 for w in context.downstream if w.is_unbounded)
            suffix = f", {unbounded} unbounded" if unbounded else ""
            inputs["downstream_dependencies"] = (
                f"{active} active consumer(s){suffix} -> {downstream:.2f} [datahub:lineage]"
            )

        # -- criticality
        if context.business_criticality is None:
            criticality = 1.0
            inputs["business_criticality"] = (
                "UNDECLARED -> scored as hot (1.00). Set io.coldlineage.policy.businessCriticality "
                "in DataHub to refine."
            )
        else:
            criticality = min(max(context.business_criticality, 0.0), 1.0)
            inputs["business_criticality"] = (
                f"declared {criticality:.2f} [{context.policy_provenance.source.value}]"
            )

        score = 100.0 * (
            W_RECENCY * recency
            + W_FREQUENCY * frequency
            + W_DOWNSTREAM * downstream
            + W_CRITICALITY * criticality
        )
        score = round(score, 1)

        return TemperatureBreakdown(
            recency_component=round(W_RECENCY * recency * 100, 1),
            frequency_component=round(W_FREQUENCY * frequency * 100, 1),
            downstream_component=round(W_DOWNSTREAM * downstream * 100, 1),
            criticality_component=round(W_CRITICALITY * criticality * 100, 1),
            score=score,
            classification=classify(score),
            inputs=inputs,
        )
