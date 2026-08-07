"""Single source of truth for the demo estate's downstream consumers.

Every consumer below carries THE ACTUAL SQL IT RUNS. That SQL is:

  1. ingested into DataHub as a `query` entity (queryProperties.statement) by
     scripts/ingest_datahub.py, associated with the subject dataset through
     `querySubjects`;
  2. later read back out of DataHub by the ColdLineage backend and parsed with
     sqlglot to derive how far back that consumer actually reads.

Nothing in the product reads this file. The product reads DataHub. This file only
puts the queries there. That distinction is the whole reason the project can claim
its context is real.

WHY THE PREDICATE SHAPES VARY
-----------------------------
A history window is only defensible if the parser handles the predicate forms that
occur in the wild. The estate deliberately covers all of them:

    literal lower bound       event_date >= DATE '2025-08-01'
    relative interval         event_date >= CURRENT_DATE - INTERVAL '90 days'
    BETWEEN                   event_date BETWEEN DATE '2024-01-01' AND CURRENT_DATE
    function-wrapped          event_date > (NOW() - INTERVAL '24 months')::date
    date_trunc comparison     date_trunc('month', event_date)
                                  >= date_trunc('month', CURRENT_DATE - INTERVAL '18 months')
    filtered but undated      WHERE performing_lab IS NOT NULL   <- no date bound at all
    no observed query         lineage edge exists, no query text captured
    not a query consumer      reachable through lineage, never touches the subject

The sixth case is the one that matters most. `hipaa_lab_disclosure_extract` has a
WHERE clause, so a naive "does this query have a WHERE?" heuristic passes it. It has
no date predicate, so it reads every row ever written. A parser that confuses "has a
filter" with "has a date bound" will approve an archive that silently truncates a
regulatory submission.

THE COLUMN NAMES ARE DELIBERATELY DIFFERENT PER TABLE
-----------------------------------------------------
event_date, service_date, collected_date, posted_date. A parser that hardcodes one
date column name will appear to work and be wrong. The window derivation has to key
off the subject dataset's own date column, which it learns from DataHub's schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from estate import (
    ANCHOR,
    BILLING_LEDGER,
    CARE_EVENTS_LIVE,
    CLAIMS_HISTORY,
    LAB_RESULTS,
    PATIENT_ENCOUNTERS,
    TableSpec,
    month_start,
    months_before,
)

# --------------------------------------------------------------------------
# Consumer types and urn construction
# --------------------------------------------------------------------------

DATASET = "DATASET"
DASHBOARD = "DASHBOARD"
CHART = "CHART"
MLMODEL = "MLMODEL"
DATA_JOB = "DATA_JOB"

DATAHUB_ENV = "PROD"

# DataJobs live under DataFlows. One flow per functional pipeline group.
AIRFLOW_FLOWS = {
    "clinical_pipelines": "Clinical batch pipelines",
    "compliance_pipelines": "Regulatory and compliance extracts",
    "finance_pipelines": "Revenue-cycle and close pipelines",
    "ml_training": "Model training pipelines",
}


def dataflow_urn(flow_id: str, orchestrator: str = "airflow") -> str:
    return f"urn:li:dataFlow:({orchestrator},{flow_id},{DATAHUB_ENV})"


def datajob_urn(flow_id: str, job_id: str, orchestrator: str = "airflow") -> str:
    return f"urn:li:dataJob:({dataflow_urn(flow_id, orchestrator)},{job_id})"


def dataset_urn(platform: str, name: str) -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:{platform},{name},{DATAHUB_ENV})"


def dashboard_urn(tool: str, dash_id: str) -> str:
    return f"urn:li:dashboard:({tool},{dash_id})"


def chart_urn(tool: str, chart_id: str) -> str:
    return f"urn:li:chart:({tool},{chart_id})"


def mlmodel_urn(platform: str, name: str) -> str:
    return f"urn:li:mlModel:(urn:li:dataPlatform:{platform},{name},{DATAHUB_ENV})"


def _ts(days_ago: int, hour: int = 6) -> datetime:
    """A UTC timestamp `days_ago` days before the estate anchor."""
    return datetime.combine(
        ANCHOR - timedelta(days=days_ago), time(hour=hour, minute=17), tzinfo=timezone.utc
    )


# --------------------------------------------------------------------------
# Test oracle
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Expectation:
    """What a correct parse of this consumer's SQL should produce.

    NOT ingested into DataHub. NOT read by the backend. This exists only so
    scripts/smoke_test.py can assert that the backend derived the right window
    from the SQL rather than from anything preloaded. If the backend ever agreed
    with these values without parsing, the smoke test would be worthless -- so
    the test asserts against the API response, and this is the oracle it checks.
    """

    derivation: str  # matches domain.models.WindowDerivation values
    earliest_date_read: date | None
    note: str = ""


# --------------------------------------------------------------------------
# Consumer record
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Consumer:
    key: str
    name: str
    urn: str
    consumer_type: str
    platform: str
    subject: TableSpec
    description: str
    sql: str | None
    last_run_at: datetime | None
    run_count: int | None
    expectation: Expectation
    degree: int = 1
    # For MLMODEL consumers: DataHub models mlModel lineage as TrainedBy -> dataJob,
    # so the actual lineage edge to the dataset is carried by a training job.
    lineage_via_job: str | None = None
    # For degree-2 consumers: the intermediate entity urn they actually read.
    reads_via: str | None = None
    external_url: str | None = None

    @property
    def query_id(self) -> str:
        """Deterministic query urn id so re-ingestion updates instead of duplicating."""
        return f"coldlineage-{self.subject.key}-{self.key}"

    @property
    def query_urn(self) -> str:
        return f"urn:li:query:{self.query_id}"


# --------------------------------------------------------------------------
# patient_encounters -- HERO. Every consumer bounded, so a 2023 cutoff is provable.
# --------------------------------------------------------------------------

_PE = PATIENT_ENCOUNTERS

QUARTERLY_COMPLIANCE_DASHBOARD = Consumer(
    key="quarterly_compliance_dashboard",
    name="Quarterly Compliance Dashboard",
    urn=dashboard_urn("superset", "quarterly_compliance_dashboard"),
    consumer_type=DASHBOARD,
    platform="superset",
    subject=_PE,
    description=(
        "Quarterly encounter-volume and charge attestation reviewed by the compliance "
        "committee. Reaches back to the start of the current attestation cycle."
    ),
    sql="""SELECT e.region,
       e.encounter_type,
       date_trunc('quarter', e.event_date)::date AS quarter_start,
       count(*)                                  AS encounters,
       sum(e.total_charges)                      AS total_charges
