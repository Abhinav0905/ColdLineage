---
name: assess-data-temperature
description: |
  Use this skill when the user wants to decide whether a dataset or a historical date range inside it can be moved from hot warehouse storage to a colder tier, or wants to understand storage cost, retention, or archival risk for a table. Triggers on: "can we archive X", "is this table cold", "what is safe to move to cold storage", "how far back does anything actually read X", "storage cost of X", "retention policy for X", "tier this table", "purge old partitions", "what breaks if I drop data before <date>", or any request involving data temperature, lifecycle, tiering, archival, rehydration, or cold storage. For pure dependency tracing with no archival question, use `/datahub-lineage`. For metadata edits with no archival question, use `/datahub-enrich`.
user-invocable: true
min-cli-version: 1.4.0
allowed-tools: Bash(datahub *), Bash(curl *)
effort: high
---

# Assess Data Temperature

You are a data lifecycle engineer. Your job is to decide whether a **specific date range inside a
table** can be moved to cold storage, prove that decision against every downstream consumer's actual
read history, and — only after a human approves — drive the move and write the receipt back into
DataHub.

**The thing that makes this skill different:** DataHub's metadata model is dataset- and column-level.
It can tell you a table is unused. It structurally cannot tell you that rows before `2024-07-01` are
cold while the last 90 days are hot, and it cannot move a byte. This skill closes both gaps. It reads
DataHub for context, derives a per-consumer history window by parsing real SQL, and delegates the
physical move to the ColdLineage executor, which verifies the object by read-back before deleting
anything.

**You never move data yourself.** You have no database credentials and no object-store credentials.
Every physical action goes through the executor HTTP API in Step 6 onward.

---

## Not This Skill

| If the user wants to...                                   | Use this instead                          |
| --------------------------------------------------------- | ----------------------------------------- |
| Trace lineage with no archival question                   | `/datahub-lineage`                        |
| Search or answer "who owns X?"                            | `/datahub-search`                         |
| Edit tags/owners/descriptions with no archival question   | `/datahub-enrich`                         |
| Create assertions or handle incidents                     | `/datahub-quality`                        |
| Configure the DataHub connection                          | `/datahub-setup`                          |

**Key boundary:** this skill owns the question *"is this range safe to move, and what does it cost to
keep?"*. It uses lineage as an input, but a lineage question alone is not this skill.

---

## Multi-Agent Compatibility

Works across Claude Code, Cursor, Codex, Copilot, Gemini CLI, and Windsurf. The workflow, the GraphQL
documents in `references/datahub-queries.md`, and the executor HTTP calls are portable. Only
`allowed-tools`, `user-invocable`, and `effort` in the frontmatter are Claude Code-specific; other
agents ignore them harmlessly.

---

## Step 0: Establish Your Context Sources — and Say Which You Got

Do this first, every run. Never proceed on an assumed connection.

**DataHub.** Detect in this order:

1. MCP tools whose names end in `search`, `get_entities`, `get_lineage`, `execute_graphql` → preferred
   for single-entity reads and simple updates.
2. `which datahub` → CLI path. Confirm reachability with
   `datahub get --urn "urn:li:corpuser:datahub"`. (`datahub check server-health` does not exist.)
3. Neither → tell the user DataHub context is unavailable and point at `/datahub-setup`. **Do not
   continue to a recommendation.** An archive decision without catalog context is exactly the
   age-based `DELETE` this project exists to replace.

**ColdLineage executor.** `curl -s $COLDLINEAGE_URL/api/health` (default
`COLDLINEAGE_URL=http://localhost:8000`). The response carries the DataHub mode the *executor* is in:

```json
{"ok": true, "service": "ColdLineage", "version": "...",
 "datahub": {"mode": "live", "reachable": true, "gms_url": "...",
             "recorded_at": null, "detail": "...", "entity_count": 412}}
```

There are exactly two modes and no third:

- `live` — the executor is talking to a real GMS at `gms_url`.
- `replay` — the executor is serving verbatim recorded GMS responses from committed cassettes.
  `recorded_at` is the recording timestamp. Say it out loud: *"DataHub context is replayed from a
  cassette recorded at `<recorded_at>`, not a live catalog."*

**Open your report with a one-line source banner** naming the DataHub mode, `reachable`, and whether
you read the catalog over MCP or CLI. If `reachable` is false, every DataHub-derived field in your
output must carry provenance `"unavailable"` and you must not emit a `SAFE_TO_ARCHIVE`
recommendation.

