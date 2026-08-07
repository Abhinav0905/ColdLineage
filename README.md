# ColdLineage

**Keep the context hot. Move the data cold.**

ColdLineage is a DataHub-native agentic control plane for governed data tiering. It does not archive data simply because it is old. It combines DataHub context, warehouse usage, retention rules and lineage to explain *why* a historical range is safe to move, simulates downstream impact, requires approval, performs a real archive to Parquet, verifies the result, removes the hot rows, supports real rehydration and writes archive provenance back to DataHub.

## Hackathon demo

The included synthetic healthcare estate contains three deliberately different cases:

1. `patient_encounters` — cold enough to archive, with downstream dependencies that are safe within the chosen horizon.
2. `claims_history` — old but blocked by a seven-year retention/legal-hold case.
3. `care_events_live` — actively queried and correctly kept hot.

That contrast is intentional. The demo proves the agent is not a glorified `DELETE WHERE date < ...` script.

## What is implemented

- Data Temperature Map
- Evidence Graph
- policy and legal-hold blocker checks
- downstream "What if?" simulation
- human approval gate
- real PostgreSQL -> Parquet archive
- real MinIO/S3-compatible cold storage
- SHA-256 object verification
- source-row removal only after archive write succeeds
- real restore into a temporary rehydration table
- audit trail
- DataHub writeback adapter
- reusable DataHub skill: `assess-data-temperature`
- polished Next.js demo UI

## Stack

- Next.js 15 / React 19
- FastAPI
- PostgreSQL 16
- MinIO / S3 API
- Pandas + PyArrow
- SQLAlchemy
- DataHub context + writeback adapter

## Quick start

Requirements: Docker Desktop and Python 3.11+.

```bash
git clone <your-repo-url>
cd coldlineage
docker compose up -d --build

# Seed the synthetic healthcare estate from your host machine.
pip install sqlalchemy psycopg[binary] faker
DATABASE_URL=postgresql+psycopg://coldlineage:coldlineage@localhost:5433/coldlineage python scripts/seed.py
```

Open:

- UI: http://localhost:3000
- FastAPI docs: http://localhost:8000/docs
- MinIO console: http://localhost:9001 (`minioadmin` / `minioadmin`)

Run the smoke test:

```bash
python scripts/smoke_test.py
```

## Three-minute demo path

1. Open **Overview**. Point out the three temperature classes.
2. Open **Candidates** and select `patient_encounters`.
3. Show the Evidence Graph: stale usage, policy, lineage and sensitive-data context.
4. Set the cutoff to `2024-07-01` and click **Simulate**.
5. Show the downstream impact and the recommended `ARCHIVE_WITH_REHYDRATION` result.
6. Click **Approve & execute**.
7. Show the generated S3 URI and SHA-256 digest.
8. Open **Restore** and rehydrate the run.
9. Open **Audit** and show preview -> simulation -> execution -> restore as a single trace.
10. In a live DataHub environment, show the `coldlineage.*` custom properties written back to the dataset.

## Temperature score

The score is deterministic and intentionally inspectable. Higher is hotter:

- 42% access recency
- 28% query frequency
- 18% active downstream dependency count
- 12% business criticality

Legal holds and retention policy are separate blockers rather than hidden inside the temperature number. This makes explanations defensible.

## Archive safety model

ColdLineage uses a constrained executor instead of giving an LLM arbitrary database permissions.

Before hot rows are removed, it:

1. selects the candidate rows
2. serializes them to Parquet
3. calculates SHA-256
4. writes the object to MinIO/S3
5. writes a JSON manifest
6. only then deletes the hot rows

Restoration downloads the archive object, recalculates SHA-256 and rejects the restore if the digest differs.

## DataHub integration

Set these environment variables in the root shell before `docker compose up`:

```bash
export DATAHUB_GMS_URL=http://host.docker.internal:8080
export DATAHUB_TOKEN=<token>
export DEMO_MODE=false
```

The application treats DataHub as the context and audit system, not as the row-moving engine. DataHub supplies dataset identity, lineage, ownership, classifications, policy context and usage signals. ColdLineage performs constrained archive/restore operations and writes the result back.

`backend/app/services/datahub.py` is isolated intentionally. DataHub deployment/API details differ between OSS and Cloud versions, so the hackathon demo can run fully in `DEMO_MODE=true` while the adapter can be pointed at the hackathon DataHub endpoint without touching the archive engine.

## DataHub Skill

See:

`datahub-skill/coldlineage/SKILL.md`

The skill defines the context requirements, decision procedure, safety rules and machine-readable output contract for an `assess-data-temperature` capability.

## Production extensions after the hackathon

- ingest real warehouse query histories (Snowflake/BigQuery/Postgres pg_stat_statements)
- partition-level rather than table-level heat scores
- DataHub MCP/Agent Context Kit as the primary context-fetch path
- owner approval through Slack/Teams
- policy-as-code integration
- KMS encryption metadata and ACL replication
- Iceberg/Delta-aware detach/reattach executors
- transparent query federation against cold objects
- scheduled restore drills
- savings model from actual cloud billing metadata

## Why DataHub matters

Without DataHub, the executor can only see age and rows. With DataHub, it can ask whether an apparently cold partition still feeds a dashboard, ML model, compliance extract, data owner policy or legal constraint. That difference is the product.

## License

Apache-2.0 recommended for the hackathon/open-source submission.
