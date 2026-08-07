"""Declarative definition of the ColdLineage demo estate.

This module is the single source of truth for the FIVE demo tables: their real
Postgres DDL, the date column each one is ranged on, how much data to generate,
the DataHub governance context (owners / domain / tags / glossary terms), the
`io.coldlineage.policy.*` structured-property values, and the usage telemetry
profile each table is meant to exhibit.

`scripts/seed_warehouse.py` creates the tables from here.
`scripts/ingest_datahub.py` pushes the catalog context from here.
`scripts/consumers.py` attaches downstream consumers to the table keys defined here.

HONESTY NOTE -- read this before you cite any number produced by these scripts.
    Everything in this module is an intentionally SYNTHETIC demo estate. It exists
    so that ColdLineage has something real to measure. What must stay true is the
    direction of the arrow:

      * The estate is synthetic, and every entity emitted to DataHub carries
        `coldlineage.synthetic = "true"` in its customProperties so nobody can
        mistake it for a production catalog.
      * The MEASUREMENTS are not synthetic. Row counts, byte sizes and min/max
        dates are read back out of Postgres with pg_total_relation_size() and
        real aggregates. Nothing here declares a size; sizes are measured.
      * The product never reads this module. ColdLineage reads DataHub. This
        module only populates DataHub.

THE FIVE CASES, and why each one exists
---------------------------------------
patient_encounters  HERO. A table that is *warm at the table level* -- dashboards
                    query it daily -- but whose 2019-2022 history is dead. DataHub
                    can only say "this table is used". ColdLineage says "the first
                    four years of it are not". Every downstream consumer has a
                    bounded date predicate, so the range is provably safe.

claims_history      Cold by every telemetry signal AND under an ACTIVE legal hold
                    declared in DataHub as a structured property. Range analysis
                    would happily approve a cutoff; policy vetoes it
                    unconditionally. Tests that policy outranks evidence.

care_events_live    Genuinely hot. Correctly kept. The control case -- without it,
                    a tool that recommends archiving everything looks smart.

lab_results         THE KILLER CASE. Table-level telemetry is as cold as it gets:
                    zero queries in 30 days, zero distinct users, last access over
                    a month ago. Any tool that reasons at dataset granularity
                    archives this table. It would be WRONG: the quarterly HIPAA
                    disclosure extract issues a full-table scan with no date
                    predicate, so it reads back to 2019 and *any* cutoff truncates
                    it. Only consumer-level SQL analysis catches this.

billing_ledger      Archivable, but carries a 7-year retention floor as a DataHub
                    structured property. An aggressive cutoff is illegal; a
                    conservative one is fine. Proves the cutoff itself is the
                    decision variable, which is the thing dataset-level tiering
                    cannot express.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, timedelta

# --------------------------------------------------------------------------
# Anchor date
# --------------------------------------------------------------------------
# Relative predicates in consumers.py ("CURRENT_DATE - INTERVAL '90 days'") only
# mean anything against a date. The estate is generated relative to the anchor so
# that a freshly seeded warehouse always has data right up to "today". Override
# with COLDLINEAGE_ANCHOR_DATE=YYYY-MM-DD to get a byte-reproducible estate.

_ANCHOR_ENV = os.environ.get("COLDLINEAGE_ANCHOR_DATE", "").strip()
ANCHOR: date = date.fromisoformat(_ANCHOR_ENV) if _ANCHOR_ENV else date.today()

PG_SCHEMA = "public"
PG_DATABASE = "coldlineage"
DATAHUB_ENV = "PROD"
PLATFORM_POSTGRES = "postgres"


def months_before(anchor: date, months: int) -> date:
    """Calendar-month arithmetic without pulling in dateutil."""
    total = (anchor.year * 12 + (anchor.month - 1)) - months
    y, m = divmod(total, 12)
    day = min(anchor.day, [31, 29 if y % 4 == 0 and (y % 100 != 0 or y % 400 == 0) else 28,
                           31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m])
    return date(y, m + 1, day)


def month_start(d: date) -> date:
    return d.replace(day=1)


# --------------------------------------------------------------------------
# Column / table specs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Column:
    name: str
    pg_type: str
    nullable: bool = True
    description: str = ""
    # Value generator kind, consumed by seed_warehouse.py.
    gen: str = "null"
    gen_args: tuple = ()
    tags: tuple[str, ...] = ()
    terms: tuple[str, ...] = ()

    @property
    def ddl(self) -> str:
        null = "" if self.nullable else " NOT NULL"
        return f"    {self.name} {self.pg_type}{null}"


@dataclass(frozen=True)
class UsageProfile:
    """The usage telemetry this table should exhibit in DataHub.

    Emitted as datasetUsageStatistics + operation aspects by ingest_datahub.py.
    `last_query_days_ago` is measured back from ANCHOR so the estate never goes
    stale. These numbers are deliberately consistent with the consumer run
    schedules in consumers.py -- e.g. lab_results reports 0 queries in 30 days
    because both of its consumers last ran more than 30 days ago.
    """

    last_query_days_ago: int
    query_count_30d: int
    distinct_users_30d: int
    top_user_keys: tuple[str, ...] = ()

    def last_query_date(self, anchor: date = ANCHOR) -> date:
        return anchor - timedelta(days=self.last_query_days_ago)


@dataclass(frozen=True)
class PolicyProperties:
    """Values for io.coldlineage.policy.* structured properties.

    Ids and allowed values come from backend/app/datahub/properties.yaml.
    ColdLineage READS these out of DataHub; it never invents them. Seeding them
    here is how a governance owner's declaration gets into the catalog.
    """

    retention_years: float
    legal_hold: str  # NONE | ACTIVE | RELEASED  (allowed_values in properties.yaml)
    business_criticality: float
    legal_hold_matter: str | None = None


@dataclass(frozen=True)
class TableSpec:
    key: str
    description: str
    columns: tuple[Column, ...]
    date_column: str
    rows: int
    start: date
    end: date
    growth: float  # 0.0 = uniform over time; higher = more recent rows
    domain: str
    owners: tuple[tuple[str, str], ...]  # (username, ownership type)
    tags: tuple[str, ...]
    terms: tuple[str, ...]
    policy: PolicyProperties
    usage: UsageProfile
    sensitive: bool
    demo_role: str  # one-line explanation of why this table is in the estate
    indexes: tuple[tuple[str, tuple[str, ...]], ...] = ()
    pk: str = "id"
    pk_type: str = "BIGSERIAL"

    # ---- naming -------------------------------------------------------

    @property
    def table(self) -> str:
        return self.key

    @property
    def qualified(self) -> str:
        """What the executor actually touches."""
        return f'"{PG_SCHEMA}"."{self.key}"'

    @property
    def dotted(self) -> str:
        return f"{PG_SCHEMA}.{self.key}"

    @property
    def datahub_name(self) -> str:
        """Must match exactly what the first-party Postgres connector produces.

        The DataHub postgres source builds dataset names as
        `<database>.<schema>.<table>`. Hand-emitted aspects have to land on the
        SAME urn or the catalog ends up with two half-populated entities.
        """
        return f"{PG_DATABASE}.{PG_SCHEMA}.{self.key}"

    @property
    def urn(self) -> str:
        return (
            f"urn:li:dataset:(urn:li:dataPlatform:{PLATFORM_POSTGRES},"
            f"{self.datahub_name},{DATAHUB_ENV})"
        )

    # ---- DDL ----------------------------------------------------------

    @property
    def create_sql(self) -> str:
        cols = [f"    {self.pk} {self.pk_type} PRIMARY KEY"]
        cols += [c.ddl for c in self.columns]
        return f'CREATE TABLE "{PG_SCHEMA}"."{self.key}" (\n' + ",\n".join(cols) + "\n)"

    @property
    def index_sql(self) -> list[str]:
        out = []
        for name, cols in self.indexes:
            collist = ", ".join(cols)
            out.append(
                f'CREATE INDEX IF NOT EXISTS {name} ON "{PG_SCHEMA}"."{self.key}" ({collist})'
            )
        return out

    @property
    def copy_columns(self) -> list[str]:
        return [c.name for c in self.columns]


# --------------------------------------------------------------------------
# Shared value pools for the generator
# --------------------------------------------------------------------------

DEPARTMENTS = ("cardiology", "oncology", "orthopedics", "primary_care",
               "emergency", "neurology", "obstetrics", "endocrinology")
ENCOUNTER_TYPES = ("inpatient", "outpatient", "telehealth", "emergency", "preventive")
DIAGNOSIS_CODES = ("I10", "E11.9", "J45.20", "M54.50", "Z00.00", "N18.3",
                   "F32.9", "K21.9", "I48.91", "J44.1", "E78.5", "R07.9")
PAYERS = ("MEDICARE", "MEDICAID", "BCBS", "AETNA", "UNITED", "CIGNA", "SELF_PAY")
REGIONS = ("WEST", "EAST", "MIDWEST", "SOUTH", "NORTHEAST")
CLAIM_STATUSES = ("PAID", "DENIED", "PENDING", "ADJUSTED", "REVERSED")
DENIAL_REASONS = ("CO-16 missing information", "CO-97 bundled service",
                  "PR-1 deductible", "CO-45 exceeds fee schedule",
                  "CO-29 timely filing", "")
PROCEDURE_CODES = ("99213", "99214", "93000", "80053", "36415", "71046",
                   "45378", "29881", "70450", "85025")
CARE_EVENT_TYPES = ("vitals_alert", "med_administration", "fall_risk", "sepsis_screen",
                    "discharge_ready", "consult_requested", "code_blue", "rounding_note")
CARE_TEAMS = ("team_alpha", "team_bravo", "team_charlie", "team_delta", "rapid_response")
UNITS = ("ICU", "MED_SURG", "TELEMETRY", "ED", "PROGRESSIVE", "PEDS", "L_AND_D")
SOURCE_SYSTEMS = ("epic_clarity", "cerner_millennium", "bedside_monitor", "nurse_mobile")
LOINC_CODES = ("2345-7", "718-7", "4548-4", "2160-0", "1975-2", "6690-2",
               "2951-2", "2823-3", "33914-3", "13457-7", "2093-3", "1742-6")
ANALYTES = ("glucose", "hemoglobin", "hba1c", "creatinine", "bilirubin", "wbc",
            "sodium", "potassium", "egfr", "ldl_calc", "cholesterol", "alt")
RESULT_UNITS = ("mg/dL", "g/dL", "%", "mg/dL", "mg/dL", "10*3/uL",
                "mmol/L", "mmol/L", "mL/min/1.73m2", "mg/dL", "mg/dL", "U/L")
ABNORMAL_FLAGS = ("N", "N", "N", "N", "N", "H", "L", "HH", "LL", "A")
PERFORMING_LABS = ("CENTRAL_LAB", "QUEST_REF", "LABCORP_REF", "POINT_OF_CARE", "MICRO_LAB")
GL_ACCOUNTS = ("4000-patient-revenue", "4100-contractual-allowance", "5000-supplies",
               "5100-salaries", "5200-benefits", "6000-depreciation",
               "1200-accounts-receivable", "2000-accounts-payable")
ENTRY_TYPES = ("CHARGE", "PAYMENT", "ADJUSTMENT", "REFUND", "WRITE_OFF", "ACCRUAL")
COST_CENTERS = ("CC-100-INPATIENT", "CC-200-OUTPATIENT", "CC-300-LAB",
                "CC-400-IMAGING", "CC-500-PHARMACY", "CC-600-ADMIN")


# --------------------------------------------------------------------------
# The five tables
# --------------------------------------------------------------------------

PATIENT_ENCOUNTERS = TableSpec(
    key="patient_encounters",
    description=(
        "Completed clinical encounters across all facilities, one row per encounter. "
        "Written continuously since 2019. Recent history drives operational reporting; "
        "the pre-2023 tail is retained only because nothing ever removed it."
    ),
    demo_role=(
        "HERO CASE -- warm table, dead history. Table-level usage says 'in use'; "
        "range-level analysis says the first four years are archivable."
    ),
    columns=(
        Column("patient_id", "TEXT", False, "Pseudonymous patient identifier.",
               gen="patient", tags=("PII", "PHI"), terms=("PatientIdentifier",)),
        Column("event_date", "DATE", False, "Date the encounter occurred. Range key.",
               gen="date", terms=("ClinicalObservation",)),
        Column("encounter_type", "TEXT", True, "Care setting for the encounter.",
               gen="choice", gen_args=(ENCOUNTER_TYPES,)),
        Column("department", "TEXT", True, "Owning clinical department.",
               gen="choice", gen_args=(DEPARTMENTS,)),
        Column("attending_provider_id", "TEXT", True, "NPI of the attending provider.",
               gen="npi"),
        Column("primary_diagnosis_code", "TEXT", True, "ICD-10-CM principal diagnosis.",
               gen="choice", gen_args=(DIAGNOSIS_CODES,), tags=("PHI",),
               terms=("ClinicalObservation",)),
        Column("length_of_stay_days", "INTEGER", True, "Inpatient length of stay; 0 for ambulatory.",
               gen="int", gen_args=(0, 21)),
        Column("total_charges", "NUMERIC(12,2)", True, "Gross charges posted for the encounter.",
               gen="money", gen_args=(80, 94_000), terms=("FinancialRecord",)),
        Column("payer", "TEXT", True, "Primary payer at time of service.",
               gen="choice", gen_args=(PAYERS,)),
        Column("region", "TEXT", True, "Facility region.", gen="choice", gen_args=(REGIONS,)),
        Column("created_at", "TIMESTAMPTZ", True, "Row insert time in the source system.",
               gen="ts_from_date"),
    ),
    date_column="event_date",
    rows=1_100_000,
    start=date(2019, 1, 1),
    end=ANCHOR,
    growth=0.6,
    domain="Clinical Analytics",
    owners=(("maya.chen", "BUSINESS_OWNER"), ("data-platform", "TECHNICAL_OWNER")),
    tags=("ColdLineageDemoEstate", "PHI", "PII", "HIPAA", "Tier2"),
    terms=("ProtectedHealthInformation", "ClinicalObservation"),
    policy=PolicyProperties(retention_years=2.0, legal_hold="NONE", business_criticality=0.35),
    usage=UsageProfile(
        last_query_days_ago=1,
        query_count_30d=68,
        distinct_users_30d=6,
        top_user_keys=("maya.chen", "analytics-svc", "priya.shah"),
    ),
    sensitive=True,
    indexes=(("ix_patient_encounters_event_date", ("event_date",)),),
    pk="encounter_id",
)

CLAIMS_HISTORY = TableSpec(
    key="claims_history",
    description=(
        "Adjudicated payer claims from the legacy billing platform. Writes stopped at "
        "the end of 2024 when claims moved to the new clearinghouse. Retained under an "
        "active litigation hold."
    ),
    demo_role=(
        "POLICY VETO -- cold by every telemetry signal, and range analysis would approve "
        "a cutoff, but an ACTIVE legal hold declared in DataHub blocks it unconditionally."
    ),
    columns=(
        Column("claim_number", "TEXT", False, "Payer-assigned claim control number.",
               gen="claimno"),
        Column("member_id", "TEXT", False, "Pseudonymous member identifier.",
               gen="patient", tags=("PII", "PHI"), terms=("PatientIdentifier",)),
        Column("service_date", "DATE", False, "Date of service. Range key.", gen="date"),
        Column("adjudication_date", "DATE", True, "Date the payer adjudicated the claim.",
               gen="date_offset", gen_args=(3, 75)),
        Column("claim_status", "TEXT", True, "Terminal adjudication status.",
               gen="choice", gen_args=(CLAIM_STATUSES,)),
        Column("billed_amount", "NUMERIC(12,2)", True, "Amount billed to the payer.",
               gen="money", gen_args=(45, 62_000), terms=("FinancialRecord",)),
        Column("allowed_amount", "NUMERIC(12,2)", True, "Payer-allowed amount.",
               gen="money", gen_args=(20, 48_000), terms=("FinancialRecord",)),
        Column("paid_amount", "NUMERIC(12,2)", True, "Amount actually paid.",
               gen="money", gen_args=(0, 44_000), terms=("FinancialRecord",)),
        Column("provider_npi", "TEXT", True, "Rendering provider NPI.", gen="npi"),
        Column("procedure_code", "TEXT", True, "CPT/HCPCS procedure code.",
               gen="choice", gen_args=(PROCEDURE_CODES,)),
        Column("denial_reason", "TEXT", True, "CARC denial reason, blank when paid.",
               gen="choice", gen_args=(DENIAL_REASONS,)),
    ),
    date_column="service_date",
    rows=620_000,
    start=date(2018, 1, 1),
    end=date(2024, 12, 31),
    growth=0.2,
    domain="Finance & Claims",
    owners=(("evan.brooks", "BUSINESS_OWNER"), ("data-platform", "TECHNICAL_OWNER")),
    tags=("ColdLineageDemoEstate", "PHI", "PII", "HIPAA", "LegalHold", "Tier1"),
    terms=("ProtectedHealthInformation", "FinancialRecord"),
    policy=PolicyProperties(
        retention_years=7.0,
        legal_hold="ACTIVE",
        legal_hold_matter="MDL-2291 -- In re Regional Payer Billing Practices (D. Mass.)",
        business_criticality=0.80,
    ),
    usage=UsageProfile(
        last_query_days_ago=203,
        query_count_30d=0,
        distinct_users_30d=0,
        top_user_keys=(),
    ),
    sensitive=True,
    indexes=(("ix_claims_history_service_date", ("service_date",)),),
    pk="claim_id",
)

CARE_EVENTS_LIVE = TableSpec(
    key="care_events_live",
    description=(
        "Real-time clinical event stream landed from bedside monitors and the EHR. "
        "Backs the operations command centre and the sepsis risk model."
    ),
    demo_role=(
        "CONTROL CASE -- genuinely hot. Correctly kept. Without a table that "
        "ColdLineage refuses to touch, a tool that archives everything looks clever."
    ),
    columns=(
        Column("patient_id", "TEXT", False, "Pseudonymous patient identifier.",
               gen="patient", tags=("PII", "PHI"), terms=("PatientIdentifier",)),
        Column("event_ts", "TIMESTAMPTZ", False, "Event timestamp to the second.",
               gen="ts_from_date"),
        Column("event_date", "DATE", False, "Event date, partition/range key.", gen="date"),
        Column("event_type", "TEXT", True, "Clinical event category.",
               gen="choice", gen_args=(CARE_EVENT_TYPES,), terms=("ClinicalObservation",)),
        Column("care_team", "TEXT", True, "Assigned care team.",
               gen="choice", gen_args=(CARE_TEAMS,)),
        Column("severity", "SMALLINT", True, "Triage severity 0-4, higher is worse.",
               gen="int", gen_args=(0, 4)),
        Column("acknowledged", "BOOLEAN", True, "Whether a clinician acknowledged the event.",
               gen="bool", gen_args=(0.82,)),
        Column("unit", "TEXT", True, "Inpatient unit.", gen="choice", gen_args=(UNITS,)),
        Column("source_system", "TEXT", True, "Originating system.",
               gen="choice", gen_args=(SOURCE_SYSTEMS,)),
    ),
    date_column="event_date",
    rows=780_000,
    start=ANCHOR - timedelta(days=545),
    end=ANCHOR,
    growth=0.35,
    domain="Clinical Operations",
    owners=(("priya.shah", "BUSINESS_OWNER"), ("data-platform", "TECHNICAL_OWNER")),
    tags=("ColdLineageDemoEstate", "PHI", "PII", "HIPAA", "Tier1"),
    terms=("ProtectedHealthInformation", "ClinicalObservation"),
    policy=PolicyProperties(retention_years=2.0, legal_hold="NONE", business_criticality=0.95),
    usage=UsageProfile(
        last_query_days_ago=0,
        query_count_30d=41_820,
        distinct_users_30d=44,
        top_user_keys=("ops-dashboard-svc", "priya.shah", "ml-platform-svc", "dana.okafor"),
    ),
    sensitive=True,
    indexes=(("ix_care_events_live_event_date", ("event_date",)),),
    pk="event_id",
)

LAB_RESULTS = TableSpec(
    key="lab_results",
    description=(
        "Resulted laboratory observations, one row per analyte per specimen. Feeds a "
        "retired abnormal-flag pipeline and the quarterly HIPAA disclosure extract."
    ),
    demo_role=(
        "KILLER CASE -- zero queries in 30 days and zero distinct users, so every "
        "dataset-level tiering tool archives it. WRONG: the quarterly HIPAA disclosure "
        "extract is an unbounded full-table scan reaching back to 2019, so no cutoff "
        "is safe. Only consumer-level SQL analysis can see this."
    ),
    columns=(
        Column("patient_id", "TEXT", False, "Pseudonymous patient identifier.",
               gen="patient", tags=("PII", "PHI"), terms=("PatientIdentifier",)),
        Column("specimen_id", "TEXT", False, "Accession number of the specimen.",
               gen="specimen"),
        Column("collected_date", "DATE", False, "Specimen collection date. Range key.",
               gen="date"),
        Column("resulted_at", "TIMESTAMPTZ", True, "Time the result was released.",
               gen="ts_from_date"),
        Column("loinc_code", "TEXT", True, "LOINC code for the observation.",
               gen="paired_choice", gen_args=(LOINC_CODES, 0)),
        Column("analyte", "TEXT", True, "Human-readable analyte name.",
               gen="paired_choice", gen_args=(ANALYTES, 0), terms=("ClinicalObservation",)),
        Column("result_value", "NUMERIC(12,3)", True, "Numeric result value.",
               gen="money", gen_args=(0, 480)),
        Column("result_units", "TEXT", True, "Units of measure.",
               gen="paired_choice", gen_args=(RESULT_UNITS, 0)),
        Column("reference_low", "NUMERIC(12,3)", True, "Lower reference bound.",
               gen="money", gen_args=(0, 60)),
        Column("reference_high", "NUMERIC(12,3)", True, "Upper reference bound.",
               gen="money", gen_args=(60, 500)),
        Column("abnormal_flag", "TEXT", True, "HL7 abnormal flag; N is normal.",
               gen="choice", gen_args=(ABNORMAL_FLAGS,)),
        Column("performing_lab", "TEXT", True, "Lab that performed the assay.",
               gen="choice", gen_args=(PERFORMING_LABS,)),
    ),
    date_column="collected_date",
    rows=700_000,
    start=date(2019, 3, 1),
    end=ANCHOR - timedelta(days=5),
    growth=0.3,
    domain="Clinical Diagnostics",
    owners=(("dana.okafor", "BUSINESS_OWNER"), ("data-platform", "TECHNICAL_OWNER")),
    tags=("ColdLineageDemoEstate", "PHI", "PII", "HIPAA", "RegulatoryReporting", "Tier3"),
    terms=("ProtectedHealthInformation", "ClinicalObservation"),
    policy=PolicyProperties(retention_years=3.0, legal_hold="NONE", business_criticality=0.30),
    # Consistent with consumers.py: lab_abnormal_flags last ran 96 days ago and
    # hipaa_lab_disclosure_extract last ran 37 days ago, so a 30-day window is empty.
    usage=UsageProfile(
        last_query_days_ago=37,
        query_count_30d=0,
        distinct_users_30d=0,
        top_user_keys=(),
    ),
    sensitive=True,
    indexes=(("ix_lab_results_collected_date", ("collected_date",)),),
    pk="result_id",
)

BILLING_LEDGER = TableSpec(
    key="billing_ledger",
    description=(
        "General-ledger postings for patient revenue and cost centres. Source of record "
        "for the finance close and AR aging."
    ),
    demo_role=(
        "RETENTION FLOOR -- consumers only need 90 days, so range analysis would allow a "
        "2022 cutoff, but the 7-year retention floor declared in DataHub makes anything "
        "newer than the floor illegal. Shows the cutoff itself is the decision variable."
    ),
    columns=(
        Column("account_id", "TEXT", False, "Patient account identifier.", gen="account"),
        Column("posted_date", "DATE", False, "GL posting date. Range key.", gen="date"),
        Column("gl_account", "TEXT", True, "General-ledger account.",
               gen="choice", gen_args=(GL_ACCOUNTS,), terms=("FinancialRecord",)),
        Column("entry_type", "TEXT", True, "Ledger entry classification.",
               gen="choice", gen_args=(ENTRY_TYPES,)),
        Column("debit_amount", "NUMERIC(14,2)", True, "Debit side of the entry.",
               gen="money", gen_args=(0, 38_000), terms=("FinancialRecord",)),
        Column("credit_amount", "NUMERIC(14,2)", True, "Credit side of the entry.",
               gen="money", gen_args=(0, 38_000), terms=("FinancialRecord",)),
        Column("currency", "TEXT", True, "ISO 4217 currency code.",
               gen="choice", gen_args=(("USD",),)),
        Column("cost_center", "TEXT", True, "Owning cost centre.",
               gen="choice", gen_args=(COST_CENTERS,)),
        Column("invoice_number", "TEXT", True, "Invoice the entry belongs to.", gen="invoice"),
        Column("reconciled", "BOOLEAN", True, "Whether the entry has been reconciled.",
               gen="bool", gen_args=(0.71,)),
    ),
    date_column="posted_date",
    rows=450_000,
    start=date(2016, 1, 1),
    end=ANCHOR,
    growth=0.4,
    domain="Revenue Cycle",
    owners=(("sam.ruiz", "BUSINESS_OWNER"), ("data-platform", "TECHNICAL_OWNER")),
    tags=("ColdLineageDemoEstate", "SOX", "Financial", "Tier2"),
    terms=("FinancialRecord",),
    policy=PolicyProperties(retention_years=7.0, legal_hold="NONE", business_criticality=0.55),
    usage=UsageProfile(
        last_query_days_ago=1,
        query_count_30d=310,
        distinct_users_30d=9,
        top_user_keys=("sam.ruiz", "finance-svc", "evan.brooks"),
    ),
    sensitive=False,
    indexes=(("ix_billing_ledger_posted_date", ("posted_date",)),),
    pk="entry_id",
)

TABLES: tuple[TableSpec, ...] = (
    PATIENT_ENCOUNTERS,
    CLAIMS_HISTORY,
    CARE_EVENTS_LIVE,
    LAB_RESULTS,
    BILLING_LEDGER,
)

BY_KEY: dict[str, TableSpec] = {t.key: t for t in TABLES}


# --------------------------------------------------------------------------
# Supporting catalog entities
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Person:
    username: str
    display_name: str
    email: str
    title: str


PEOPLE: tuple[Person, ...] = (
    Person("maya.chen", "Maya Chen", "maya.chen@example-health.org",
           "Director, Clinical Analytics"),
    Person("evan.brooks", "Evan Brooks", "evan.brooks@example-health.org",
           "Manager, Claims Data"),
    Person("priya.shah", "Priya Shah", "priya.shah@example-health.org",
           "Lead, Clinical Operations Data"),
    Person("dana.okafor", "Dana Okafor", "dana.okafor@example-health.org",
           "Manager, Laboratory Informatics"),
    Person("sam.ruiz", "Sam Ruiz", "sam.ruiz@example-health.org",
           "Controller, Revenue Cycle"),
    Person("data-platform", "Data Platform Team", "data-platform@example-health.org",
           "Platform Engineering"),
    Person("analytics-svc", "Analytics Service Account", "analytics-svc@example-health.org",
           "Service Account"),
    Person("ops-dashboard-svc", "Ops Dashboard Service Account",
           "ops-dashboard-svc@example-health.org", "Service Account"),
    Person("ml-platform-svc", "ML Platform Service Account",
           "ml-platform-svc@example-health.org", "Service Account"),
    Person("finance-svc", "Finance Service Account", "finance-svc@example-health.org",
           "Service Account"),
    Person("compliance-svc", "Compliance Extract Service Account",
           "compliance-svc@example-health.org", "Service Account"),
)

PEOPLE_BY_KEY: dict[str, Person] = {p.username: p for p in PEOPLE}


@dataclass(frozen=True)
class TagDef:
    name: str
    description: str
    color_hex: str


TAGS: tuple[TagDef, ...] = (
    # Applied to every table in the estate, by BOTH the native Postgres recipe
    # (via its simple_add_dataset_tags transformer) and by ingest_datahub.py.
    # Both write the globalTags aspect wholesale, so whichever runs last would
    # otherwise erase the other's tags -- carrying it in both places means the
    # "this is not a production catalog" marker survives either ordering.
    TagDef("ColdLineageDemoEstate",
           "Synthetic demo estate created by scripts/seed_warehouse.py. Not production data.",
           "#4A4A4A"),
    TagDef("PHI", "Protected Health Information under HIPAA. Movement is auditable.", "#B3261E"),
    TagDef("PII", "Personally identifiable information.", "#D2691E"),
    TagDef("HIPAA", "In scope for HIPAA Security Rule controls.", "#7B2D8B"),
    TagDef("SOX", "In scope for Sarbanes-Oxley financial controls.", "#1F5C8B"),
    TagDef("Financial", "Financial record used in statutory reporting.", "#2E7D32"),
    TagDef("RegulatoryReporting", "Feeds a mandated regulatory submission.", "#8B5E00"),
    TagDef("LegalHold", "Subject to an active or historical litigation hold.", "#8B0000"),
    TagDef("Tier1", "Business critical. Outage is immediately felt.", "#B3261E"),
    TagDef("Tier2", "Important. Degradation is tolerable for hours.", "#8B5E00"),
    TagDef("Tier3", "Low criticality. Batch or periodic use only.", "#4A4A4A"),
)


@dataclass(frozen=True)
class TermDef:
    name: str
    definition: str


GLOSSARY_TERMS: tuple[TermDef, ...] = (
    TermDef(
        "ProtectedHealthInformation",
        "Individually identifiable health information held or transmitted by a covered "
        "entity, as defined by 45 CFR 160.103. Archival of PHI must preserve integrity "
        "and remain restorable for the full retention period.",
    ),
    TermDef(
        "PatientIdentifier",
        "A direct or pseudonymous identifier that resolves to a single patient. Treated "
        "as PHI for access-control and archival purposes.",
    ),
    TermDef(
        "ClinicalObservation",
        "A recorded fact about a patient's clinical state, encounter or specimen. "
        "Carries a clinically meaningful event date, which is what makes range-level "
        "archival possible at all.",
    ),
    TermDef(
        "FinancialRecord",
        "A monetary amount that rolls up into a reported financial statement. Subject to "
        "the statutory retention floor rather than to usage-based tiering.",
    ),
)


@dataclass(frozen=True)
class DomainDef:
    name: str
    description: str

    @property
    def id(self) -> str:
        return self.name.lower().replace(" & ", "-").replace(" ", "-")

    @property
    def urn(self) -> str:
        return f"urn:li:domain:{self.id}"


DOMAINS: tuple[DomainDef, ...] = (
    DomainDef("Clinical Analytics",
              "Retrospective analysis of completed clinical activity."),
    DomainDef("Finance & Claims",
              "Payer claims, adjudication and reimbursement data."),
    DomainDef("Clinical Operations",
              "Real-time operational data supporting active patient care."),
    DomainDef("Clinical Diagnostics",
              "Laboratory, pathology and imaging observations."),
    DomainDef("Revenue Cycle",
              "Charge capture, general ledger and accounts receivable."),
)

DOMAIN_BY_NAME: dict[str, DomainDef] = {d.name: d for d in DOMAINS}


# --------------------------------------------------------------------------
# Structured property urns (definitions live in backend/app/datahub/properties.yaml)
# --------------------------------------------------------------------------

PROPERTY_NAMESPACE = "io.coldlineage"


def property_urn(qualified_name: str) -> str:
    return f"urn:li:structuredProperty:{qualified_name}"


POLICY_RETENTION_YEARS = f"{PROPERTY_NAMESPACE}.policy.retentionYears"
POLICY_LEGAL_HOLD = f"{PROPERTY_NAMESPACE}.policy.legalHold"
POLICY_LEGAL_HOLD_MATTER = f"{PROPERTY_NAMESPACE}.policy.legalHoldMatter"
POLICY_BUSINESS_CRITICALITY = f"{PROPERTY_NAMESPACE}.policy.businessCriticality"


def corpuser_urn(username: str) -> str:
    return f"urn:li:corpuser:{username}"


def tag_urn(name: str) -> str:
    return f"urn:li:tag:{name}"


def term_urn(name: str) -> str:
    return f"urn:li:glossaryTerm:{name}"


__all__ = [
    "ANCHOR", "PG_SCHEMA", "PG_DATABASE", "DATAHUB_ENV", "PLATFORM_POSTGRES",
    "Column", "TableSpec", "UsageProfile", "PolicyProperties",
    "TABLES", "BY_KEY", "PATIENT_ENCOUNTERS", "CLAIMS_HISTORY", "CARE_EVENTS_LIVE",
    "LAB_RESULTS", "BILLING_LEDGER",
    "PEOPLE", "PEOPLE_BY_KEY", "TAGS", "GLOSSARY_TERMS", "DOMAINS", "DOMAIN_BY_NAME",
    "Person", "TagDef", "TermDef", "DomainDef",
    "property_urn", "corpuser_urn", "tag_urn", "term_urn",
    "POLICY_RETENTION_YEARS", "POLICY_LEGAL_HOLD", "POLICY_LEGAL_HOLD_MATTER",
    "POLICY_BUSINESS_CRITICALITY",
    "months_before", "month_start",
]
