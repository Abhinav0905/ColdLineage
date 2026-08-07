# Decision Rules

The scoring, the blockers, and the range-safety rule. All of it is deterministic: same inputs, same
answer, every time. Nothing here is a model judgement, because a tiering decision that cannot be
recomputed by hand cannot be defended in an audit.

Two things are kept strictly apart:

- **Temperature** answers *"how much is this dataset being used?"* It is a continuous score.
- **Blockers** answer *"is there a rule that forbids this regardless?"* They are boolean and they
  win.

A legal hold is not a very high temperature. It is a separate gate. Folding policy into a score is
how a cold-looking table under litigation hold gets archived by arithmetic.

---

## 1. Temperature Score

Higher is hotter. Range 0–100.

```
score = 100 × ( 0.42 × recency
              + 0.28 × frequency
              + 0.18 × downstream
              + 0.12 × criticality )
```

| Weight | Component     | Question it answers                          | Provenance                      |
| ------ | ------------- | --------------------------------------------- | ------------------------------- |
| 42%    | `recency`     | When was this last read at all?               | `datahub:usage`                 |
| 28%    | `frequency`   | How often is it read?                         | `datahub:usage`                 |
| 18%    | `downstream`  | How many live things depend on it?            | `datahub:lineage`               |
| 12%    | `criticality` | How important does the owner say it is?       | `datahub:structured_properties` |

Recency carries the most weight because it is the signal least likely to be an artifact of how
telemetry was configured. Criticality carries the least because it is declared rather than observed
— but it is included, because usage telemetry cannot see that a table is the quarterly regulatory
filing source that runs four times a year.

### Component normalization

These are the reference implementation's constants
(`backend/app/services/temperature.py`). The four weights are the contract; the normalizations below
are what the executor actually computes, and you should match them so your number and the UI's
number agree.

```
days_since_last_query = (today − last_query_at).days          # from datahub:usage

recency     = max(0, 1 − min(days_since_last_query, 730) / 730)
frequency   = min(1, log1p(query_count_30d) / log1p(1000))
downstream  = min(1, active_downstream_count / 5)
criticality = clamp(business_criticality, 0, 1)
```

- **`recency`** decays linearly over a two-year horizon. Anything unread for 730+ days scores 0.
- **`frequency`** is logarithmic and saturates near 1000 queries per 30 days. Linear scaling would
  let one busy table make every other table look frozen.
- **`downstream`** counts *active, non-deprecated* degree-1 consumers and saturates at 5. A
  deprecated dashboard is not a dependency.
- **`criticality`** is `io.coldlineage.policy.businessCriticality`, owner-declared, 0.0–1.0.

Report all four components and the raw input behind each. The score exists to be argued with.

### Missing inputs — do not substitute zero

Zero is a measurement. Absence is not.

If a component's input is unavailable, **do not** contribute 0 for it — that biases the score cold
and manufactures archive eligibility out of a telemetry gap, which is the single most dangerous
failure mode of this whole system.

Instead, report the score as a **band**:

```
score_min = 100 × Σ (weight × value)  over known components only
score_max = score_min + 100 × Σ weight  over unknown components
```

Classify on `score_min` for display, but treat any dataset whose band spans a class boundary as
**unclassified**, and never emit `SAFE_TO_ARCHIVE` for it. State which signal is missing and what
would fix it (usually: configure usage or query ingestion for that platform).

Example: usage stats unavailable, everything else known → `recency` and `frequency` unknown → the
band is 70 points wide. That is not a cold dataset. That is an unmeasured dataset.

---

## 2. Classification Bands

| Score      | Class    | What it means                                                        |
| ---------- | -------- | -------------------------------------------------------------------- |
| `>= 75`    | `HOT`    | Actively read. Do not propose an archive of any range.               |
| `>= 45`    | `WARM`   | Regular use. A deep historical range may still be movable.           |
| `>= 20`    | `COOL`   | Light use. Range archive is a reasonable proposal.                   |
| `>= 8`     | `COLD`   | Near-dormant. Strong candidate, subject to consumer windows.         |
| `< 8`      | `FROZEN` | Effectively unread. Strongest candidate.                             |

Bands are evaluated top-down; the first match wins. Boundaries are inclusive at the lower edge.

**A class is not a permission.** `FROZEN` with an unbounded consumer is `DO_NOT_ARCHIVE`. `WARM` with
every consumer bounded to the last 90 days can safely shed three years of history. The class ranks
candidates; the range-safety rule decides.

---

## 3. Blocker Taxonomy

