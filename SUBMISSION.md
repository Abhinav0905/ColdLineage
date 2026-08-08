# Devpost submission text

**Project:** ColdLineage
**Category:** Agents That Do Real Work
**Repo:** https://github.com/Abhinav0905/ColdLineage
**License:** Apache-2.0

---

## What it does

DataHub can tell you a table is cold. It cannot tell you that *half* a table is cold — and it cannot
move a single byte. ColdLineage does both.

DataHub's model is dataset- and column-level, so it can say "this table is unused" but has no way to
express "rows before 2023 are cold while the last 90 days are hot". That leaves the biggest and
scariest case invisible: a **heavily-queried table whose first four years nobody has read in years**.
And nothing in DataHub touches the data plane — soft delete, DataHubGC and deprecation all operate
strictly on metadata.

ColdLineage decides whether a specific **date range inside a still-hot table** is safe to archive,
executes the move, and writes the receipt back into DataHub.

The decision reduces to one measurable thing: *how far back does each downstream consumer actually
read?* DataHub already holds each consumer's real SQL as Query entities. ColdLineage reads that SQL
out of DataHub and parses it with sqlglot, resolving the lower bound each consumer places on the
date column into a concrete date — handling literals, relative intervals, `BETWEEN`, casts,
`date_trunc`, and AND/OR boolean algebra. A cutoff is safe iff it is no later than the earliest
window across every consumer.

Everything unproven blocks. No date predicate, an `OR` with an unconstrained branch, unparseable
SQL, or unreadable lineage all resolve to *unbounded*, which refuses the archive. Absence of
evidence is not evidence of safety.

The demo estate makes the argument in two rows. `patient_encounters` scores **81.2 HOT** and is
genuinely in active use — yet 46.9% of it is provably unread and archivable. `lab_results` has **0
queries and 0 users in 30 days** — every dataset-level tool archives it — and is **blocked**, because
one HIPAA extract runs `WHERE performing_lab IS NOT NULL`: it *has* a filter, so a "is this query
filtered?" heuristic passes it, but with no date bound it reads every row ever written.
Dataset-level temperature gets both of them wrong.

## How it uses DataHub

DataHub is the source of truth for every decision input, read at request time — not a writeback
bolted onto a local database. Consumers come from `searchAcrossLineage`; their SQL from
`listQueries`; usage from `datasetUsageStatistics`; retention floor, legal hold and business
criticality from typed structured properties `io.coldlineage.policy.*`; schema, owners, domain and
tags from the entity. Every value carries a provenance tag rendered in the UI, so a missing input
appears as a visible gap rather than a plausible number.

After a verified archive it contributes four things back: six typed archive properties via
`upsertStructuredProperties`, a deprecation note carrying the cutoff and restore path via
`updateDeprecation`, a manifest link via `addLink`, and a `cold-tier-archived` tag. It deliberately
never writes `datasetProperties` wholesale, because that clobbers other writers' custom properties.
Ingestion uses the first-party `postgres` connector plus the `acryl-datahub` SDK.

**The agent.** `agent/` reads DataHub through the official **MCP Server** — `search`,
`get_lineage`, `get_dataset_queries`, `get_entities`, `list_schema_fields`,
`get_lineage_paths_between` — and acts through six constrained operations. The tool list is the
security model: it holds no database credentials, no object-store client, and no ability to issue
SQL, and the MCP server runs with mutation tools off, so it cannot write to the catalog on its own.
`coldlineage_execute_plan` blocks on a human; decline and the tool tells the model to stop rather
than letting it route around the gate. That is a stronger guarantee than any system-prompt
instruction, and it holds even if the model is wrong or the prompt is attacked.

It runs on **Claude or GPT**, which is the same argument made twice: if a system's safety depended
on which model you plugged in, it was never safe. One JSON Schema definition is converted to both
Anthropic and OpenAI tool formats, one system prompt serves both, and the approval gate lives in the
provider-neutral executor — so no driver can route around it. Tests assert that the two providers
are handed identical tool sets and that the executor imports no model SDK, which makes the claim
checkable rather than rhetorical. The OpenAI driver targets the bare Responses API, so
`OPENAI_BASE_URL` also points it at Azure, vLLM, Ollama or a self-hosted model. Forty-four tests
cover the agent offline, including an end-to-end run of the real loop, MCP bridge, executor and
approval gate against a scripted model — no key required to verify it.
`skills/assess-data-temperature/` encodes the same procedure as a loadable **DataHub Skill** for
anyone already in a skills runtime.

## The estate view, and the demo that makes the point

Every table's rows split three ways: **archivable** (past every consumer's reach and past the
retention floor), **held by policy** (provably unread, but inside the retention window), and
**in use** (a consumer can still reach it). Only the middle band answers to configuration.

Sweeping `io.coldlineage.policy.retentionYears` on `patient_encounters` (1,100,000 rows):

| retention | floor | archivable | held by policy | in use |
|---|---|---|---|---|
| 14 years | 2012-08-08 | 0.0% | 60.6% | **39.4%** (433,161) |
| 4 years | 2022-08-08 | 41.7% | 18.9% | **39.4%** (433,161) |
| 2 years | 2024-08-08 | 60.6% | 0.0% | **39.4%** (433,161) |
| 4 months | 2026-04-08 | 60.6% | 0.0% | **39.4%** (433,161) |