FROM public.patient_encounters e
WHERE e.event_date BETWEEN DATE '2024-01-01' AND CURRENT_DATE
GROUP BY 1, 2, 3
ORDER BY 3 DESC, 1""",
    last_run_at=_ts(2, hour=7),
    run_count=96,
    external_url="https://superset.example-health.org/dashboard/quarterly-compliance",
    expectation=Expectation(
        "sql_predicate", date(2024, 1, 1),
        "BETWEEN with a literal lower bound. This is the binding constraint for the "
        "hero case: it is the earliest date any patient_encounters consumer reads.",
    ),
)

PATIENT_LTV_MODEL = Consumer(
    key="patient_ltv_model",
    name="Patient Lifetime Value Model",
    urn=mlmodel_urn("mlflow", "patient_ltv_model"),
    consumer_type=MLMODEL,
    platform="mlflow",
    subject=_PE,
    description=(
        "Gradient-boosted lifetime-value estimate used for outreach prioritisation. "
        "Retrained monthly on a rolling 24-month window."
    ),
    sql="""SELECT e.patient_id,
       count(*)                   AS encounter_count,
       sum(e.total_charges)       AS lifetime_charges,
       avg(e.length_of_stay_days) AS avg_length_of_stay,
       max(e.event_date)          AS last_seen