Evaluated in this order. Evaluation is short-circuit only for `LEGAL_HOLD` — for everything else,
collect **all** applicable blockers so the user sees the full picture in one pass rather than fixing
them one at a time.

### `LEGAL_HOLD` — unconditional

- **Trigger:** `io.coldlineage.policy.legalHold == "ACTIVE"`.
- **Provenance:** `datahub:structured_properties`.
- **Effect:** stop. No plan, no simulation, no cutoff. Not overridable by approval, by user
  instruction, or by any temperature score.
- **Message:** name the matter from `io.coldlineage.policy.legalHoldMatter` so the block is
  attributable. `"Blocked: ACTIVE legal hold (matter LIT-2025-118)."` An anonymous block invites
  someone to route around it.
- `RELEASED` and `NONE` do not block. A **missing** property does not block, but it does lower
  confidence — record it as "no hold declared", not "no hold".

### `RETENTION_FLOOR`

- **Trigger:** `cutoff_date > (max_date − retention_years)`. That is, the cutoff would remove history
  the retention policy requires to stay queryable.
- **Provenance:** `datahub:structured_properties` (`io.coldlineage.policy.retentionYears`).
- **Effect:** blocks *this cutoff*, not the dataset. Compute and offer the oldest compliant cutoff:
  `max_date − retention_years`.
- Absent `retentionYears` means no declared floor. Say "no retention floor declared" — do not
  silently treat absence as permission, and surface it to the approver as a governance gap.

### `UNBOUNDED_CONSUMER`

- **Trigger:** any active downstream consumer with `derivation` of `no_date_filter` or
  `no_queries_observed`, i.e. `ConsumerWindow.is_unbounded`.
- **Provenance:** `datahub:queries` for `no_date_filter`; `datahub:queries` with detail
  `"no statements captured"` for `no_queries_observed`.
- **Effect:** blocks **every** cutoff. There is no date old enough to be safe from a full-table scan.
- **Message:** name the consumer and quote its statement (or state that no statement was captured).
  The remediation is concrete and worth offering: add a date predicate to that consumer's query, or
  have its owner declare a window, and the block clears.

### `NO_DATE_COLUMN`

- **Trigger:** no partition key and no `DATE`/`TIMESTAMP` column in `schemaMetadata.fields`.
- **Provenance:** `datahub:schema`.
- **Effect:** range archive is undefined. Whole-table archival is out of scope for this skill —
  ColdLineage moves ranges, not tables.

### `DEPRECATED_UPSTREAM`

- **Trigger:** an upstream dataset has `deprecation.deprecated == true`.
- **Provenance:** `datahub:deprecation`.
- **Effect:** **warn, do not hard-block.** A deprecated upstream means this dataset's history may be
  the last surviving copy, so moving it to cold storage raises the cost of a mistake. Surface it
  prominently in the approval summary and require the approver to acknowledge it. Downgrade
  `SAFE_TO_ARCHIVE` to `ARCHIVE_WITH_REHYDRATION`.

---

## 4. The Range-Safety Rule

The rule the whole project reduces to:

> **A cutoff is safe if and only if it is strictly older than `min(earliest_date_read)` across all
> active consumers, and no consumer window is unbounded or unknown.**

```
bounded  = [c for c in consumers if c.earliest_date_read is not None]
unknown  = [c for c in consumers if c.earliest_date_read is None]

if unknown:                      → DO_NOT_ARCHIVE          (blocked by UNBOUNDED_CONSUMER)
if not consumers:                → lineage may be missing; treat as unknown, not as safe
binding  = min(bounded, key=earliest_date_read)
headroom = (binding.earliest_date_read − cutoff_date).days

headroom <  0   → BLOCKED   the cutoff removes rows this consumer reads
headroom == 0   → BLOCKED   off-by-one is not a margin
0 < headroom < 30 → TIGHT   clears, but with less than the safety margin
headroom >= 30  → SAFE
```

**Unknown is never permissive.** This is the asymmetry the entire design rests on: leaving data hot
costs money and is reversible; deleting data a consumer still reads is neither. When evidence is
missing, the answer is no.

### Per-consumer state

| State     | Condition                                | Contribution to the verdict          |
| --------- | ---------------------------------------- | ------------------------------------- |
| `safe`    | `headroom_days >= 30`                    | none                                  |
| `tight`   | `0 < headroom_days < 30`                 | forces `ARCHIVE_WITH_REHYDRATION`     |
| `blocked` | `headroom_days <= 0`                     | forces `DO_NOT_ARCHIVE`               |
| `unknown` | no bound could be established            | forces `DO_NOT_ARCHIVE`               |