---

## Step 1: Resolve the Dataset URN

1. URN given → use it.
2. Name given → `datahub search "<name>" --where "entity_type = dataset" --limit 5`.
3. More than one match → present name, URN, platform, env and ask. Never guess between two matches;
   archiving the wrong table is unrecoverable.
4. Echo back the resolved `urn`, `name`, `platform`, `env` before doing anything else.

**Input validation:** reject shell metacharacters in names and URNs before passing them to the CLI.
Dataset URNs contain parentheses and commas — always quote them.

---

## Step 2: Read Context From DataHub

Run the entity read in `references/datahub-queries.md` § *Dataset context*. Collect:

| Signal                                         | Source aspect / field                                      | Provenance label                    |
| ---------------------------------------------- | ---------------------------------------------------------- | ----------------------------------- |
| Schema, candidate date/partition columns       | `schemaMetadata.fields`                                     | `datahub:schema`                    |
| Owners                                         | `ownership.owners`                                          | `datahub:ownership`                 |
| Domain, tags, glossary terms, sensitivity      | `domain`, `tags`, `glossaryTerms`                           | `datahub:tags`                      |
| Deprecation                                    | `deprecation { deprecated note }`                           | `datahub:deprecation`               |
| Retention floor, legal hold, criticality       | `structuredProperties` → `io.coldlineage.policy.*`          | `datahub:structured_properties`     |
| Query counts, unique users, last access        | `usageStats(range: MONTH)`                                  | `datahub:usage`                     |
| Downstream consumers                           | `searchAcrossLineage(direction: DOWNSTREAM)`                | `datahub:lineage`                   |
| Consumer SQL                                   | `listQueries(input: {datasetUrn: ...})`                     | `datahub:queries`                   |

**Attach a provenance label to every value you carry forward.** A number with no provenance is a
number someone made up, and you must not put one in a recommendation.

**Pick the date column deliberately.** Prefer a declared partition key, then a `DATE`/`TIMESTAMP`
column named like `event_date`, `occurred_at`, `created_at`, `encounter_date`. If the schema has no
usable temporal column, stop: emit blocker `NO_DATE_COLUMN`. A range archive is undefined without a
column to range over.

---

## Step 3: Derive Each Consumer's Real History Window

This is the analytical core. For every downstream entity found in Step 2, establish **how far back it
actually reads**, and record *how* you learned it:

| Derivation             | Meaning                                                                 | Treated as |
| ---------------------- | ----------------------------------------------------------------------- | ---------- |
| `sql_predicate`        | Parsed a real query; found an explicit lower bound on the date column   | bounded    |
| `declared_property`    | Owner declared the window via a structured property                     | bounded    |
| `no_date_filter`       | Consumer issues an unbounded scan of the table                          | **blocking** |
| `no_queries_observed`  | Consumer is in lineage but no query text was captured                   | **blocking** |
| `not_a_query_consumer` | Dashboard/chart/ML model reached through an intermediate dataset        | inherit from its upstream dataset |

Read each consumer's SQL from `listQueries` (see `references/datahub-queries.md` § *Consumer
queries*). Extract the lower bound on the date column from the `WHERE` clause. Keep the extracted
predicate **verbatim** and keep the query text **verbatim** — that fragment is the evidence you will
show the approver, and paraphrasing it destroys its value.

Handle relative bounds honestly. `WHERE event_date >= CURRENT_DATE - INTERVAL '180 day'` yields a
*rolling* earliest read; resolve it against today and label it as rolling, because it slides forward
and a cutoff that is tight today is safer tomorrow — but never the reverse.

**Rules you may not bend:**

- An unbounded consumer (`no_date_filter`) blocks **every** cutoff. There is no date old enough to be
  safe from a full-table scan.
- `no_queries_observed` is **unknown, not permissive**. Absence of captured SQL is absence of
  evidence, not evidence of absence.
- Never infer row-level non-use from table-level telemetry. "This table had 3 queries last month"
  says nothing about which rows those queries touched. Only a parsed predicate does.

If the ColdLineage executor is reachable, `GET /api/datasets/{id}` returns these windows already
derived by the server's sqlglot parser, with provenance attached. Prefer the executor's derivation
over your own reading — it is deterministic and the UI shows the same numbers. Use your own parse to
sanity-check it, and report any disagreement instead of silently picking one.

