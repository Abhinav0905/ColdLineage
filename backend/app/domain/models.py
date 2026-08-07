"""Domain contract for ColdLineage.

Every value that informs an archive decision carries a `Provenance` saying where it
came from. That is the whole point of the project: a tiering decision is only
defensible if you can show which system supplied each input. A number with no
provenance is a number someone made up.
"""

from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class Source(StrEnum):
    """Where a signal came from. `SEEDED` exists so that fabricated values are
    visibly fabricated in the API response and in the UI, never silently mixed
    in with real catalog data."""

    DATAHUB_LINEAGE = "datahub:lineage"
    DATAHUB_USAGE = "datahub:usage"
    DATAHUB_QUERIES = "datahub:queries"
    DATAHUB_PROPERTIES = "datahub:structured_properties"
    DATAHUB_TAGS = "datahub:tags"
    DATAHUB_OWNERSHIP = "datahub:ownership"
    DATAHUB_SCHEMA = "datahub:schema"
    DATAHUB_DEPRECATION = "datahub:deprecation"
    WAREHOUSE = "warehouse:postgres"
    CASSETTE = "cassette:recorded"
    UNAVAILABLE = "unavailable"


class Provenance(BaseModel):
    source: Source
    detail: str = ""
    observed_at: datetime | None = None


# --------------------------------------------------------------------------
# Consumers and their history windows -- the core of the differentiator.
# --------------------------------------------------------------------------


class WindowDerivation(StrEnum):
    """How we learned how far back a consumer reads.

    Ordered by strength. `SQL_PREDICATE` is the strong case: we parsed a real
    query and found an explicit lower bound on the date column. `NO_DATE_FILTER`
    is the dangerous case -- the consumer issues an unbounded scan, so *any*
    cutoff truncates data it reads.
    """

    SQL_PREDICATE = "sql_predicate"
    DECLARED_PROPERTY = "declared_property"
    NO_DATE_FILTER = "no_date_filter"
    NO_QUERIES_OBSERVED = "no_queries_observed"
    NOT_A_QUERY_CONSUMER = "not_a_query_consumer"


class ConsumerWindow(BaseModel):
    """How far back one downstream consumer actually reads from this dataset."""

    consumer_urn: str
    consumer_name: str
    consumer_type: str  # DATASET | DASHBOARD | CHART | MLMODEL | DATA_JOB
    platform: str | None = None
    degree: int = 1  # lineage hops from the subject dataset

    earliest_date_read: date | None = None
    derivation: WindowDerivation
    predicate: str | None = None  # the extracted WHERE fragment, verbatim
    evidence_sql: str | None = None  # the query we parsed, verbatim
    query_last_seen: datetime | None = None
    query_run_count: int | None = None
    provenance: Provenance

    @property
    def is_unbounded(self) -> bool:
        """True when this consumer may read arbitrarily far back, so no cutoff is provably safe."""
        return self.earliest_date_read is None and self.derivation in (
            WindowDerivation.NO_DATE_FILTER,
            WindowDerivation.NO_QUERIES_OBSERVED,
        )


class ImpactState(StrEnum):
    SAFE = "safe"  # cutoff is comfortably older than anything this consumer reads
    TIGHT = "tight"  # clears it, but by less than the margin
    BLOCKED = "blocked"  # cutoff would remove rows this consumer still reads
    UNKNOWN = "unknown"  # we could not establish a bound -- treated as blocking


class ConsumerImpact(BaseModel):
    window: ConsumerWindow
    state: ImpactState
    headroom_days: int | None = None  # cutoff -> earliest_date_read, negative means overlap
    reason: str


# --------------------------------------------------------------------------
# Evidence, blockers, temperature
# --------------------------------------------------------------------------


class EvidenceItem(BaseModel):
    kind: str  # usage | lineage | policy | classification | schema | deprecation
    label: str
    status: Literal["pass", "warn", "block"]
    provenance: Provenance


class Blocker(BaseModel):
    code: str  # LEGAL_HOLD | RETENTION_FLOOR | UNBOUNDED_CONSUMER | NO_DATE_COLUMN | DEPRECATED_UPSTREAM
    message: str
    provenance: Provenance


