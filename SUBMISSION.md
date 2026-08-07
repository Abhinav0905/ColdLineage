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
`skills/assess-data-temperature/` is a loadable DataHub Skill and is the reasoning layer.

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

516,088 of 1,100,000 rows archived to 11 verified Parquet parts, source truncated only after
read-back passed, all four DataHub writeback operations landing on the entity, and a
checksum-verified restore of all 516,088 rows. Artifacts from that exact run are committed in
`examples/`, so judges can assess it without running anything.

Honest about scale: at demo size the saving is about a cent a month, and we report the measured
figure rather than inflating it. What transfers is the fraction — **46.9% of a live table was
provably unread** — not the dollars.

## Upstream contribution

We found DataHub's own `datahub-enrich` skill documents `upsertStructuredProperties` incorrectly;
the documented example fails schema validation three ways. Proof and a draft PR are in
`CONTRIBUTING-UPSTREAM.md`, alongside a proposal to contribute this skill upstream —
`datahub-skills` currently has no skill covering cost, storage, tiering, retention or archival.