FROM public.patient_encounters e
WHERE e.event_date > (NOW() - INTERVAL '24 months')::date
GROUP BY e.patient_id""",
    last_run_at=_ts(6, hour=2),
    run_count=48,
    degree=2,  # mlModel --TrainedBy--> dataJob --Consumes--> dataset
    lineage_via_job=datajob_urn("ml_training", "patient_ltv_training"),
    external_url="https://mlflow.example-health.org/models/patient_ltv_model",
    expectation=Expectation(
        "sql_predicate", months_before(ANCHOR, 24),
        "Function-wrapped relative bound: NOW() - INTERVAL, cast to date, compared with "
        "a strict >. Exercises cast unwrapping and strict-vs-inclusive handling.",
    ),
)

ENCOUNTERS_MONTHLY_AGG = Consumer(
    key="encounters_monthly_agg",
    name="encounters_monthly_agg",
    urn=dataset_urn("dbt", "coldlineage.analytics.encounters_monthly_agg"),
    consumer_type=DATASET,
    platform="dbt",
    subject=_PE,
    description=(
        "Incremental dbt model rolling encounters up to department-month. Rebuilds an "
        "18-month trailing window each night."
    ),
    sql="""SELECT date_trunc('month', e.event_date)::date AS month_start,
       e.department,
       e.payer,
       count(*)             AS encounters,
       sum(e.total_charges) AS charges
FROM public.patient_encounters e
WHERE date_trunc('month', e.event_date) >= date_trunc('month', CURRENT_DATE - INTERVAL '18 months')
GROUP BY 1, 2, 3""",
    last_run_at=_ts(0, hour=3),
    run_count=540,
    expectation=Expectation(
        "sql_predicate", month_start(months_before(ANCHOR, 18)),
        "date_trunc on both sides. The bound is the START of the month 18 months back, "
        "not the same day-of-month -- a parser that ignores the trunc is off by up to 30 days.",
    ),
)

CARE_GAP_CLOSURE_CHART = Consumer(
    key="care_gap_closure_chart",
    name="Care Gap Closure Rate",
    urn=chart_urn("superset", "care_gap_closure_chart"),
    consumer_type=CHART,
    platform="superset",
    subject=_PE,
    description="Preventive-encounter share by diagnosis for the current plan year.",
    sql="""SELECT e.primary_diagnosis_code,
       count(*) FILTER (WHERE e.encounter_type = 'preventive') AS preventive_encounters,
       count(*)                                                AS total_encounters
FROM public.patient_encounters e
WHERE e.event_date >= DATE '2025-08-01'
GROUP BY e.primary_diagnosis_code
ORDER BY total_encounters DESC
LIMIT 25""",
    last_run_at=_ts(0, hour=9),
    run_count=1204,
    external_url="https://superset.example-health.org/chart/care-gap-closure",
    expectation=Expectation(
        "sql_predicate", date(2025, 8, 1),
        "Plain literal lower bound. The easy case; included so the easy case is covered.",
    ),
)

ENCOUNTER_READMISSION_ETL = Consumer(
    key="encounter_readmission_etl",
    name="encounter_readmission_etl",
    urn=datajob_urn("clinical_pipelines", "encounter_readmission_etl"),
    consumer_type=DATA_JOB,
    platform="airflow",
    subject=_PE,
    description="Daily 30-day readmission flagging job. Only looks at the recent tail.",
    sql="""SELECT e.patient_id,
       e.event_date,
       e.department,
       e.length_of_stay_days,
       lead(e.event_date) OVER (PARTITION BY e.patient_id ORDER BY e.event_date) AS next_event_date