---

## Step 4: Score Temperature and Collect Blockers

Compute the score using the exact weights in `references/decision-rules.md`: **42%** access recency,
**28%** query frequency, **18%** active downstream count, **12%** declared business criticality.
Higher is hotter. Bands: `HOT >= 75`, `WARM >= 45`, `COOL >= 20`, `COLD >= 8`, else `FROZEN`.

Show all four components and their inputs. The score exists to be argued with, not trusted.

Evaluate blockers **separately from the score** — a cold-looking table under legal hold must never be
archivable by arithmetic:

`LEGAL_HOLD` · `RETENTION_FLOOR` · `UNBOUNDED_CONSUMER` · `NO_DATE_COLUMN` · `DEPRECATED_UPSTREAM`

Full taxonomy and evaluation order in `references/decision-rules.md`.

---

## Step 5: Propose a Cutoff and Simulate It

A cutoff is safe **iff it is strictly older than `min(earliest_date_read)` across all active
consumers**, and no consumer window is unbounded or unknown.

Propose: `cutoff = min(earliest_date_read) - safety_margin`, then floor it against the retention
requirement (`max_date - retention_years`), then take the older of the two. Default safety margin: 30
days.

Then ask the executor for the authoritative verdict rather than asserting your own:

```bash
curl -s -X POST "$COLDLINEAGE_URL/api/datasets/<id>/simulate" \
  -H 'content-type: application/json' \
  -d '{"cutoff_date":"2024-07-01"}'
```

It returns a `RangeVerdict`: `recommendation` (`SAFE_TO_ARCHIVE` | `ARCHIVE_WITH_REHYDRATION` |
`DO_NOT_ARCHIVE`), per-consumer `state` (`safe` | `tight` | `blocked` | `unknown`), the
`binding_constraint` (the single consumer that limits you), `headroom_days`, and a `rationale`.

If your independent analysis disagrees with the verdict, **report both and defer to the stricter
one.** Never present your own looser conclusion as the answer.

---

## Step 6: Build the Plan

```bash
curl -s -X POST "$COLDLINEAGE_URL/api/datasets/<id>/plan" \
  -H 'content-type: application/json' -d '{"cutoff_date":"2024-07-01"}'
```

Returns an `ArchivePlan`: `plan_hash`, `rows_in_scope`, `bytes_in_scope`, the embedded `verdict`,
`blockers`, `monthly_savings_usd`, `requires_approval: true`.

The `plan_hash` binds dataset + cutoff + row count + verdict. `POST /api/execute` requires it, so a
plan cannot be approved for one cutoff and executed against another. If the underlying data moved,
the hash no longer matches and execution is refused — re-plan, do not retry.

A `409` here carries the verdict or blocker list in `detail`. That is a refusal, not a transient
error. Surface the reason; do not retry it.

---

## Step 7: Require Human Approval — Actually Stop

Present the plan and **stop**. Do not call `/api/execute` in the same turn you presented the plan,
even if the user previously said "go ahead and archive it". Approval must be given against the
concrete numbers.

Show:

1. Dataset, resolved date column, proposed cutoff.
2. Rows and bytes in scope; estimated monthly saving.
3. Temperature score with all four components.
4. **The consumer table** — every downstream, its earliest read, derivation, headroom, state. Include
   the verbatim predicate for the binding constraint.
5. Blockers, each with its provenance.
6. What is reversible: the range is restorable via `POST /api/restore`, subject to the restore SLA.

Then ask for explicit approval and capture **who** approved. `approved_by` goes into the audit trail
and into the DataHub writeback; an anonymous approval is not an approval.

---

## Step 8: Execute and Verify

```bash
curl -s -X POST "$COLDLINEAGE_URL/api/execute" \
  -H 'content-type: application/json' \
  -d '{"plan_hash":"<hash>","approved_by":"<name or email>"}'
```

Returns `{run_id, manifest, verification, datahub_writeback}`.

The executor's ordering is fixed and is the safety property of this whole system:

> select rows → write Parquet → compute SHA-256 → upload → write manifest → **read the object back
> and re-verify digest, row count and schema** → only then delete hot rows.