433,161 rows in use, identically, at every setting — including the two where policy has stopped
binding at all. Between rows two and three the binding constraint flips from policy to evidence,
and after that the knob does nothing. **Retention is a floor, not a permission slip.** The band
that will not move is fixed by `WHERE e.event_date BETWEEN DATE '2024-01-01' AND CURRENT_DATE`,
read out of DataHub and parsed. Policy is set with `scripts/set_policy.py`, which writes to
DataHub rather than the UI offering a knob: retention belongs to a governance owner, not to the
tool that benefits from relaxing it.

## What DataHub does not model — verified by introspecting a live GMS

We introspected the running DataHub v1.7.0 GraphQL schema — 935 types, 169 mutations — rather
than asserting novelty from memory:

- **No temperature, tier, archive or retention concept exists.** Types matching `Tier`,
  `Temperat`, `Cold`, `Archiv`, `Retention`: none. `io.coldlineage.*` had nothing to reuse.
- **Deprecation is a boolean on the whole entity.** `Deprecation { deprecated, note,
  decommissionTime, replacement }` — no range, no row scope. *"Rows before 2024 are deprecated"*
  is inexpressible. That is the gap this project fills.
- **No mutation touches data.** All 169 operate on metadata; every `delete*` removes a catalog
  object, and `batchUpdateSoftDeleted` soft-deletes the *metadata record*, not rows.
- **DataHub stores consumer SQL but never interprets it.** `QueryStatement { value, language }`
  is the raw string. The raw material for the decision is already in the catalog; the derivation
  is not. That derivation is our contribution.

One nuance we will not overclaim: `PartitionSpec` *does* exist, with a `timePartition`. Its own
description is *"Information about the partition being profiled"* — it records what a profiling
run measured. DataHub has partition **vocabulary**; it has no partition **lifecycle**.

## What DataHub already does that we did not rebuild

Metadata Tests already finds cold tables by usage. Impact Analysis already validates blast radius.
Deprecation, soft delete and DataHubGC already exist, and DPG Media already cut Snowflake spend 25%
with them. We concede all of it. What is new is sub-table date-range granularity and touching the
data plane at all.

## The tech

FastAPI, PostgreSQL, MinIO/S3, Parquet + PyArrow, sqlglot, Next.js 15 / React 19, DataHub OSS v1.7.0
via GraphQL and the acryl-datahub SDK.

The executor is constrained to four operations — plan, simulate, execute, restore — and the
reasoning layer never receives database credentials. Approval is a **plan hash** binding dataset +
cutoff + row count + verdict; if live state drifted since the plan was shown, execution is refused
rather than proceeding against different data. Before any row is deleted, the archive is
**downloaded back from object storage** and its SHA-256 recomputed on the retrieved bytes, with row
count and schema asserted. Hashing the buffer you are about to upload proves nothing about what
landed.

## Verified

666,839 of 1,100,000 rows archived to 14 read-back-verified Parquet parts, the source truncated
only after the retrieved bytes re-hashed clean, **nine of nine** DataHub writeback operations
landing, the frozen archive resolving in DataHub as its own entity with 12 schema fields and COPY
lineage back to `coldlineage.public.patient_encounters`, then a checksum-verified restore to
1,100,000 rows spanning 2019-01-01 to 2026-08-05.

A live agent run is committed verbatim at `examples/agent-run-lab-results.md`: `gpt-5.6-sol`,
reasoning effort high, reading DataHub over MCP and **refusing** to archive the coldest table in
the estate. Artifacts from these exact runs are in `examples/`, so judges can assess the system
without running anything.

Test suites, all green: 25 end-to-end smoke checks · 20 window-extraction · 13 band-split ·
21 frozen-copy · 4 restore-integrity · 32 agent parity/trust-boundary · 12 agent-loop
integration.

**Bugs this development found and fixed, each by running the whole cycle rather than trusting a
response.** They are listed because they are the argument for the verification discipline, not
in spite of it: a restore that reported `verified: true` and restored nothing (a failed statement
aborted the transaction, so the commit became a silent rollback); an archive that became a
downstream consumer of its own source, blocking every future cutoff; a band chart that could
paint rows archivable that `/simulate` refuses; a retention floor that truncated fractional years,
so "four months" meant no floor at all; and a plan hash that was single-use forever, so anyone
running the demo twice hit a 409 on a plan they had legitimately re-issued.

Honest about scale: at demo size the saving is about a cent a month, and we report the measured
figure rather than inflating it. What transfers is the fraction — **46.9% of a live table was
provably unread** — not the dollars.

## Upstream contribution

We found DataHub's own `datahub-enrich` skill documents `upsertStructuredProperties` incorrectly;
the documented example fails schema validation three ways. Proof and a draft PR are in
`CONTRIBUTING-UPSTREAM.md`, alongside a proposal to contribute this skill upstream —
`datahub-skills` currently has no skill covering cost, storage, tiering, retention or archival.
