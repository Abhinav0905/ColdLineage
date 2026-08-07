# Consumer history windows

**This is the differentiator, on one page.**

DataHub knows *that* `patient_encounters` has seven downstream consumers. It does not
know that the furthest back any of them reads is 2024-01-01, which is the only fact that
decides whether 2019-2022 can be moved to cold storage while the table stays live.

Every row below was produced by reading the consumer's real SQL out of DataHub as a
`Query` entity, parsing it with sqlglot, and resolving the lower bound it places on the
subject table's date column into a concrete date. Nothing is declared, configured or
guessed. Where a bound cannot be proven, the consumer is reported as unbounded and it
blocks every cutoff -- that is the fail-closed rule, and it is what makes `lab_results`
unarchivable despite being stone cold by every table-level signal.

Captured 2026-08-07T02:54:03+00:00 from a live DataHub OSS v1.7.0 instance via `scripts/record_examples.py`.

## Summary

| Dataset | Consumer | Type | Deg | Derivation | Earliest date read |
|---|---|---|---|---|---|
| `billing_ledger` | Monthly Finance Close | DASHBOARD | 1 | `sql_predicate` | 2025-08-01 |
| `billing_ledger` | revenue_recognition_job | DATA_JOB | 1 | `sql_predicate` | 2026-01-01 |
| `billing_ledger` | AR Aging Buckets | CHART | 1 | `sql_predicate` | 2026-05-08 |
| `care_events_live` | Operations Command Centre | DASHBOARD | 1 | `sql_predicate` | 2026-07-30 |
| `care_events_live` | sepsis_risk_training | DATA_JOB | 1 | `sql_predicate` | 2025-08-06 |
| `care_events_live` | care_events_hourly_rollup | DATASET | 1 | `sql_predicate` | 2026-07-07 |
| `care_events_live` | sepsis_risk_model_v4 | MLMODEL | 2 | `sql_predicate` | 2025-08-06 |
| `care_events_live` | Daily Unit Census | CHART | 1 | `sql_predicate` | 2026-01-01 |
| `claims_history` | annual_audit_extract | DATA_JOB | 1 | `sql_predicate` | 2019-01-01 |
| `claims_history` | claims_actuarial_snapshot | DATASET | 1 | `no_queries_observed` | **unbounded** |
| `claims_history` | Claims Denial Rate by Reason | CHART | 1 | `sql_predicate` | 2023-01-01 |
| `lab_results` | hipaa_lab_disclosure_extract | DATA_JOB | 1 | `no_date_filter` | **unbounded** |
| `lab_results` | lab_abnormal_flags | DATASET | 1 | `sql_predicate` | 2026-05-08 |
| `patient_encounters` | Executive KPI Dashboard | DASHBOARD | 2 | `not_a_query_consumer` | 2024-01-01 |
| `patient_encounters` | Quarterly Compliance Dashboard | DASHBOARD | 1 | `sql_predicate` | 2024-01-01 |
| `patient_encounters` | encounter_readmission_etl | DATA_JOB | 1 | `sql_predicate` | 2026-05-08 |
| `patient_encounters` | patient_ltv_training | DATA_JOB | 1 | `sql_predicate` | 2024-08-06 |
| `patient_encounters` | encounters_monthly_agg | DATASET | 1 | `sql_predicate` | 2025-02-01 |
| `patient_encounters` | patient_ltv_model | MLMODEL | 2 | `sql_predicate` | 2024-08-06 |
| `patient_encounters` | Care Gap Closure Rate | CHART | 1 | `sql_predicate` | 2025-08-01 |

---

## `billing_ledger`

- date column: `posted_date` (datahub:structured_properties: declared as coldlineage.date_column)
- measured span in Postgres: 2016-01-01 to 2026-08-05, 450,000 rows, 70.4 MB
- latest provably safe cutoff: **2025-08-01** (the earliest date any consumer still reads)

### Monthly Finance Close

- urn: `urn:li:dashboard:(superset,finance_close_dashboard)`
- type: DASHBOARD on superset, 1 lineage hop(s) from the subject
- runs recorded in DataHub: 620
- **earliest_date_read: 2025-08-01**
- derivation: `sql_predicate` -- sqlglot parsed the statement DataHub holds for this consumer, found a lower bound on the subject's date column, and resolved it to a concrete date
- extracted predicate: `b.posted_date >= CAST('2025-08-01' AS DATE)`
- provenance: `datahub:queries` -- parsed from SQL recorded in DataHub (urn:li:query:coldlineage-billing_ledger-finance_close_dashboard)