### Verdict

| Recommendation              | When                                                                           |
| --------------------------- | ------------------------------------------------------------------------------ |
| `SAFE_TO_ARCHIVE`           | every consumer `safe`, no blockers, no `DEPRECATED_UPSTREAM` warning            |
| `ARCHIVE_WITH_REHYDRATION`  | at least one `tight` consumer, or a deprecated upstream; nothing `blocked`/`unknown` |
| `DO_NOT_ARCHIVE`            | any `blocked` or `unknown` consumer, or any hard blocker                        |

`ARCHIVE_WITH_REHYDRATION` means: proceed, but the restore path is load-bearing. Confirm the restore
SLA is acceptable to the tight consumer's owner before executing, and record that in the approval.

The `binding_constraint` is the single consumer that produced `min(earliest_date_read)`. Always name
it, and always show its verbatim predicate. It is the entire argument in one line — "we can go back
to 2024-08-16 because the Encounter Trends dashboard filters `encounter_date >= '2024-08-16'`" is a
statement an approver can check. "The model says it is safe" is not.

### Degree-2 and beyond

A dashboard two hops out does not read the subject table directly; it reads the degree-1 dataset
between them. Its effective window is:

```
effective_window = min(own_window, upstream_dataset_window)
```

If the intervening dataset is itself unbounded, the whole branch is unbounded. Do not stop the
traversal at degree 1 unless you say so explicitly in the report — an unexplored branch is an
unknown branch, and unknown blocks.

---

## 5. Proposing a Cutoff

```
consumer_floor  = min(earliest_date_read across bounded consumers) − 30 days
retention_floor = max_date − retention_years            (skip if not declared)
cutoff          = min(consumer_floor, retention_floor)
```

Take the **older** of the two floors — both constraints must hold simultaneously.

Then sanity-check the result:

- `cutoff <= min_date` → nothing is in scope. Say so; do not produce an empty plan.
- `rows_in_scope` under a few thousand → the saving will not repay the operational risk. Say that
  too. Recommending a 400-row archive is a worse answer than recommending none.
- The user proposed their own cutoff → simulate **theirs**, report the verdict honestly, and offer
  the computed one alongside. Never quietly substitute your cutoff for theirs.

---

## 6. Confidence

```
confidence = signal_completeness × window_completeness

signal_completeness = (# of the 4 temperature inputs actually observed) / 4
window_completeness = (# consumers with a bounded window) / (# consumers)
```

- `window_completeness == 0` with consumers present → `confidence = 0`, and the verdict is
  `DO_NOT_ARCHIVE` regardless of anything else.
- No consumers found at all → `confidence = null`. An empty lineage graph is unmeasured, not clean,
  and a null is the honest value.
- **Never round confidence up, and never fill a null with a plausible number.** `null` is a valid,
  informative answer. A fabricated `0.9` is not.

---

## 7. Evidence Status Mapping

Each `EvidenceItem` carries `status` of `pass` / `warn` / `block`:

| Status  | Meaning                                                        | Example                                            |
| ------- | -------------------------------------------------------------- | -------------------------------------------------- |
| `pass`  | Checked, and it supports the proposal                          | `legalHold = NONE`; all consumers `safe`            |
| `warn`  | Checked, and it constrains the proposal without forbidding it  | tight consumer; deprecated upstream; PII tags       |
| `block` | Checked, and it forbids the proposal                           | `legalHold = ACTIVE`; unbounded consumer            |

A signal you could not check is **not** `pass`. It is an evidence item with provenance
`unavailable` and status `warn`. The distinction between "checked and fine" and "could not check" is
the difference between an audit trail and a decoration.

---

## 8. Cost Model

```
gb            = bytes_in_scope / 1024³
monthly_saving = gb × (hot_cost_per_gb_month − cold_cost_per_gb_month)
```

Defaults in `backend/app/core/config.py`: hot `$0.115`/GB-month (warehouse-attached storage, which is
what a Postgres or Snowflake estate actually bills at), cold `$0.004`/GB-month (S3 Glacier Instant
Retrieval, us-east-1 list price).

`bytes_in_scope` is **measured** by the executor against the real rows in the range, never estimated
from a row count multiplied by an average row width. If the executor has not measured it, report the
saving as unavailable rather than computing one from an estimate. A savings figure derived from a
guess is the kind of number that gets a project cited in a postmortem.

State the assumptions with the number: list price, region, tier, and that egress and retrieval costs
are excluded.
