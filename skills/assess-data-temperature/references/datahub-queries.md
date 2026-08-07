# DataHub Queries for Temperature Assessment

Every document below is verified against the DataHub GraphQL schema
(`datahub-graphql-core/src/main/resources/{entity,search,properties,forms}.graphql`) and is runnable
as written. Where a field name differs from what you might expect, the difference is called out.

---

## Running Them

**Preferred: MCP.** If tools ending in `search`, `get_entities`, `get_lineage`, or `execute_graphql`
are available, use them. `execute_graphql(query=...)` takes the same documents shown here. MCP tool
names may be prefixed (`mcp__datahub__execute_graphql`) — match on the suffix.

**CLI fallback.**

```bash
export DATAHUB_GMS_URL="http://localhost:8080"
export DATAHUB_GMS_TOKEN="<personal-access-token>"

datahub -C skill=assess-data-temperature graphql \
  --query /tmp/dataset-context.graphql \
  --variables /tmp/vars.json \
  --format json
```

Three rules that will otherwise waste your time:

1. **Long inline `--query` strings break on macOS** — the shell hands the string to the CLI, which
   tries to treat it as a path and fails with `File name too long`. Write the document to a
   `.graphql` file and pass the path.
2. **Dataset URNs contain parentheses and commas.** Always pass them through a variables JSON file
   (`-v/--variables`), never interpolated into a shell-quoted inline query.
3. **Verify before you guess.** `datahub graphql --describe <operation> --recurse --format json`
   prints the real input shape for the server you are connected to.

Variables file:

```json
{ "urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,public.patient_encounters,PROD)" }
```

---

## Dataset context

One round trip for schema, ownership, domain, tags, deprecation, and the
`io.coldlineage.policy.*` structured properties. This is Step 2 of the skill.

```graphql
query datasetContext($urn: String!) {
  dataset(urn: $urn) {
    urn
    exists
    name
    platform {
      name
      properties { displayName }
    }
    properties {
      name
      qualifiedName
      description
      externalUrl
      lastModified { time }
    }
    subTypes { typeNames }

    ownership {
      owners {
        owner {
          ... on CorpUser { urn properties { displayName email } }
          ... on CorpGroup { urn properties { displayName } }
        }
        ownershipType { urn info { name } }
      }
    }

    domain { domain { urn properties { name } } }
    tags { tags { tag { urn properties { name } } } }
    glossaryTerms { terms { term { urn properties { name } } } }

    deprecation { deprecated note decommissionTime actor }
    institutionalMemory { elements { url label } }

    schemaMetadata {
      primaryKeys
      fields {
        fieldPath
        nullable
        nativeDataType
        isPartitioningKey
        isPartOfKey
        type
      }
    }

    structuredProperties {
      properties {
        structuredProperty {
          urn
          definition {
            qualifiedName
            displayName
            cardinality
            valueType { urn }
          }
        }
        values {
          ... on StringValue { stringValue }
          ... on NumberValue { numberValue }
        }
      }
    }
  }
}
```

**Field notes.**

- `schemaMetadata.fields[].type` is the enum `SchemaFieldDataType` (`DATE`, `TIME`, `STRING`,
  `NUMBER`, …). `nativeDataType` is the warehouse's own string (`timestamp without time zone`). Use
  both when choosing the date column — the enum for the coarse decision, the native type to confirm.
- `isPartitioningKey` is the strongest date-column signal there is. Prefer it over name heuristics.
- `platform.displayName` is deprecated; use `platform.properties.displayName` and fall back to
  `platform.name`.
- `PropertyValue` is a union of exactly `StringValue | NumberValue`. There is no date value type —
  DataHub stores `type: date` structured properties as strings on the read path, so
  `io.coldlineage.archive.archivedThrough` comes back under `stringValue`.

**Policy properties to pull out of the `structuredProperties` block:**

| `qualifiedName`                            | Read as | Meaning                                        |
| ------------------------------------------ | ------- | ---------------------------------------------- |
| `io.coldlineage.policy.retentionYears`     | number  | Years of history that must stay hot            |
| `io.coldlineage.policy.legalHold`          | string  | `NONE` \| `ACTIVE` \| `RELEASED`               |
| `io.coldlineage.policy.legalHoldMatter`    | string  | Matter ID justifying an `ACTIVE` hold          |
| `io.coldlineage.policy.businessCriticality`| number  | 0.0–1.0, feeds 12% of the temperature score    |

A missing property is **absent, not zero**. `retentionYears` absent means "no declared floor", which
is not the same as "floor is 0". Record it as `null` with provenance
`datahub:structured_properties` / `detail: "not set"`.