Verbatim SQL, as read from the DataHub `Query` entity:

```sql
SELECT b.cost_center,
       b.gl_account,
       sum(b.debit_amount)  AS debits,
       sum(b.credit_amount) AS credits
FROM public.billing_ledger b
WHERE b.posted_date >= DATE '2025-08-01'
GROUP BY 1, 2
```

### revenue_recognition_job

- urn: `urn:li:dataJob:(urn:li:dataFlow:(airflow,finance_pipelines,PROD),revenue_recognition_job)`
- type: DATA_JOB, 1 lineage hop(s) from the subject
- runs recorded in DataHub: 218
- **earliest_date_read: 2026-01-01**
- derivation: `sql_predicate` -- sqlglot parsed the statement DataHub holds for this consumer, found a lower bound on the subject's date column, and resolved it to a concrete date
- extracted predicate: `b.posted_date BETWEEN CAST('2026-01-01' AS DATE) AND CURRENT_DATE`
- provenance: `datahub:queries` -- parsed from SQL recorded in DataHub (urn:li:query:coldlineage-billing_ledger-revenue_recognition_job)

Verbatim SQL, as read from the DataHub `Query` entity:

```sql
SELECT b.account_id,
       b.invoice_number,
       b.posted_date,
       b.entry_type,
       b.debit_amount - b.credit_amount AS net_amount
FROM public.billing_ledger b
WHERE b.posted_date BETWEEN DATE '2026-01-01' AND CURRENT_DATE
  AND b.reconciled IS TRUE
```

### AR Aging Buckets

- urn: `urn:li:chart:(superset,ar_aging_chart)`
- type: CHART on superset, 1 lineage hop(s) from the subject
- runs recorded in DataHub: 903
- **earliest_date_read: 2026-05-08**
- derivation: `sql_predicate` -- sqlglot parsed the statement DataHub holds for this consumer, found a lower bound on the subject's date column, and resolved it to a concrete date
- extracted predicate: `b.posted_date >= CURRENT_DATE - INTERVAL '90 DAYS'`
- provenance: `datahub:queries` -- parsed from SQL recorded in DataHub (urn:li:query:coldlineage-billing_ledger-ar_aging_chart)

Verbatim SQL, as read from the DataHub `Query` entity:

```sql
SELECT b.cost_center,
       CURRENT_DATE - b.posted_date                 AS age_days,
       sum(b.debit_amount - b.credit_amount)        AS open_balance
FROM public.billing_ledger b
WHERE b.posted_date >= CURRENT_DATE - INTERVAL '90 days'
  AND b.reconciled IS FALSE
GROUP BY 1, 2
```

---

## `care_events_live`

- date column: `event_date` (datahub:structured_properties: declared as coldlineage.date_column)
- measured span in Postgres: 2025-02-07 to 2026-08-05, 780,000 rows, 112.8 MB
- latest provably safe cutoff: **2025-08-06** (the earliest date any consumer still reads)

### Operations Command Centre

- urn: `urn:li:dashboard:(superset,operations_command_dashboard)`
- type: DASHBOARD on superset, 1 lineage hop(s) from the subject
- runs recorded in DataHub: 8412
- **earliest_date_read: 2026-07-30**
- derivation: `sql_predicate` -- sqlglot parsed the statement DataHub holds for this consumer, found a lower bound on the subject's date column, and resolved it to a concrete date
- extracted predicate: `v.event_date >= CURRENT_DATE - INTERVAL '7 DAYS'`
- provenance: `datahub:queries` -- parsed from SQL recorded in DataHub (urn:li:query:coldlineage-care_events_live-operations_command_dashboard)

Verbatim SQL, as read from the DataHub `Query` entity:

```sql
SELECT v.unit,
       v.event_type,
       v.severity,
       count(*)                                   AS events,
       count(*) FILTER (WHERE NOT v.acknowledged) AS unacknowledged
FROM public.care_events_live v
WHERE v.event_date >= CURRENT_DATE - INTERVAL '7 days'
GROUP BY 1, 2, 3
```

### sepsis_risk_training