FROM public.patient_encounters e
WHERE e.event_date >= CURRENT_DATE - INTERVAL '90 days'""",
    last_run_at=_ts(0, hour=4),
    run_count=365,
    external_url="https://airflow.example-health.org/dags/clinical_pipelines",
    expectation=Expectation(
        "sql_predicate", ANCHOR - timedelta(days=90),
        "Relative interval in days. The most common shape in real warehouses.",
    ),
)

EXECUTIVE_KPI_DASHBOARD = Consumer(
    key="executive_kpi_dashboard",
    name="Executive KPI Dashboard",
    urn=dashboard_urn("superset", "executive_kpi_dashboard"),
    consumer_type=DASHBOARD,
    platform="superset",
    subject=_PE,
    description=(
        "Board-level volume and charge trend. Reads the monthly aggregate, never the "
        "encounter grain, so it is two hops from patient_encounters and imposes no "
        "direct constraint on it."
    ),
    sql=None,  # deliberately: it does not query the subject table
    last_run_at=_ts(1, hour=8),
    run_count=310,
    degree=2,
    reads_via=ENCOUNTERS_MONTHLY_AGG.urn,
    external_url="https://superset.example-health.org/dashboard/executive-kpi",
    expectation=Expectation(
        "not_a_query_consumer", None,
        "Reachable through lineage at degree 2 but never touches patient_encounters. "
        "Must be reported and must NOT be treated as an unbounded reader -- its "
        "constraint is inherited from encounters_monthly_agg's own 18-month window.",
    ),
)

# --------------------------------------------------------------------------
# claims_history -- range analysis says fine, ACTIVE legal hold says no.
# --------------------------------------------------------------------------

_CH = CLAIMS_HISTORY

ANNUAL_AUDIT_EXTRACT = Consumer(
    key="annual_audit_extract",
    name="annual_audit_extract",
    urn=datajob_urn("compliance_pipelines", "annual_audit_extract"),
    consumer_type=DATA_JOB,
    platform="airflow",
    subject=_CH,
    description=(
        "Annual external-audit extract. Bounded, but bounded a long way back -- the "
        "audit scope opens at the 2019 plan year."
    ),
    sql="""SELECT c.claim_number,
       c.member_id,
       c.service_date,
       c.adjudication_date,
       c.claim_status,
       c.billed_amount,
       c.allowed_amount,
       c.paid_amount,
       c.provider_npi,
       c.procedure_code
FROM public.claims_history c
WHERE c.service_date >= DATE '2019-01-01'
ORDER BY c.service_date""",
    last_run_at=_ts(203, hour=1),
    run_count=7,
    external_url="https://airflow.example-health.org/dags/compliance_pipelines",
    expectation=Expectation(
        "sql_predicate", date(2019, 1, 1),
        "Bounded but deep. A cutoff at 2018-06-01 clears every consumer -- and the "
        "ACTIVE legal hold still has to veto it. Tests that policy outranks evidence.",
    ),
)

CLAIMS_DENIAL_RATE_CHART = Consumer(
    key="claims_denial_rate_chart",
    name="Claims Denial Rate by Reason",
    urn=chart_urn("superset", "claims_denial_rate_chart"),
    consumer_type=CHART,
    platform="superset",
    subject=_CH,
    description="Monthly denial counts by CARC reason. Retired when claims moved platforms.",
    sql="""SELECT date_trunc('month', c.service_date)::date AS month_start,
       c.denial_reason,
       count(*) AS denied_claims
FROM public.claims_history c
WHERE c.claim_status = 'DENIED'
  AND c.service_date BETWEEN DATE '2023-01-01' AND CURRENT_DATE
GROUP BY 1, 2""",
    last_run_at=_ts(311, hour=10),
    run_count=88,
    external_url="https://superset.example-health.org/chart/claims-denial-rate",
    expectation=Expectation(
        "sql_predicate", date(2023, 1, 1),
        "BETWEEN alongside an unrelated equality filter. The parser must pick the "
        "predicate on the date column and ignore claim_status.",
    ),
)

CLAIMS_ACTUARIAL_SNAPSHOT = Consumer(
    key="claims_actuarial_snapshot",
    name="claims_actuarial_snapshot",
    urn=dataset_urn("snowflake", "actuarial.reserving.claims_actuarial_snapshot"),
    consumer_type=DATASET,
    platform="snowflake",
    subject=_CH,
    description=(
        "Reserving snapshot maintained by the actuarial team on Snowflake. The lineage "
        "edge is declared in their dbt manifest; no query text was ever captured."
    ),
    sql=None,
    last_run_at=None,
    run_count=None,
    expectation=Expectation(
        "no_queries_observed", None,
        "Lineage exists, query text does not. Must be reported as UNKNOWN and treated "
        "as blocking, never silently assumed safe. Absence of evidence is not evidence "
        "of absence, and this is where most tiering tools quietly cheat. "
        "It lives on claims_history on purpose: claims_history's headline blocker is a "
        "POLICY blocker (ACTIVE legal hold), which the API returns in `blockers[]`, "
        "structurally separate from `verdict.consumers[]`. So the two independent "
        "reasons to refuse show up in two different places instead of competing. On "
        "lab_results the same consumer would have muddied the unbounded-scan case.",
    ),
)

# --------------------------------------------------------------------------
# care_events_live -- genuinely hot. Nothing here should ever be archived.
# --------------------------------------------------------------------------

_CE = CARE_EVENTS_LIVE

OPERATIONS_COMMAND_DASHBOARD = Consumer(
    key="operations_command_dashboard",
    name="Operations Command Centre",
    urn=dashboard_urn("superset", "operations_command_dashboard"),
    consumer_type=DASHBOARD,
    platform="superset",
    subject=_CE,
    description="Live unit-by-unit event board. Refreshes every 60 seconds.",
    sql="""SELECT v.unit,
       v.event_type,
       v.severity,
       count(*)                                   AS events,
       count(*) FILTER (WHERE NOT v.acknowledged) AS unacknowledged