**Quick read of just the properties, without the full entity:**

```bash
datahub get --urn "$URN" --aspect structuredProperties
```

---

## Downstream consumers

`searchAcrossLineage` rather than the `lineage` field: it paginates, it returns `degree`, and it
filters by entity type in one call.

```graphql
query downstreamConsumers($urn: String!, $count: Int!) {
  searchAcrossLineage(
    input: {
      urn: $urn
      direction: DOWNSTREAM
      types: [DATASET, DASHBOARD, CHART, DATA_JOB, MLMODEL]
      query: "*"
      start: 0
      count: $count
      searchFlags: { skipCache: true }
    }
  ) {
    total
    searchResults {
      degree
      explored
      truncatedChildren
      entity {
        urn
        type
        ... on Dataset {
          name
          platform { name }
          properties { qualifiedName }
          deprecation { deprecated note }
          ownership { owners { owner { ... on CorpUser { urn } ... on CorpGroup { urn } } } }
        }
        ... on Dashboard { urn properties { name } platform { name } }
        ... on Chart { urn properties { name } platform { name } }
        ... on DataJob { urn properties { name } }
        ... on MLModel { urn name }
      }
    }
  }
}
```

Variables: `{"urn": "...", "count": 100}`.

**Field notes.**

- `direction` takes `UPSTREAM` or `DOWNSTREAM` only.
- `degree` is hops from the source. Degree-1 dataset consumers are the ones whose SQL you can parse
  directly; degree-2+ inherit their bound from the degree-1 dataset between them.
- `truncatedChildren: true` on any result means the graph was cut off. Raise `count`, or state in
  your report that the consumer set is incomplete — an incomplete consumer set cannot support
  `SAFE_TO_ARCHIVE`.
- `total: 0` means *no lineage edges were found*, which may mean lineage was never ingested. That is
  `unknown`, not `no consumers`.

CLI equivalent for a quick look (does not return `degree` in table form):

```bash
datahub lineage --urn "$URN" --direction downstream --hops 2 --format json
```

---

## Usage statistics

```graphql
query datasetUsage($urn: String!) {
  dataset(urn: $urn) {
    usageStats(range: MONTH) {
      aggregations {
        uniqueUserCount
        totalSqlQueries
        users { user { urn } count userEmail }
        fields { fieldName count }
      }
      buckets {
        bucket
        duration
        metrics { totalSqlQueries uniqueUserCount }
      }
    }
    statsSummary {
      queryCountLast30Days
      uniqueUserCountLast30Days
    }
  }
}
```

**Field notes.**

- The GraphQL field is `usageStats`. The underlying aspect is `datasetUsageStatistics` — that is the
  name you use with `datahub get --urn "$URN" --aspect datasetUsageStatistics`, not in GraphQL.
- `range` takes `TimeRange`: `DAY`, `WEEK`, `MONTH`, `QUARTER`, `YEAR`. The first argument
  (`resource`) is deprecated; omit it.
- `buckets[].bucket` is epoch milliseconds. The newest bucket with `metrics.totalSqlQueries > 0`
  gives you last-access recency. If every bucket is zero or `usageStats` is null, recency is
  **unknown** — feed it as unknown, not as "very old".
- `aggregations.fields` is per-column read counts. Useful colour, but it says nothing about *which
  rows* were read. Do not let it near the range decision.
- `statsSummary` is marked experimental upstream and may be absent on OSS deployments. Treat a null
  as absence of data.

---

## Consumer queries

The SQL that lets you derive a real history window. This is the only source that can bound a
consumer.

```graphql
query datasetQueries($urn: String!, $count: Int!) {
  listQueries(
    input: {
      datasetUrn: $urn
      start: 0
      count: $count
    }
  ) {
    start
    count
    total
    queries {
      urn
      properties {
        name
        description
        source
        statement { value language }
        created { time actor }
        lastModified { time actor }
        origin { urn type }
      }
      subjects {
        dataset { urn name }
        schemaField { urn fieldPath }
      }
      platform { name }
    }
  }
}
```

Variables: `{"urn": "...", "count": 50}`.

**Field notes.**

- `properties.statement.value` is the verbatim SQL. Keep it verbatim in your evidence; the extracted
  predicate is what the approver reads.
- `properties.source` is `MANUAL` (entered in the UI) or `SYSTEM` (extracted from a view, dbt model,
  dashboard, or query log). `SYSTEM` queries are the ones that reflect real traffic.
- `properties.origin` points at the asset the query came from — a view, a dbt model, a dashboard.
  That is how you attribute a query to a specific downstream consumer.
