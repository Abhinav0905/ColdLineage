# ColdLineage Skills

A loadable agent skill for DataHub-governed data tiering.

```
skills/
└── assess-data-temperature/
    ├── SKILL.md                          # the decision procedure and the action surface
    └── references/
        ├── datahub-queries.md            # runnable GraphQL, verified against the DataHub schema
        └── decision-rules.md             # temperature formula, blockers, range-safety rule
```

Layout, frontmatter and plugin manifests follow
[datahub-project/datahub-skills](https://github.com/datahub-project/datahub-skills), so this skill
drops into that repo unchanged. See `CONTRIBUTING-UPSTREAM.md` at the repo root.

---

## What `assess-data-temperature` does

DataHub can tell you a table is cold. It cannot tell you that **half** a table is cold — its metadata
model is dataset- and column-level, so there is nowhere to express "rows before 2024-07-01 are cold
while the last 90 days are hot". And it cannot move a byte.

This skill closes both gaps:

1. **Reads context from DataHub** — lineage for downstream consumers, `usageStats` and `listQueries`
   for access telemetry, `io.coldlineage.policy.*` structured properties for retention floor and
   legal hold, plus tags, ownership, domain, schema and deprecation. Every value it carries forward
   is labelled with where it came from.
2. **Derives each consumer's real history window** by parsing the date predicate out of that
   consumer's actual SQL. A dashboard that filters `encounter_date >= '2024-08-16'` is bounded. One
   with no date predicate is unbounded and blocks every cutoff.
3. **Scores temperature** deterministically — 42% access recency, 28% query frequency, 18% active
   downstream count, 12% declared business criticality. Policy blockers are evaluated separately, so
   a cold-looking table under legal hold can never be archived by arithmetic.
4. **Simulates a cutoff** against every consumer and names the single binding constraint.
5. **Stops for human approval**, showing rows, bytes, saving, the consumer table, and the verbatim
   SQL predicate that sets the limit.
6. **Executes through the ColdLineage executor**, which writes Parquet to object storage, reads the
   object back and re-verifies digest, row count and schema **before** deleting a single source row.
7. **Writes the receipt back to DataHub** — `io.coldlineage.archive.*` structured properties, a
   deprecation *note* (with `deprecated: false`, because the table is healthy and only its history
   moved), and an institutional-memory link to the manifest.

The skill never touches a database or an object store itself. Every physical action goes through the
executor's HTTP API, which enforces the approval gate and the verify-before-delete ordering.

### What it will refuse to do

- Produce a recommendation when DataHub is unreachable. An archive decision without catalog context
  is the age-based `DELETE WHERE date < …` this project exists to replace.
- Treat a missing signal as a cold signal. Unknown inputs widen the score into a band and block
  `SAFE_TO_ARCHIVE`; they never quietly contribute zero.
- Clear an `ACTIVE` legal hold for any reason, including explicit user instruction.
- Report a run as complete when read-back verification failed.
- Write `datasetProperties` wholesale, which would clobber other writers' metadata.

---

## Install

### As a skill

```bash
npx skills add Abhinav0905/ColdLineage
```

### As a Claude Code plugin

```bash
claude plugin marketplace add Abhinav0905/ColdLineage
claude plugin install coldlineage@coldlineage
```

### From a local clone

```bash
git clone https://github.com/Abhinav0905/ColdLineage.git
claude plugin marketplace add ./ColdLineage
claude plugin install coldlineage@coldlineage
```

Verify it loaded:

```
/assess-data-temperature
```

---

## Prerequisites

**DataHub.** Either the MCP server (preferred — the skill uses it for single-entity reads) or the
CLI:

```bash
pip install 'acryl-datahub>=1.4.0'
export DATAHUB_GMS_URL="http://localhost:8080"
export DATAHUB_GMS_TOKEN="<personal-access-token>"
datahub get --urn "urn:li:corpuser:datahub"     # connectivity check
```

**Structured property definitions.** The policy properties the skill reads and the archive properties
it writes must exist in your catalog first:

```bash
datahub properties upsert -f backend/app/datahub/properties.yaml
```

This defines `io.coldlineage.policy.{retentionYears,legalHold,legalHoldMatter,businessCriticality}`
and `io.coldlineage.archive.{state,archivedThrough,objectUri,sha256,restoreSla,lastRunId}`.

**ColdLineage executor** — required for planning, execution, verification and restore:

```bash
docker compose up -d --build
export COLDLINEAGE_URL="http://localhost:8000"     # default
curl -s "$COLDLINEAGE_URL/api/health"
```

Without the executor the skill still assesses and explains, but it cannot plan or move anything, and
it will say so rather than pretending otherwise.

---

## DataHub modes

`GET /api/health` reports which mode the executor is in. There are exactly two, and no mode that
invents context:

| Mode     | Meaning                                                                                   |
| -------- | ----------------------------------------------------------------------------------------- |
| `live`   | Talking to a real GMS at `gms_url`. Reads lineage/usage/queries/properties, writes back.   |
| `replay` | Serving verbatim GMS responses recorded into committed cassettes. `recorded_at` is the recording timestamp. |

Replay exists so a reviewer can run the whole demo without standing up DataHub, **without** the
system ever claiming a connection it does not have. The skill opens every report with a banner naming
the mode, and in replay it states the recording timestamp out loud.

---

## Permissions

The DataHub principal needs:

| Operation                    | Privilege                    |
| ---------------------------- | ---------------------------- |
| Read entity, lineage, usage  | View Entity Page             |
| Read/write structured props  | Edit Structured Properties   |
| Write deprecation note       | Edit Deprecation             |
| Add manifest link            | Edit Links                   |

Read-only is enough for assessment. Writeback needs the three edit privileges.

---

## Development

Validate the skill before committing:

```bash
python3 - <<'PY'
import json, pathlib, yaml
p = pathlib.Path("skills/assess-data-temperature/SKILL.md")
raw = p.read_bytes()
assert raw[:3] == b"---", "frontmatter must start at byte 0"
fm = yaml.safe_load(raw.decode().split("---", 2)[1])
assert fm["name"] == p.parent.name, (fm["name"], p.parent.name)
assert fm.get("description"), "description is required"
for j in (".claude-plugin/plugin.json", ".claude-plugin/marketplace.json"):
    json.loads(pathlib.Path(j).read_text())
print("OK", fm["name"], len(raw.decode().splitlines()), "lines")
PY
```

Keep `SKILL.md` under ~500 lines. Depth goes in `references/`, which the agent loads on demand.