FROM public.care_events_live v
WHERE v.event_date >= CURRENT_DATE - INTERVAL '7 days'
GROUP BY 1, 2, 3""",
    last_run_at=_ts(0, hour=11),
    run_count=8412,
    external_url="https://superset.example-health.org/dashboard/operations-command",
    expectation=Expectation(
        "sql_predicate", ANCHOR - timedelta(days=7),
        "Very tight window. Consumers being satisfied is not the same as the table "
        "being archivable -- care_events_live only holds 18 months in total.",
    ),
)

SEPSIS_RISK_MODEL_V4 = Consumer(
    key="sepsis_risk_model_v4",
    name="Sepsis Risk Model v4",
    urn=mlmodel_urn("mlflow", "sepsis_risk_model_v4"),
    consumer_type=MLMODEL,
    platform="mlflow",
    subject=_CE,
    description="Production early-warning model. Retrained nightly on 12 months of events.",
    sql="""SELECT v.patient_id,
       v.event_date,
       v.event_type,
       v.severity,
       v.unit,
       v.source_system
FROM public.care_events_live v
WHERE v.event_date > (NOW() - INTERVAL '12 months')::date
  AND v.severity >= 2""",
    last_run_at=_ts(0, hour=1),
    run_count=730,
    degree=2,
    lineage_via_job=datajob_urn("ml_training", "sepsis_risk_training"),
    external_url="https://mlflow.example-health.org/models/sepsis_risk_model_v4",
    expectation=Expectation(
        "sql_predicate", months_before(ANCHOR, 12),
        "Function-wrapped bound plus a numeric filter on a different column.",
    ),
)

CARE_EVENTS_HOURLY_ROLLUP = Consumer(
    key="care_events_hourly_rollup",
    name="care_events_hourly_rollup",
    urn=dataset_urn("dbt", "coldlineage.analytics.care_events_hourly_rollup"),
    consumer_type=DATASET,
    platform="dbt",
    subject=_CE,
    description="Hourly event counts by unit. Rebuilt every 30 minutes over a 30-day window.",
    sql="""SELECT date_trunc('hour', v.event_ts) AS hour_start,
       v.unit,
       v.event_type,
       count(*) AS events
FROM public.care_events_live v
WHERE v.event_date >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY 1, 2, 3""",
    last_run_at=_ts(0, hour=12),
    run_count=17520,
    expectation=Expectation(
        "sql_predicate", ANCHOR - timedelta(days=30),
        "Groups on a timestamp column but filters on the date column. The parser must "
        "read the WHERE clause, not the SELECT list.",
    ),
)

UNIT_CENSUS_CHART = Consumer(
    key="unit_census_chart",
    name="Daily Unit Census",
    urn=chart_urn("superset", "unit_census_chart"),
    consumer_type=CHART,
    platform="superset",
    subject=_CE,
    description="Distinct patients per unit per day for the current calendar year.",
    sql="""SELECT v.unit,
       v.event_date,
       count(DISTINCT v.patient_id) AS census