class TemperatureBreakdown(BaseModel):
    """Deterministic and inspectable. Every component reports its own input and provenance
    so the score can be argued with rather than trusted."""

    recency_component: float
    frequency_component: float
    downstream_component: float
    criticality_component: float
    score: float  # 0-100, higher = hotter
    classification: str  # HOT | WARM | COOL | COLD | FROZEN
    inputs: dict[str, str]  # human-readable "what fed this", including provenance labels


# --------------------------------------------------------------------------
# Dataset context assembled from DataHub
# --------------------------------------------------------------------------


class DatasetContext(BaseModel):
    urn: str
    name: str
    platform: str
    qualified_table: str  # what the executor will actually touch, e.g. "public"."patient_encounters"
    date_column: str | None
    date_column_provenance: Provenance

    owners: list[str] = Field(default_factory=list)
    domain: str | None = None
    tags: list[str] = Field(default_factory=list)
    glossary_terms: list[str] = Field(default_factory=list)
    deprecated: bool = False

    # Policy, read from DataHub structured properties -- not from a local column.
    retention_years: float | None = None
    legal_hold: bool = False
    legal_hold_matter: str | None = None
    business_criticality: float | None = None
    policy_provenance: Provenance

    # Usage, read from DataHub usage aspects.
    last_query_at: datetime | None = None
    query_count_30d: int | None = None
    distinct_users_30d: int | None = None
    # True when DataHub actually has a usage aspect for this dataset. Distinguishes
    # "measured, and nobody read it" (genuinely cold) from "not measured" (unknown, and
    # therefore scored hot). Without this, an idle table is indistinguishable from an
    # unmonitored one.
    usage_observed: bool = False
    usage_provenance: Provenance

    downstream: list[ConsumerWindow] = Field(default_factory=list)

    # Physical facts, measured from the warehouse -- never estimated.
    row_count: int | None = None
    size_bytes: int | None = None
    # The date span actually present in the table. The UI draws the range timeline
    # against this, so it has to be measured, not inferred from the cutoff.
    min_date: date | None = None
    max_date: date | None = None
    physical_provenance: Provenance

    sensitive: bool = False


class DatasetAssessment(BaseModel):
    context: DatasetContext
    temperature: TemperatureBreakdown
    evidence: list[EvidenceItem]
    blockers: list[Blocker]
    archive_eligible: bool
    confidence: float | None = None


# --------------------------------------------------------------------------
# Plan / simulate / execute
# --------------------------------------------------------------------------


class Recommendation(StrEnum):
    SAFE_TO_ARCHIVE = "SAFE_TO_ARCHIVE"
    ARCHIVE_WITH_REHYDRATION = "ARCHIVE_WITH_REHYDRATION"
    DO_NOT_ARCHIVE = "DO_NOT_ARCHIVE"


class RangeVerdict(BaseModel):
    """The answer to 'is this specific date range safe to move?'.

    This is what DataHub structurally cannot express: its model is dataset- and
    column-level, so it can say a table is cold but not that rows before a given
    date are cold while recent rows stay hot.
    """

    cutoff_date: date
    recommendation: Recommendation
    consumers: list[ConsumerImpact]
    binding_constraint: ConsumerImpact | None = None
    headroom_days: int | None = None
    rationale: str


class ArchivePlan(BaseModel):
    plan_hash: str  # binds dataset + cutoff + row count + verdict; execute() requires it
    dataset_urn: str
    cutoff_date: date
    rows_in_scope: int
    bytes_in_scope: int
    verdict: RangeVerdict
    blockers: list[Blocker]
    monthly_savings_usd: float
    requires_approval: bool = True
    created_at: datetime


class ArchiveManifest(BaseModel):
    dataset_urn: str
    table: str
    cutoff_date: date
    rows: int
    bytes: int
    parts: list[dict]  # per-part {key, rows, bytes, sha256}
    sha256: str  # digest over the concatenated part digests
    columns: list[str]
    object_uri: str
    manifest_uri: str
    verified_readback: bool
    created_at: datetime


class VerificationReport(BaseModel):
    """Produced after the object lands and BEFORE any row is deleted."""

    readback_sha256_match: bool
    readback_row_count: int
    source_row_count: int
    row_count_match: bool
    schema_match: bool
    passed: bool
    checked_at: datetime