- `listQueries` has no `runCount` field. Query frequency comes from `usageStats`, not from here. Do
  not invent a run count.
- `total: 0` means no query text is available for this dataset. Every consumer then derives as
  `no_queries_observed`, which is a **blocking** state.

### Extracting the lower bound

From each statement, find predicates on the chosen date column:

| SQL fragment                                                    | Earliest read           | Note                      |
| ---------------------------------------------------------------- | ----------------------- | ------------------------- |
| `WHERE encounter_date >= '2024-08-16'`                           | `2024-08-16`            | literal, bounded          |
| `WHERE encounter_date > DATE '2024-08-16'`                       | `2024-08-17`            | strict — add a day        |
| `WHERE encounter_date BETWEEN '2024-08-16' AND '2025-01-01'`     | `2024-08-16`            | bounded                   |
| `WHERE encounter_date >= CURRENT_DATE - INTERVAL '180 day'`      | today − 180d            | **rolling** — label it    |
| `WHERE encounter_date >= date_trunc('year', CURRENT_DATE)`       | Jan 1 of current year   | rolling                   |
| `WHERE patient_id = 42` (no date predicate)                      | none                    | `no_date_filter` → blocks |
| no `WHERE` at all                                                | none                    | `no_date_filter` → blocks |

A predicate on a *different* date column than the one you are archiving on does not bound the
archive. Only a bound on the archive's own date column counts.

`OR` across date predicates widens the window: take the **oldest** branch. `AND` narrows it: take the
**newest** lower bound. When in doubt, take the older date — being wrong in the conservative
direction leaves data in place, which is recoverable; being wrong the other way is not.

---

## Writeback

Run these only after `verification.passed` is true, and only if the ColdLineage executor reported the
corresponding operation as `failed` or `skipped`. The executor is the primary writer.

### 1. Archive provenance — structured properties

```graphql
mutation writeArchiveProvenance(
  $assetUrn: String!
  $archivedThrough: String!
  $objectUri: String!
  $sha256: String!
  $restoreSla: String!
  $runId: String!
) {
  upsertStructuredProperties(
    input: {
      assetUrn: $assetUrn
      structuredPropertyInputParams: [
        {
          structuredPropertyUrn: "urn:li:structuredProperty:io.coldlineage.archive.state"
          values: [{ stringValue: "PARTIALLY_ARCHIVED" }]
        }
        {
          structuredPropertyUrn: "urn:li:structuredProperty:io.coldlineage.archive.archivedThrough"
          values: [{ stringValue: $archivedThrough }]
        }
        {
          structuredPropertyUrn: "urn:li:structuredProperty:io.coldlineage.archive.objectUri"
          values: [{ stringValue: $objectUri }]
        }
        {
          structuredPropertyUrn: "urn:li:structuredProperty:io.coldlineage.archive.sha256"
          values: [{ stringValue: $sha256 }]
        }
        {
          structuredPropertyUrn: "urn:li:structuredProperty:io.coldlineage.archive.restoreSla"
          values: [{ stringValue: $restoreSla }]
        }
        {
          structuredPropertyUrn: "urn:li:structuredProperty:io.coldlineage.archive.lastRunId"
          values: [{ stringValue: $runId }]
        }
      ]
    }
  ) {
    properties {
      structuredProperty { urn }
      values {
        ... on StringValue { stringValue }
        ... on NumberValue { numberValue }
      }
    }
  }
}
```

**Field notes — these are the two things people get wrong.**

- The input field is `structuredPropertyInputParams`, **not** `structuredPropertyInputs`.
- `values` is `[PropertyValueInput!]!` where `PropertyValueInput` is
  `{ stringValue: String, numberValue: Float }`. It is **not** a list of bare strings. For a
  `type: number` property use `values: [{ numberValue: 0.31 }]`.

`upsertStructuredProperties` returns `StructuredProperties!`, so a selection set is required.

`objectUri` and `sha256` are declared `cardinality: MULTIPLE` in
`backend/app/datahub/properties.yaml` because a dataset accumulates one archive object per run.
`upsertStructuredProperties` **replaces** the value list for a property. To append rather than
replace, read the current values first and send the union. Silently dropping a previous run's object
URI destroys the restore path for that run.

The property definitions must exist before values can be set:

```bash
datahub properties upsert -f backend/app/datahub/properties.yaml
```

To clear provenance after a full restore:

```graphql
mutation clearArchiveProvenance($assetUrn: String!) {
  removeStructuredProperties(
    input: {
      assetUrn: $assetUrn
      structuredPropertyUrns: [
        "urn:li:structuredProperty:io.coldlineage.archive.archivedThrough"
        "urn:li:structuredProperty:io.coldlineage.archive.state"
      ]
    }
  ) {
    properties { structuredProperty { urn } }
  }
}
```

### 2. Consumer warning — deprecation note

```graphql
mutation noteArchive($urn: String!, $note: String!) {
  updateDeprecation(input: { urn: $urn, deprecated: false, note: $note })
}
```

Suggested note text:

> `ColdLineage: history before 2024-07-01 has been moved to cold storage (run 41, verified
> sha256:9f2c…). Rows on or after that date are unaffected. An unqualified scan of this table
> returns partial history. Restore: POST /api/restore {"run_id": 41}.`

**`deprecated` must stay `false`.** The table is healthy — only its history moved. Setting
`deprecated: true` marks the asset as retired across the entire catalog, hides it from search
defaults, and misleads every consumer. The `note` is the payload; the flag is not.

`decommissionTime` is a `Long` (epoch ms) and does not apply here — a partial archive has no
decommission date.

Batch form, if several datasets were archived in one run:

```graphql
mutation noteArchiveBatch($note: String!) {
  batchUpdateDeprecation(
    input: {
      deprecated: false
      note: $note
      resources: [{ resourceUrn: "<URN_1>" }, { resourceUrn: "<URN_2>" }]
    }
  )
}
```

### 3. Manifest link — institutional memory

```graphql
mutation linkManifest($urn: String!, $manifestUri: String!, $label: String!) {
  addLink(input: { resourceUrn: $urn, linkUrl: $manifestUri, label: $label })
}
```

Use a label that identifies the run: `ColdLineage archive manifest (run 41)`. `linkUrl` + `label`
together form the link's identity, so a distinct label per run means runs do not overwrite each
other.

`linkUrl` must be resolvable by a human in a browser. An `s3://` URI is not. Use the console or
presigned URL the executor returns in `manifest.manifest_uri` if it is HTTP(S); otherwise link the
ColdLineage run page and note the object URI in the deprecation note instead.

Remove with the same pair:

```graphql
mutation unlinkManifest($urn: String!, $manifestUri: String!) {
  removeLink(input: { resourceUrn: $urn, linkUrl: $manifestUri })
}
```

### What never to write

```graphql
# WRONG — clobbers every other writer's custom properties on this aspect.
mutation { updateDataset(urn: "...", input: { ... }) { urn } }
```

`datasetProperties` is a shared aspect written by ingestion sources, dbt, and other agents. A
wholesale write replaces the whole map. Use typed structured properties (above), or the
`patch.graphql` mutations, which merge.

---

## Finding Already-Archived Datasets

Structured properties are searchable by qualified name, so the catalog itself becomes the archive
index:

```bash
datahub search "*" \
  --where "entity_type = dataset AND structuredProperties.io.coldlineage.archive.state = 'PARTIALLY_ARCHIVED'"

datahub search "*" \
  --where "entity_type = dataset AND structuredProperties.io.coldlineage.policy.legalHold = 'ACTIVE'"
```

The second query is the one to run *before* a batch assessment: it tells you which datasets are
unconditionally off-limits, so you never present a plan for one.

---

## Discovery and Troubleshooting

```bash
datahub graphql --list-mutations --format json
datahub graphql --describe upsertStructuredProperties --recurse --format json
datahub graphql --query '{ me { corpUser { urn } } }' --dry-run
datahub graphql --agent-context
datahub check server-config   # serverEnv: cloud | core
```

| Symptom                                            | Cause                                                                     |
| -------------------------------------------------- | ------------------------------------------------------------------------- |
| `File name too long`                               | Inline `--query` too long on macOS. Use a `.graphql` file.                 |
| `Unknown field 'structuredPropertyInputs'`         | Wrong field name. It is `structuredPropertyInputParams`.                   |
| `Expected type 'PropertyValueInput'`               | Bare strings in `values`. Use `[{ stringValue: "..." }]`.                  |
| `Unauthorized to perform this action`              | Missing *Edit Deprecation* / *Edit Structured Properties* privilege.       |
| `structuredProperties` returns null                | Definitions not upserted. Run `datahub properties upsert -f …`.            |
| `searchAcrossLineage` returns `total: 0`           | No lineage ingested for this entity. Unknown, not empty.                   |
| `listQueries` returns `total: 0`                   | Query ingestion not configured. Every window is `no_queries_observed`.     |
| Shell mangles a URN                                | Parentheses and commas. Pass URNs via `--variables`, never inline.         |