FROM public.care_events_live v
WHERE v.event_date BETWEEN DATE '2026-01-01' AND CURRENT_DATE
GROUP BY 1, 2""",
    last_run_at=_ts(0, hour=8),
    run_count=2140,
    external_url="https://superset.example-health.org/chart/unit-census",
    expectation=Expectation(
        "sql_predicate", date(2026, 1, 1),
        "Calendar-year-to-date BETWEEN with a literal lower bound.",
    ),
)

# --------------------------------------------------------------------------
# lab_results -- THE KILLER CASE.
#
# Table-level telemetry: 0 queries in 30 days, 0 distinct users, last access 37
# days ago. Every dataset-granularity tiering tool archives this table.
#
# It is wrong, and the reason is entirely below.
# --------------------------------------------------------------------------

_LR = LAB_RESULTS

LAB_ABNORMAL_FLAGS = Consumer(
    key="lab_abnormal_flags",
    name="lab_abnormal_flags",
    urn=dataset_urn("dbt", "coldlineage.analytics.lab_abnormal_flags"),
    consumer_type=DATASET,
    platform="dbt",
    subject=_LR,
    description=(
        "Abnormal-result surfacing model. Ran nightly on a 90-day window until it was "
        "retired in favour of in-EHR alerting."
    ),
    sql="""SELECT r.patient_id,
       r.specimen_id,
       r.collected_date,
       r.loinc_code,
       r.analyte,
       r.result_value,
       r.reference_low,
       r.reference_high,
       r.abnormal_flag
FROM public.lab_results r
WHERE r.collected_date >= CURRENT_DATE - INTERVAL '90 days'
  AND r.abnormal_flag <> 'N'""",
    last_run_at=_ts(96, hour=3),
    run_count=214,
    expectation=Expectation(
        "sql_predicate", ANCHOR - timedelta(days=90),
        "Bounded and stale. On its own this consumer would make almost the whole "
        "table archivable -- which is exactly the trap.",
    ),
)

HIPAA_LAB_DISCLOSURE_EXTRACT = Consumer(
    key="hipaa_lab_disclosure_extract",
    name="hipaa_lab_disclosure_extract",
    urn=datajob_urn("compliance_pipelines", "hipaa_lab_disclosure_extract"),
    consumer_type=DATA_JOB,
    platform="airflow",
    subject=_LR,
    description=(
        "Quarterly HIPAA accounting-of-disclosures extract. Regulators can request the "
        "full disclosure history for any patient, so this job reads every row that has "
        "ever been written -- there is no date predicate and there cannot be one."
    ),
    # THE KILLER QUERY.
    #
    # Note that it HAS a WHERE clause. A heuristic that asks "is this query filtered?"
    # says yes and approves the archive. The filter is on performing_lab. There is no
    # bound on collected_date anywhere in the statement, so this consumer reads back to
    # the first row in the table and ANY cutoff truncates a regulatory submission.
    sql="""SELECT r.result_id,
       r.patient_id,
       r.specimen_id,
       r.collected_date,
       r.resulted_at,
       r.loinc_code,
       r.analyte,
       r.result_value,
       r.result_units,
       r.abnormal_flag,
       r.performing_lab
FROM public.lab_results r
WHERE r.performing_lab IS NOT NULL
ORDER BY r.collected_date""",
    last_run_at=_ts(37, hour=5),
    run_count=27,
    external_url="https://airflow.example-health.org/dags/compliance_pipelines",
    expectation=Expectation(
        "no_date_filter", None,
        "THE KILLER. A WHERE clause with no date bound. Must derive NO_DATE_FILTER, "
        "state BLOCKED, and force DO_NOT_ARCHIVE at every cutoff -- even though the "
        "table reports zero queries and zero users over the last 30 days.",
    ),
)

# --------------------------------------------------------------------------
# billing_ledger -- consumers are shallow, the retention floor is deep.
# --------------------------------------------------------------------------

_BL = BILLING_LEDGER

FINANCE_CLOSE_DASHBOARD = Consumer(
    key="finance_close_dashboard",
    name="Monthly Finance Close",
    urn=dashboard_urn("superset", "finance_close_dashboard"),
    consumer_type=DASHBOARD,
    platform="superset",
    subject=_BL,
    description="Close-package debits and credits by cost centre for the current fiscal year.",
    sql="""SELECT b.cost_center,
       b.gl_account,
       sum(b.debit_amount)  AS debits,
       sum(b.credit_amount) AS credits