- urn: `urn:li:dataJob:(urn:li:dataFlow:(airflow,ml_training,PROD),sepsis_risk_training)`
- type: DATA_JOB, 1 lineage hop(s) from the subject
- runs recorded in DataHub: 730
- **earliest_date_read: 2025-08-06**
- derivation: `sql_predicate` -- sqlglot parsed the statement DataHub holds for this consumer, found a lower bound on the subject's date column, and resolved it to a concrete date
- extracted predicate: `v.event_date > CAST((CURRENT_TIMESTAMP - INTERVAL '12 MONTHS') AS DATE)`
- provenance: `datahub:queries` -- parsed from SQL recorded in DataHub (urn:li:query:coldlineage-care_events_live-sepsis_risk_model_v4)

Verbatim SQL, as read from the DataHub `Query` entity:

```sql
SELECT v.patient_id,
       v.event_date,
       v.event_type,
       v.severity,
       v.unit,
       v.source_system
FROM public.care_events_live v
WHERE v.event_date > (NOW() - INTERVAL '12 months')::date
  AND v.severity >= 2
```

### care_events_hourly_rollup

- urn: `urn:li:dataset:(urn:li:dataPlatform:dbt,coldlineage.analytics.care_events_hourly_rollup,PROD)`
- type: DATASET on dbt, 1 lineage hop(s) from the subject
- runs recorded in DataHub: 17520
- **earliest_date_read: 2026-07-07**
- derivation: `sql_predicate` -- sqlglot parsed the statement DataHub holds for this consumer, found a lower bound on the subject's date column, and resolved it to a concrete date
- extracted predicate: `v.event_date >= CURRENT_DATE - INTERVAL '30 DAYS'`
- provenance: `datahub:queries` -- parsed from SQL recorded in DataHub (urn:li:query:coldlineage-care_events_live-care_events_hourly_rollup)

Verbatim SQL, as read from the DataHub `Query` entity:

```sql
SELECT date_trunc('hour', v.event_ts) AS hour_start,
       v.unit,
       v.event_type,
       count(*) AS events
FROM public.care_events_live v
WHERE v.event_date >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY 1, 2, 3
```

### sepsis_risk_model_v4

- urn: `urn:li:mlModel:(urn:li:dataPlatform:mlflow,sepsis_risk_model_v4,PROD)`
- type: MLMODEL on mlflow, 2 lineage hop(s) from the subject
- runs recorded in DataHub: 730
- **earliest_date_read: 2025-08-06**
- derivation: `sql_predicate` -- sqlglot parsed the statement DataHub holds for this consumer, found a lower bound on the subject's date column, and resolved it to a concrete date
- extracted predicate: `v.event_date > CAST((CURRENT_TIMESTAMP - INTERVAL '12 MONTHS') AS DATE)`
- provenance: `datahub:queries` -- parsed from SQL recorded in DataHub (urn:li:query:coldlineage-care_events_live-sepsis_risk_model_v4)

Verbatim SQL, as read from the DataHub `Query` entity:

```sql
SELECT v.patient_id,
       v.event_date,
       v.event_type,
       v.severity,
       v.unit,
       v.source_system
FROM public.care_events_live v
WHERE v.event_date > (NOW() - INTERVAL '12 months')::date
  AND v.severity >= 2
```

### Daily Unit Census

- urn: `urn:li:chart:(superset,unit_census_chart)`
- type: CHART on superset, 1 lineage hop(s) from the subject
- runs recorded in DataHub: 2140
- **earliest_date_read: 2026-01-01**
- derivation: `sql_predicate` -- sqlglot parsed the statement DataHub holds for this consumer, found a lower bound on the subject's date column, and resolved it to a concrete date
- extracted predicate: `v.event_date BETWEEN CAST('2026-01-01' AS DATE) AND CURRENT_DATE`
- provenance: `datahub:queries` -- parsed from SQL recorded in DataHub (urn:li:query:coldlineage-care_events_live-unit_census_chart)

Verbatim SQL, as read from the DataHub `Query` entity:

```sql
SELECT v.unit,
       v.event_date,
       count(DISTINCT v.patient_id) AS census
FROM public.care_events_live v
WHERE v.event_date BETWEEN DATE '2026-01-01' AND CURRENT_DATE
GROUP BY 1, 2
```

---

## `claims_history`

- date column: `service_date` (datahub:structured_properties: declared as coldlineage.date_column)
- measured span in Postgres: 2018-01-01 to 2024-12-30, 620,000 rows, 101.8 MB
- **no safe cutoff exists**: 1 unbounded consumer(s) (claims_actuarial_snapshot)

### annual_audit_extract