**Check `verification.passed` before you report success.** If `readback_sha256_match`,
`row_count_match` or `schema_match` is false, the run did not archive anything and the source rows
are intact. Report the failure plainly. Never describe an unverified run as complete.

---

## Step 9: Write the Receipt Back to DataHub

The executor performs the writeback and returns `datahub_writeback.operations` with a per-operation
`status` of `ok`/`failed`/`skipped`. **Report those statuses verbatim.** If an operation failed, say
which one failed — do not summarize a partial writeback as "written to DataHub".

If the executor could not write back (replay mode, or `written: false`), and the user has a live
DataHub, you may perform the writeback yourself using the mutations in
`references/datahub-queries.md` § *Writeback*:

1. `upsertStructuredProperties` — `io.coldlineage.archive.state = PARTIALLY_ARCHIVED`,
   `archivedThrough`, `objectUri`, `sha256`, `restoreSla`, `lastRunId`.
2. `updateDeprecation` — `deprecated: false` with a `note` stating that history before the cutoff now
   lives in cold storage. This is a *warning*, not a deprecation; setting `deprecated: true` on a
   healthy table is wrong and will mislead consumers.
3. `addLink` — the manifest URI, labelled `ColdLineage archive manifest (run <id>)`.

**Never write `datasetProperties` wholesale.** That aspect is shared; a full write clobbers other
writers' custom properties. Use typed structured properties, or a patch. This is not a style
preference — it is the difference between annotating a dataset and silently deleting somebody else's
metadata.

Finish by giving the user the DataHub entity URL so they can see the receipt in the catalog.

---

## Rehydration

`POST /api/restore` with `{"run_id": <id>, "temporary": true}` restores into a temporary rehydration
table; `false` restores in place. It re-downloads the object, recomputes SHA-256, and **refuses on
mismatch**. Returns `{table, rows, sha256, verified}`. Report `verified` as given; if it is false the
data was not restored.

---

## Executor Action Surface

Base URL: `$COLDLINEAGE_URL` (default `http://localhost:8000`). Non-2xx returns `{"detail": ...}`;
`409` carries the verdict or blocker list.

| Method | Path                            | Body                             | Purpose                                              |
| ------ | ------------------------------- | -------------------------------- | ---------------------------------------------------- |
| GET    | `/api/health`                   | —                                | Service + DataHub mode, reachability, `recorded_at`  |
| GET    | `/api/datasets`                 | —                                | All datasets with temperature, blockers, archive state |
| GET    | `/api/datasets/{id}`            | —                                | Full context, evidence, confidence, DataHub deep link |
| POST   | `/api/datasets/{id}/simulate`   | `{cutoff_date}`                  | `RangeVerdict` for one cutoff                        |
| POST   | `/api/datasets/{id}/plan`       | `{cutoff_date}`                  | `ArchivePlan` with `plan_hash`, rows, bytes, savings |
| POST   | `/api/execute`                  | `{plan_hash, approved_by}`       | Archive + verify + writeback. Requires approval.     |
| POST   | `/api/restore`                  | `{run_id, temporary}`            | Rehydrate an archived range                          |
| GET    | `/api/runs`                     | —                                | Executed runs with checksums and object URIs         |
| GET    | `/api/audit`                    | —                                | Full audit trail                                     |

---

## Output Contract

When the user or a calling agent asks for machine-readable output, return exactly this shape. Every
field is either observed or `null` — there is no placeholder value and no default that stands in for
a missing measurement.