FROM public.billing_ledger b
WHERE b.posted_date >= DATE '2025-08-01'
GROUP BY 1, 2""",
    last_run_at=_ts(1, hour=6),
    run_count=620,
    external_url="https://superset.example-health.org/dashboard/finance-close",
    expectation=Expectation(
        "sql_predicate", date(2025, 8, 1),
        "Fiscal-year literal bound.",
    ),
)

REVENUE_RECOGNITION_JOB = Consumer(
    key="revenue_recognition_job",
    name="revenue_recognition_job",
    urn=datajob_urn("finance_pipelines", "revenue_recognition_job"),
    consumer_type=DATA_JOB,
    platform="airflow",
    subject=_BL,
    description="Recognises revenue on reconciled entries posted in the current year.",
    sql="""SELECT b.account_id,
       b.invoice_number,
       b.posted_date,
       b.entry_type,
       b.debit_amount - b.credit_amount AS net_amount
FROM public.billing_ledger b
WHERE b.posted_date BETWEEN DATE '2026-01-01' AND CURRENT_DATE
  AND b.reconciled IS TRUE""",
    last_run_at=_ts(1, hour=2),
    run_count=218,
    external_url="https://airflow.example-health.org/dags/finance_pipelines",
    expectation=Expectation(
        "sql_predicate", date(2026, 1, 1),
        "BETWEEN with a boolean filter alongside it.",
    ),
)

AR_AGING_CHART = Consumer(
    key="ar_aging_chart",
    name="AR Aging Buckets",
    urn=chart_urn("superset", "ar_aging_chart"),
    consumer_type=CHART,
    platform="superset",
    subject=_BL,
    description="Open balance by cost centre and age for unreconciled entries.",
    sql="""SELECT b.cost_center,
       CURRENT_DATE - b.posted_date                 AS age_days,
       sum(b.debit_amount - b.credit_amount)        AS open_balance
FROM public.billing_ledger b
WHERE b.posted_date >= CURRENT_DATE - INTERVAL '90 days'
  AND b.reconciled IS FALSE