- urn: `urn:li:dataJob:(urn:li:dataFlow:(airflow,compliance_pipelines,PROD),annual_audit_extract)`
- type: DATA_JOB, 1 lineage hop(s) from the subject
- runs recorded in DataHub: 7
- **earliest_date_read: 2019-01-01**
- derivation: `sql_predicate` -- sqlglot parsed the statement DataHub holds for this consumer, found a lower bound on the subject's date column, and resolved it to a concrete date
- extracted predicate: `c.service_date >= CAST('2019-01-01' AS DATE)`
- provenance: `datahub:queries` -- parsed from SQL recorded in DataHub (urn:li:query:coldlineage-claims_history-annual_audit_extract)

Verbatim SQL, as read from the DataHub `Query` entity:

```sql
SELECT c.claim_number,
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
ORDER BY c.service_date
```

### claims_actuarial_snapshot

- urn: `urn:li:dataset:(urn:li:dataPlatform:snowflake,actuarial.reserving.claims_actuarial_snapshot,PROD)`
- type: DATASET on snowflake, 1 lineage hop(s) from the subject
- **earliest_date_read: NONE -- unbounded**
- derivation: `no_queries_observed` -- DataHub has a lineage edge but no query text for this consumer, so its lookback cannot be proven. Fail-closed: treated as unbounded
- extracted predicate: _none found_
- provenance: `datahub:lineage` -- lineage edge present, no query text recorded in DataHub

_No SQL recorded in DataHub for this consumer._

### Claims Denial Rate by Reason

- urn: `urn:li:chart:(superset,claims_denial_rate_chart)`
- type: CHART on superset, 1 lineage hop(s) from the subject
- runs recorded in DataHub: 88
- **earliest_date_read: 2023-01-01**
- derivation: `sql_predicate` -- sqlglot parsed the statement DataHub holds for this consumer, found a lower bound on the subject's date column, and resolved it to a concrete date
- extracted predicate: `c.service_date BETWEEN CAST('2023-01-01' AS DATE) AND CURRENT_DATE`
- provenance: `datahub:queries` -- parsed from SQL recorded in DataHub (urn:li:query:coldlineage-claims_history-claims_denial_rate_chart)

Verbatim SQL, as read from the DataHub `Query` entity:

```sql
SELECT date_trunc('month', c.service_date)::date AS month_start,
       c.denial_reason,
       count(*) AS denied_claims
FROM public.claims_history c
WHERE c.claim_status = 'DENIED'
  AND c.service_date BETWEEN DATE '2023-01-01' AND CURRENT_DATE
GROUP BY 1, 2
```

---

## `lab_results`

- date column: `collected_date` (datahub:structured_properties: declared as coldlineage.date_column)
- measured span in Postgres: 2019-03-01 to 2026-07-31, 700,000 rows, 111.5 MB
- **no safe cutoff exists**: 1 unbounded consumer(s) (hipaa_lab_disclosure_extract)

### hipaa_lab_disclosure_extract

- urn: `urn:li:dataJob:(urn:li:dataFlow:(airflow,compliance_pipelines,PROD),hipaa_lab_disclosure_extract)`
- type: DATA_JOB, 1 lineage hop(s) from the subject
- runs recorded in DataHub: 27
- **earliest_date_read: NONE -- unbounded**
- derivation: `no_date_filter` -- the statement was parsed successfully and places NO lower bound on the date column -- an unbounded scan. Fail-closed: no cutoff can be proven safe
- extracted predicate: _none found_
- provenance: `datahub:queries` -- no lower bound on the date column -- unbounded scan

Verbatim SQL, as read from the DataHub `Query` entity:

```sql
SELECT r.result_id,
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
ORDER BY r.collected_date
```

### lab_abnormal_flags

- urn: `urn:li:dataset:(urn:li:dataPlatform:dbt,coldlineage.analytics.lab_abnormal_flags,PROD)`
- type: DATASET on dbt, 1 lineage hop(s) from the subject
- runs recorded in DataHub: 214
- **earliest_date_read: 2026-05-08**
- derivation: `sql_predicate` -- sqlglot parsed the statement DataHub holds for this consumer, found a lower bound on the subject's date column, and resolved it to a concrete date
- extracted predicate: `r.collected_date >= CURRENT_DATE - INTERVAL '90 DAYS'`
- provenance: `datahub:queries` -- parsed from SQL recorded in DataHub (urn:li:query:coldlineage-lab_results-lab_abnormal_flags)

