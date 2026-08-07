# Contributing `datahub-tiering` upstream

A concrete proposal to contribute this repository's skill to
[datahub-project/datahub-skills](https://github.com/datahub-project/datahub-skills) as a new
catalog-interaction skill, `datahub-tiering`.

---

## 1. The gap

Upstream ships twelve skill directories:

```
datahub-connector-planning   datahub-mfe-configure-app   datahub-setup
datahub-connector-pr-review  datahub-mfe-create-app      load-standards
datahub-enrich               datahub-quality             shared-references
datahub-lineage              datahub-search              using-datahub
```

Five of those are catalog-interaction skills (`search`, `enrich`, `lineage`, `quality`, `setup`).
Between them they cover discovery, metadata mutation, dependency tracing, assertions and incidents,
and connection configuration.

**None of them covers cost, storage, tiering, retention, archival, or data lifecycle.** Verified by
code search against `datahub-project/datahub-skills` on 2026-08-06:

| Search term      | Hits in repo | Where                                                        |
| ---------------- | ------------ | ------------------------------------------------------------ |
| `tiering`        | 0            | —                                                            |
| `archive`        | 0            | —                                                            |
| `archival`       | 0            | —                                                            |
| `lifecycle`      | 0            | —                                                            |
| `retention`      | 2            | `standards/main.md`, `standards/lineage.md`                  |
| `cold storage`   | 1            | `standards/source_types/document_sources.md`                 |
| `storage cost`   | 1            | `standards/performance.md`                                   |

Every hit is in `standards/`, which is connector-*development* guidance for people writing ingestion
sources. Not one is in a skill, and none of them concerns deciding what to do with data that has gone
cold.

This is a real gap rather than a deliberate omission. "Can we archive this table?" is one of the
most common questions a data platform owner asks a catalog, and the catalog already holds every input
needed to answer it — lineage, usage, query history, ownership, and (since structured properties)
retention and legal-hold policy. The reason no skill answers it is that the question needs a
*range-level* answer, and DataHub's entity model is dataset- and column-level. That is precisely the
gap this skill fills.

`datahub-tiering` also composes cleanly with what already exists: it consumes `datahub-lineage`'s
traversal patterns and writes through `datahub-enrich`'s structured-property and deprecation
mutations, rather than duplicating either.

---

## 2. What gets contributed

The skill, generalized. Not the ColdLineage backend, the UI, or the Docker stack.

| This repo                                                          | Upstream                                                          |
| ------------------------------------------------------------------ | ------------------------------------------------------------------ |
| `skills/assess-data-temperature/SKILL.md`                          | `skills/datahub-tiering/SKILL.md`                                  |
| `skills/assess-data-temperature/references/datahub-queries.md`     | `skills/datahub-tiering/references/datahub-queries.md`             |
| `skills/assess-data-temperature/references/decision-rules.md`      | `skills/datahub-tiering/references/decision-rules.md`              |
| —                                                                   | `skills/datahub-tiering/README.md` *(new — every upstream skill has one)* |
| —                                                                   | `skills/datahub-tiering/templates/tiering-assessment.template.md` *(new)* |
| —                                                                   | `skills/datahub-tiering/evaluations/*.json` *(new — 3 cases)*      |
| —                                                                   | `commands/catalog-tiering.md` *(new — matches `commands/catalog-lineage.md`)* |

Plus three edits to existing files:

- `README.md` — a `#### Tiering` section under *Catalog interaction skills*, matching the house
  format (one-paragraph description then a fenced block of example prompts).
- `skills/using-datahub/SKILL.md` — one row in the Skill Routing Table:
  `| **Storage cost, archival, retention, tiering** (can we archive X, what is X costing) | **Tiering** | \`/datahub-tiering\` |`
  and a disambiguation rule: *"what depends on X"* → Lineage; *"can we archive X"* → Tiering.
- `.claude-plugin/plugin.json` / `marketplace.json` — extend the description string. **Do not touch
  `version`**; Release Please owns it.

### Changes required to make it upstream-appropriate

1. **Rename.** `name:` must equal the folder name, so both become `datahub-tiering`. Keep
   `assess-data-temperature` as a trigger phrase inside `description`.
2. **Decouple the executor.** Upstream cannot depend on a ColdLineage service. Restructure `SKILL.md`
   into two tiers:
   - **Assess** (default, no external service) — read DataHub, derive consumer windows, score
     temperature, evaluate blockers, compute the safe cutoff, and produce the assessment report.
     This is the whole analytical contribution and it needs nothing but DataHub.
   - **Execute** (optional) — if a `COLDLINEAGE_URL`-style executor endpoint is configured, drive
     plan → approve → execute → verify → writeback. Presented as a pluggable interface with the HTTP
     contract documented, so any org can point it at their own mover. ColdLineage becomes the
     reference implementation, named as an example rather than a requirement.
3. **Generalize the property namespace.** Ship `io.datahub.lifecycle.*` as the default and document
   `io.coldlineage.*` as one deployment's namespace. Read the namespace from configuration.
4. **Add evaluations.** Match the format in `skills/datahub-connector-planning/evaluations/*.json`.
   Three cases that mirror this repo's demo estate, because they are exactly the cases that separate
   a real decision procedure from an age-based rule:
   - `assess-cold-with-safe-downstreams.json` — cold, every consumer bounded → `SAFE_TO_ARCHIVE`.
   - `assess-blocked-by-legal-hold.json` — cold but `legalHold = ACTIVE` → refuse, name the matter.
   - `assess-blocked-by-unbounded-consumer.json` — cold, one consumer with no date predicate →
     `DO_NOT_ARCHIVE`, name the consumer.
5. **Fix the GraphQL errors this work surfaced.** While validating documents against a schema built
   from all 35 SDL files in `datahub-graphql-core/src/main/resources/`, the
   `upsertStructuredProperties` example in `skills/datahub-enrich/references/mutation-reference.md`
   fails validation with three errors:

   ```
   Field 'structuredPropertyInputs' is not defined by type 'UpsertStructuredPropertiesInput'.
     Did you mean 'structuredPropertyInputParams'?
   Field 'UpsertStructuredPropertiesInput.structuredPropertyInputParams' of required type
     '[StructuredPropertyInputParams!]!' was not provided.
   Field 'upsertStructuredProperties' of type 'StructuredProperties!' must have a selection of subfields.
   ```

   So: the input field is `structuredPropertyInputParams`; `values` is `[PropertyValueInput!]!`
   (`{ stringValue: "…" }` / `{ numberValue: 1.0 }`), not a list of bare strings; and the mutation
   returns `StructuredProperties!` and therefore requires a selection set.

   As written, that example cannot succeed against any GMS. **Send this as a separate `fix:` PR
   first** — it is small, independently useful, and lands without waiting on review of a new skill.

---

## 3. Exact steps

### Prerequisites

```bash
gh auth status
pip install pre-commit
```

### PR 1 — the mutation-reference fix (send first)

```bash
gh repo fork datahub-project/datahub-skills --clone --remote
cd datahub-skills
pre-commit install

git checkout -b fix/structured-properties-mutation-shape
# edit skills/datahub-enrich/references/mutation-reference.md
pre-commit run --all-files
git commit -am "fix: correct upsertStructuredProperties input shape in mutation reference"
git push -u origin fix/structured-properties-mutation-shape

gh pr create --repo datahub-project/datahub-skills \
  --title "fix: correct upsertStructuredProperties input shape in mutation reference" \
  --body-file /tmp/pr1-body.md
```

### PR 2 — the new skill

```bash
cd datahub-skills
git checkout main && git pull upstream main
git checkout -b feat/datahub-tiering-skill

mkdir -p skills/datahub-tiering/{references,templates,evaluations}
cp <coldlineage>/skills/assess-data-temperature/SKILL.md              skills/datahub-tiering/SKILL.md
cp <coldlineage>/skills/assess-data-temperature/references/*.md       skills/datahub-tiering/references/

# 1. In SKILL.md frontmatter: name: assess-data-temperature -> datahub-tiering
# 2. Split the body into Assess (no executor) and Execute (optional endpoint)
# 3. Swap io.coldlineage.* -> io.datahub.lifecycle.* with a configurable namespace note
# 4. Write skills/datahub-tiering/README.md
# 5. Write commands/catalog-tiering.md, modelled on commands/catalog-lineage.md
# 6. Add the three evaluations/*.json
# 7. Add the README.md section and the using-datahub routing row
# 8. Extend the plugin.json / marketplace.json descriptions -- do NOT edit version

pre-commit run --all-files          # prettier + markdownlint-cli2 + ruff
bash tests/run-tests.sh             # existing suite must stay green

git add -A
git commit -m "feat: add datahub-tiering skill for storage lifecycle decisions"
git push -u origin feat/datahub-tiering-skill

gh pr create --repo datahub-project/datahub-skills \
  --title "feat: add datahub-tiering skill for storage lifecycle decisions" \
  --body-file /tmp/pr2-body.md
```

### Rules that will fail CI if broken

- **PR title must be a Conventional Commit.** Enforced by the `Lint PR Title` check
  (`amannn/action-semantic-pull-request`). `feat:` for the skill, `fix:` for the mutation reference.
  The title becomes the squash-merge commit message.
- **Never hand-edit `version` in `plugin.json` or `.release-please-manifest.json`**, never hand-write
  `CHANGELOG.md`, never create tags. Release Please owns all of it.
- **`pre-commit run --all-files` must pass** — prettier, markdownlint-cli2, ruff.
- **`name:` in frontmatter must equal the directory name**, and the frontmatter must start at byte 0.

---

## 4. Draft PR 2

**Title**

```
feat: add datahub-tiering skill for storage lifecycle decisions
```

**Body**

---

### What

Adds `skills/datahub-tiering`, a sixth catalog-interaction skill that answers *"is this data still
worth keeping hot, and what exactly is safe to move?"* using DataHub lineage, usage, query history
and structured properties.

### Why

The five existing catalog skills cover discovery, mutation, lineage, quality and setup. A code search
of this repo turns up zero hits for `tiering`, `archive`, `archival` or `lifecycle`, and the only
hits for `retention`, `cold storage` and `storage cost` are in `standards/`, which is
connector-development guidance rather than a skill.

Storage lifecycle is a top-of-list question for platform owners, and DataHub already holds every
input needed to answer it well. What blocks a good answer today is that the answer has to be
*range-level* — "rows before 2024-07-01 are cold, the last 90 days are hot" — while the entity model
is dataset- and column-level. This skill supplies the missing layer: it derives each downstream
consumer's real read window by parsing the date predicate out of that consumer's SQL from
`listQueries`, and treats the oldest such bound as the archive floor.

### What it does

1. Reads dataset context — `schemaMetadata`, `ownership`, `domain`, `tags`, `deprecation`,
   `structuredProperties`.
2. Traverses downstream consumers with `searchAcrossLineage(direction: DOWNSTREAM)`.
3. Derives each consumer's history window from `listQueries` SQL. A consumer with no date predicate
   is unbounded and blocks every cutoff.
4. Scores temperature deterministically — 42% access recency, 28% query frequency, 18% active
   downstream count, 12% declared business criticality. Policy blockers (legal hold, retention floor)
   are evaluated **outside** the score, so a cold-looking asset under litigation hold can never be
   archived by arithmetic.
5. Produces a cutoff with per-consumer headroom and names the single binding constraint, quoting its
   verbatim SQL predicate.
6. Optionally drives an external executor through plan → human approval → execute → verify →
   writeback, if one is configured. The executor is a documented HTTP interface, not a dependency;
   with none configured the skill assesses and explains and says so.

### Design choices worth reviewing

- **Unknown is never permissive.** A consumer with no captured queries is `unknown`, and `unknown`
  blocks. Missing telemetry widens the temperature score into a band rather than contributing zero,
  because "unmeasured" scoring as "cold" is the failure mode that gets data deleted.
- **Deprecation note, `deprecated: false`.** After a partial archive the table is healthy; only its
  history moved. Setting `deprecated: true` would hide a live asset from search across the catalog.
  The `note` is the payload.
- **Structured properties, never `datasetProperties` wholesale.** A full write to that shared aspect
  clobbers other writers.

### Structure

```
skills/datahub-tiering/
├── SKILL.md                                  # ~460 lines
├── README.md
├── references/
│   ├── datahub-queries.md                    # verified GraphQL documents
│   └── decision-rules.md                     # formula, bands, blockers, range-safety rule
├── templates/tiering-assessment.template.md
└── evaluations/                              # 3 cases: safe, legal-hold block, unbounded consumer
commands/catalog-tiering.md
```

### GraphQL verification

Every document in `references/datahub-queries.md` is validated programmatically against a schema
built from all 35 SDL files in `datahub-graphql-core/src/main/resources/` on the `main` branch of
`datahub-project/datahub` — `graphql-core`'s `build_schema` + `validate`, zero errors across all ten
documents. Covers `searchAcrossLineage`, `usageStats(range: TimeRange)`, `listQueries`,
`upsertStructuredProperties`, `removeStructuredProperties`, `updateDeprecation`,
`batchUpdateDeprecation`, `addLink` and `removeLink`.

That same pass is what found the errors in `skills/datahub-enrich/references/mutation-reference.md`,
fixed separately in #<PR-1>.

### Also changed

- `README.md` — `#### Tiering` section under *Catalog interaction skills*.
- `skills/using-datahub/SKILL.md` — routing row and a Lineage-vs-Tiering disambiguation rule.
- `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json` — description text only; `version`
  untouched.

### Testing

- `pre-commit run --all-files` clean.
- `bash tests/run-tests.sh` green.
- Skill exercised end to end against a local DataHub OSS quickstart (`v1.4.x`) with a seeded Postgres
  estate covering all three evaluation cases.

### Provenance

Extracted from [ColdLineage](https://github.com/Abhinav0905/ColdLineage), built for *Build with
DataHub: The Agent Hackathon*. Contributed under Apache-2.0 to match this repository.

---

## 5. Follow-ups to offer, not to bundle

Keep PR 2 reviewable. Offer these in the PR description as future work:

- A `datahub-cost` sibling skill for estate-wide spend attribution by domain and owner.
- Partition-level rather than range-level assessment for Iceberg and Delta tables, where the manifest
  already carries per-partition row counts and byte sizes.
- Feeding `io.datahub.lifecycle.archivedThrough` into `datahub-search` result rendering, so a search
  hit visibly warns that an unqualified scan of that table returns partial history.