GROUP BY 1, 2""",
    last_run_at=_ts(0, hour=7),
    run_count=903,
    external_url="https://superset.example-health.org/chart/ar-aging",
    expectation=Expectation(
        "sql_predicate", ANCHOR - timedelta(days=90),
        "Date arithmetic appears in the SELECT list as well as the WHERE clause. The "
        "SELECT-list expression is not a bound and must not be mistaken for one.",
    ),
)


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

CONSUMERS: tuple[Consumer, ...] = (
    # patient_encounters
    QUARTERLY_COMPLIANCE_DASHBOARD,
    PATIENT_LTV_MODEL,
    ENCOUNTERS_MONTHLY_AGG,
    CARE_GAP_CLOSURE_CHART,
    ENCOUNTER_READMISSION_ETL,
    EXECUTIVE_KPI_DASHBOARD,
    # claims_history
    ANNUAL_AUDIT_EXTRACT,
    CLAIMS_DENIAL_RATE_CHART,
    CLAIMS_ACTUARIAL_SNAPSHOT,
    # care_events_live
    OPERATIONS_COMMAND_DASHBOARD,
    SEPSIS_RISK_MODEL_V4,
    CARE_EVENTS_HOURLY_ROLLUP,
    UNIT_CENSUS_CHART,
    # lab_results
    LAB_ABNORMAL_FLAGS,
    HIPAA_LAB_DISCLOSURE_EXTRACT,
    # billing_ledger
    FINANCE_CLOSE_DASHBOARD,
    REVENUE_RECOGNITION_JOB,
    AR_AGING_CHART,
)

BY_KEY: dict[str, Consumer] = {c.key: c for c in CONSUMERS}


def for_table(table_key: str) -> list[Consumer]:
    return [c for c in CONSUMERS if c.subject.key == table_key]


def binding_expectation(table_key: str) -> Consumer | None:
    """The consumer expected to constrain a cutoff hardest for this table.

    Unbounded consumers win outright; otherwise the earliest bounded read wins.
    Used by smoke_test.py to check the backend picked the same binding constraint.
    """
    cands = [c for c in for_table(table_key)
             if c.expectation.derivation in ("no_date_filter", "no_queries_observed")]
    if cands:
        return cands[0]
    bounded = [c for c in for_table(table_key) if c.expectation.earliest_date_read]
    if not bounded:
        return None
    return min(bounded, key=lambda c: c.expectation.earliest_date_read)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Self-check: every SQL statement here must be genuine, parseable PostgreSQL,
# and the date-bearing ones must actually mention their subject's date column.
# Run:  python scripts/consumers.py
# --------------------------------------------------------------------------


def _validate() -> int:
    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:
        print("sqlglot is not installed; cannot validate. pip install -r "
              "scripts/requirements-seed.txt")
        return 2

    failures = 0
    rows: list[tuple[str, ...]] = []
    for c in CONSUMERS:
        if c.sql is None:
            rows.append((c.key, c.consumer_type, "-", "-", c.expectation.derivation, "no sql"))
            continue
        try:
            tree = sqlglot.parse_one(c.sql, dialect="postgres")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            rows.append((c.key, c.consumer_type, "PARSE FAIL", "-",
                         c.expectation.derivation, str(exc)[:60]))
            continue

        tables = {t.name for t in tree.find_all(exp.Table)}
        if c.subject.key not in tables:
            failures += 1
            note = f"does not reference {c.subject.key}"
        else:
            note = "ok"

        where = tree.find(exp.Where)
        mentions_date_col = False
        if where is not None:
            mentions_date_col = any(
                col.name == c.subject.date_column for col in where.find_all(exp.Column)
            )
        expect_bound = c.expectation.derivation == "sql_predicate"
        if expect_bound and not mentions_date_col:
            failures += 1
            note = f"expected a bound on {c.subject.date_column}, WHERE does not mention it"
        if not expect_bound and mentions_date_col and c.expectation.derivation == "no_date_filter":
            failures += 1
            note = "expected NO date predicate but WHERE mentions the date column"

        rows.append((
            c.key, c.consumer_type, "parsed",
            "yes" if mentions_date_col else "no",
            c.expectation.derivation, note,
        ))

    hdr = ("consumer", "type", "parse", "date_in_where", "expected_derivation", "note")
    widths = [max(len(str(r[i])) for r in (*rows, hdr)) for i in range(len(hdr))]
    line = "  ".join(h.ljust(w) for h, w in zip(hdr, widths))
    print(line)
    print("-" * len(line))
    for r in rows:
        print("  ".join(str(v).ljust(w) for v, w in zip(r, widths)))

    print()
    print(f"anchor date          : {ANCHOR.isoformat()}")
    print(f"consumers            : {len(CONSUMERS)}")
    by_type: dict[str, int] = {}
    for c in CONSUMERS:
        by_type[c.consumer_type] = by_type.get(c.consumer_type, 0) + 1
    print(f"by type              : {by_type}")
    by_deriv: dict[str, int] = {}
    for c in CONSUMERS:
        by_deriv[c.expectation.derivation] = by_deriv.get(c.expectation.derivation, 0) + 1
    print(f"expected derivations : {by_deriv}")
    print()
    for tkey in (PATIENT_ENCOUNTERS.key, CLAIMS_HISTORY.key, CARE_EVENTS_LIVE.key,
                 LAB_RESULTS.key, BILLING_LEDGER.key):
        b = binding_expectation(tkey)
        if b is None:
            print(f"{tkey:<20} binding constraint: none")
        else:
            print(f"{tkey:<20} binding constraint: {b.key} "
                  f"({b.expectation.derivation}, earliest={b.expectation.earliest_date_read})")

    if failures:
        print(f"\nFAILED: {failures} problem(s).")
    else:
        print("\nAll consumer SQL parses as PostgreSQL and matches its expectation.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_validate())