Verbatim SQL, as read from the DataHub `Query` entity:

```sql
SELECT r.patient_id,
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
  AND r.abnormal_flag <> 'N'
```

---

## `patient_encounters`

- date column: `event_date` (datahub:structured_properties: declared as coldlineage.date_column)
- measured span in Postgres: 2019-01-01 to 2026-08-05, 1,100,000 rows, 169.8 MB
- latest provably safe cutoff: **2024-01-01** (the earliest date any consumer still reads)

### Executive KPI Dashboard

- urn: `urn:li:dashboard:(superset,executive_kpi_dashboard)`
- type: DASHBOARD on superset, 2 lineage hop(s) from the subject
- **earliest_date_read: 2024-01-01**
- derivation: `not_a_query_consumer` -- this consumer does not read the subject directly; it is reached at >1 hop and inherits the earliest bound of the consumers between it and the subject
- extracted predicate: _none found_
- provenance: `datahub:lineage` -- reads the subject at 2 hops via an intermediate; inherits the earliest bound of its upstreams (2024-01-01)

_No SQL recorded in DataHub for this consumer._

### Quarterly Compliance Dashboard

- urn: `urn:li:dashboard:(superset,quarterly_compliance_dashboard)`
- type: DASHBOARD on superset, 1 lineage hop(s) from the subject
- runs recorded in DataHub: 96
- **earliest_date_read: 2024-01-01**
- derivation: `sql_predicate` -- sqlglot parsed the statement DataHub holds for this consumer, found a lower bound on the subject's date column, and resolved it to a concrete date
- extracted predicate: `e.event_date BETWEEN CAST('2024-01-01' AS DATE) AND CURRENT_DATE`
- provenance: `datahub:queries` -- parsed from SQL recorded in DataHub (urn:li:query:coldlineage-patient_encounters-quarterly_compliance_dashboard)

Verbatim SQL, as read from the DataHub `Query` entity:

```sql
SELECT e.region,
       e.encounter_type,
       date_trunc('quarter', e.event_date)::date AS quarter_start,
       count(*)                                  AS encounters,
       sum(e.total_charges)                      AS total_charges
FROM public.patient_encounters e
WHERE e.event_date BETWEEN DATE '2024-01-01' AND CURRENT_DATE
GROUP BY 1, 2, 3
ORDER BY 3 DESC, 1
```

### encounter_readmission_etl

- urn: `urn:li:dataJob:(urn:li:dataFlow:(airflow,clinical_pipelines,PROD),encounter_readmission_etl)`
- type: DATA_JOB, 1 lineage hop(s) from the subject
- runs recorded in DataHub: 365
- **earliest_date_read: 2026-05-08**
- derivation: `sql_predicate` -- sqlglot parsed the statement DataHub holds for this consumer, found a lower bound on the subject's date column, and resolved it to a concrete date
- extracted predicate: `e.event_date >= CURRENT_DATE - INTERVAL '90 DAYS'`
- provenance: `datahub:queries` -- parsed from SQL recorded in DataHub (urn:li:query:coldlineage-patient_encounters-encounter_readmission_etl)

Verbatim SQL, as read from the DataHub `Query` entity:

```sql
SELECT e.patient_id,
       e.event_date,
       e.department,
       e.length_of_stay_days,
       lead(e.event_date) OVER (PARTITION BY e.patient_id ORDER BY e.event_date) AS next_event_date
FROM public.patient_encounters e
WHERE e.event_date >= CURRENT_DATE - INTERVAL '90 days'
```

### patient_ltv_training

- urn: `urn:li:dataJob:(urn:li:dataFlow:(airflow,ml_training,PROD),patient_ltv_training)`
- type: DATA_JOB, 1 lineage hop(s) from the subject
- runs recorded in DataHub: 48
- **earliest_date_read: 2024-08-06**
- derivation: `sql_predicate` -- sqlglot parsed the statement DataHub holds for this consumer, found a lower bound on the subject's date column, and resolved it to a concrete date
- extracted predicate: `e.event_date > CAST((CURRENT_TIMESTAMP - INTERVAL '24 MONTHS') AS DATE)`
- provenance: `datahub:queries` -- parsed from SQL recorded in DataHub (urn:li:query:coldlineage-patient_encounters-patient_ltv_model)

Verbatim SQL, as read from the DataHub `Query` entity:

```sql
SELECT e.patient_id,
       count(*)                   AS encounter_count,
       sum(e.total_charges)       AS lifetime_charges,
       avg(e.length_of_stay_days) AS avg_length_of_stay,
       max(e.event_date)          AS last_seen
FROM public.patient_encounters e
WHERE e.event_date > (NOW() - INTERVAL '24 months')::date
GROUP BY e.patient_id
```

### encounters_monthly_agg

- urn: `urn:li:dataset:(urn:li:dataPlatform:dbt,coldlineage.analytics.encounters_monthly_agg,PROD)`
- type: DATASET on dbt, 1 lineage hop(s) from the subject
- runs recorded in DataHub: 540
- **earliest_date_read: 2025-02-01**
- derivation: `sql_predicate` -- sqlglot parsed the statement DataHub holds for this consumer, found a lower bound on the subject's date column, and resolved it to a concrete date
- extracted predicate: `DATE_TRUNC('MONTH', e.event_date) >= DATE_TRUNC('MONTH', CURRENT_DATE - INTERVAL '18 MONTHS')`
- provenance: `datahub:queries` -- parsed from SQL recorded in DataHub (urn:li:query:coldlineage-patient_encounters-encounters_monthly_agg)

Verbatim SQL, as read from the DataHub `Query` entity:

```sql
SELECT date_trunc('month', e.event_date)::date AS month_start,
       e.department,
       e.payer,
       count(*)             AS encounters,
       sum(e.total_charges) AS charges
FROM public.patient_encounters e
WHERE date_trunc('month', e.event_date) >= date_trunc('month', CURRENT_DATE - INTERVAL '18 months')
GROUP BY 1, 2, 3
```

### patient_ltv_model

- urn: `urn:li:mlModel:(urn:li:dataPlatform:mlflow,patient_ltv_model,PROD)`
- type: MLMODEL on mlflow, 2 lineage hop(s) from the subject
- runs recorded in DataHub: 48
- **earliest_date_read: 2024-08-06**
- derivation: `sql_predicate` -- sqlglot parsed the statement DataHub holds for this consumer, found a lower bound on the subject's date column, and resolved it to a concrete date
- extracted predicate: `e.event_date > CAST((CURRENT_TIMESTAMP - INTERVAL '24 MONTHS') AS DATE)`
- provenance: `datahub:queries` -- parsed from SQL recorded in DataHub (urn:li:query:coldlineage-patient_encounters-patient_ltv_model)

Verbatim SQL, as read from the DataHub `Query` entity:

```sql
SELECT e.patient_id,
       count(*)                   AS encounter_count,
       sum(e.total_charges)       AS lifetime_charges,
       avg(e.length_of_stay_days) AS avg_length_of_stay,
       max(e.event_date)          AS last_seen
FROM public.patient_encounters e
WHERE e.event_date > (NOW() - INTERVAL '24 months')::date
GROUP BY e.patient_id
```

### Care Gap Closure Rate

- urn: `urn:li:chart:(superset,care_gap_closure_chart)`
- type: CHART on superset, 1 lineage hop(s) from the subject
- runs recorded in DataHub: 1204
- **earliest_date_read: 2025-08-01**
- derivation: `sql_predicate` -- sqlglot parsed the statement DataHub holds for this consumer, found a lower bound on the subject's date column, and resolved it to a concrete date
- extracted predicate: `e.event_date >= CAST('2025-08-01' AS DATE)`
- provenance: `datahub:queries` -- parsed from SQL recorded in DataHub (urn:li:query:coldlineage-patient_encounters-care_gap_closure_chart)

Verbatim SQL, as read from the DataHub `Query` entity:

```sql
SELECT e.primary_diagnosis_code,
       count(*) FILTER (WHERE e.encounter_type = 'preventive') AS preventive_encounters,
       count(*)                                                AS total_encounters
FROM public.patient_encounters e
WHERE e.event_date >= DATE '2025-08-01'
GROUP BY e.primary_diagnosis_code
ORDER BY total_encounters DESC
LIMIT 25
```

---

## How to reproduce a single row by hand

```python
from datetime import date
import sys; sys.path.insert(0, 'backend')
from app.services.window import HistoryWindowExtractor

sql = "SELECT ... FROM public.patient_encounters e "\
      "WHERE e.event_date > (NOW() - INTERVAL '24 months')::date"
print(HistoryWindowExtractor().extract(sql, 'patient_encounters', 'event_date',
                                       as_of=date.today()))
```