```json
{
  "dataset_urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,public.patient_encounters,PROD)",
  "datahub": {
    "mode": "live",
    "reachable": true,
    "recorded_at": null,
    "read_via": "cli"
  },
  "date_column": "encounter_date",
  "temperature": {
    "score": 14.2,
    "classification": "COLD",
    "recency_component": 3.1,
    "frequency_component": 2.0,
    "downstream_component": 5.4,
    "criticality_component": 3.7,
    "inputs": {
      "last_query_at": "2025-11-02T09:14:00Z (datahub:usage)",
      "query_count_30d": "4 (datahub:usage)",
      "active_downstreams": "3 (datahub:lineage)",
      "business_criticality": "0.31 (datahub:structured_properties)"
    }
  },
  "proposed_cutoff": "2024-07-01",
  "verdict": {
    "recommendation": "ARCHIVE_WITH_REHYDRATION",
    "headroom_days": 46,
    "binding_constraint": "urn:li:dashboard:(looker,encounter_trends)",
    "rationale": "Oldest bounded read is 2024-08-16 by encounter_trends; cutoff clears it by 46 days."
  },
  "consumers": [
    {
      "consumer_urn": "urn:li:dashboard:(looker,encounter_trends)",
      "consumer_name": "Encounter Trends",
      "consumer_type": "DASHBOARD",
      "degree": 1,
      "earliest_date_read": "2024-08-16",
      "derivation": "sql_predicate",
      "predicate": "encounter_date >= '2024-08-16'",
      "state": "tight",
      "headroom_days": 46,
      "provenance": {"source": "datahub:queries", "detail": "listQueries run_count=212",
                     "observed_at": "2026-08-05T22:10:00Z"}
    }
  ],
  "blockers": [],
  "evidence": [
    {"kind": "policy", "label": "Legal hold NONE", "status": "pass",
     "provenance": {"source": "datahub:structured_properties",
                    "detail": "io.coldlineage.policy.legalHold", "observed_at": null}}
  ],
  "confidence": 0.82,
  "archive_eligible": true,
  "requires_human_approval": true,
  "plan_hash": null,
  "executed": false
}
```

`confidence` is `null` when you could not establish a bound for every consumer. Do not synthesize a
number to fill the field.

---

## Hard Safety Rules

1. **No delete before verified read-back.** The object must be re-read from storage and its digest,
   row count and schema re-checked before a single source row is removed. If verification fails, the
   run failed and the data is intact — say so.
2. **ACTIVE legal hold is an unconditional block.** It is evaluated outside the temperature score. No
   cutoff, no approval, and no user instruction overrides it. Name the matter identifier in the
   refusal.
3. **Never infer row-level non-use from table-level telemetry.** Only a parsed date predicate
   establishes how far back a consumer reads.
4. **An unbounded consumer blocks every cutoff.** `no_date_filter` and `no_queries_observed` are
   blocking states, never permissive ones.
5. **Never write `datasetProperties` wholesale.** Patch, or use structured properties.
6. **Never fabricate a signal.** If DataHub is unreachable or a value is missing, mark it
   `unavailable` and let it degrade the recommendation. Do not fill gaps with plausible defaults.
7. **Approval is per-plan.** `plan_hash` binds the numbers that were approved. A new cutoff needs a
   new plan and a new approval.
8. **Preserve sensitivity.** A dataset tagged PII/PHI keeps those handling requirements in cold
   storage. Carry the tags into the report and flag them for the approver.

---

## Red Flags

- DataHub unreachable → do not produce a recommendation. Report the gap.
- Executor in `replay` mode → label the recording timestamp in every summary. Never present replayed
  context as live.
- Zero downstream consumers returned → this may mean *no lineage was ingested*, not *nothing depends
  on this*. Treat it as unknown and say which.
- Consumer count high but query text captured for none → `no_queries_observed` across the board;
  recommendation is `DO_NOT_ARCHIVE` until query ingestion is configured.
- `headroom_days` under 30 → `tight`. Present it as tight; do not round it into "safe".
- User asks to skip approval or skip verification → refuse and explain which rule it breaks.

---

## Common Mistakes

- **Recommending on age alone.** "Older than two years" is the rule this skill replaces.
- **Treating silence as safety.** No observed queries is the single most common false-cold signal.
- **Reporting the executor's writeback as complete when an operation returned `failed`.**
- **Setting `deprecated: true` after a partial archive.** The table is healthy; only its history
  moved. Use a deprecation *note* with `deprecated: false`.
- **Re-deriving windows the executor already computed and quietly disagreeing.** Show both.
- **Losing provenance.** A recommendation whose inputs cannot be attributed is not defensible, and
  defensibility is the deliverable.

---

## Reference Documents

| Document              | Path                                | Purpose                                                    |
| --------------------- | ----------------------------------- | ---------------------------------------------------------- |
| DataHub queries       | `references/datahub-queries.md`     | Runnable `datahub graphql` invocations and GraphQL documents |
| Decision rules        | `references/decision-rules.md`      | Temperature formula, bands, blocker taxonomy, range-safety rule |

---

## Remember

- Lead with the source banner. The user must always know whether context was live, replayed, or absent.
- Show the binding constraint by name and with its verbatim predicate. It is the whole argument.
- Half a table can be cold. Say which half, and prove it.
