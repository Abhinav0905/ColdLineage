# The contribution back to DataHub

ColdLineage does not only read the catalog. Once an archive is verified, the receipt
goes back into DataHub so the next person to open the entity inherits the fact that
part of this table's history is no longer in the warehouse.

Four separate contributions, each reported independently so a partial failure is
visible rather than swallowed:

1. typed structured properties under `io.coldlineage.archive.*` -- machine-readable facts
2. a deprecation note carrying the cutoff and the restore path -- the human-visible banner
3. an `institutionalMemory` link to the manifest -- for whoever needs the bytes
4. the `cold-tier-archived` tag -- so "what has an archived range?" is one search

Deliberately **not** written: the `datasetProperties` aspect. It holds other writers'
`customProperties`, and a whole-aspect PUT silently destroys them.

Entity: http://localhost:9002/dataset/urn:li:dataset:(urn:li:dataPlatform:postgres,coldlineage.public.patient_encounters,PROD)/

## How this transcript was captured

`scripts/record_examples.py` launches the unmodified `app.main:app` with
`DataHubClient._execute` wrapped so that every GraphQL document, its variables and the
GMS response are appended to a JSONL trace. The blocks below are that trace, verbatim,
filtered to the mutations issued during `POST /api/execute`. Nothing here was typed by
hand.

---

## Before

The entity as a third party sees it, read straight from GMS immediately before
`POST /api/execute`. The `io.coldlineage.policy.*` values are inputs ColdLineage READS;
no `io.coldlineage.archive.*` value exists yet, there is no deprecation banner, no
`cold-tier-archived` tag and no manifest link.

```json
{
  "data": {
    "dataset": {
      "urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,coldlineage.public.patient_encounters,PROD)",
      "name": "patient_encounters",
      "deprecation": {
        "deprecated": false,
        "note": "",
        "decommissionTime": null
      },
      "tags": {
        "tags": [
          {
            "tag": {
              "urn": "urn:li:tag:ColdLineageDemoEstate",
              "properties": {
                "name": "ColdLineageDemoEstate"
              }
            }
          },
          {
            "tag": {
              "urn": "urn:li:tag:PHI",
              "properties": {
                "name": "PHI"
              }
            }
          },
          {
            "tag": {
              "urn": "urn:li:tag:PII",
              "properties": {
                "name": "PII"
              }
            }
          },
          {
            "tag": {
              "urn": "urn:li:tag:HIPAA",
              "properties": {
                "name": "HIPAA"
              }
            }
          },
          {
            "tag": {
              "urn": "urn:li:tag:Tier2",
              "properties": {
                "name": "Tier2"
              }
            }
          }
        ]
      },
      "institutionalMemory": {
        "elements": []
      },
      "structuredProperties": {
        "properties": [
          {
            "structuredProperty": {
              "urn": "urn:li:structuredProperty:io.coldlineage.policy.retentionYears",
              "definition": {
                "qualifiedName": "io.coldlineage.policy.retentionYears",
                "displayName": "Retention Floor (years)"
              }
            },
            "values": [
              {
                "numberValue": 2.0
              }
            ]
          },
          {
            "structuredProperty": {
              "urn": "urn:li:structuredProperty:io.coldlineage.policy.legalHold",
              "definition": {
                "qualifiedName": "io.coldlineage.policy.legalHold",
                "displayName": "Legal Hold"
              }
            },
            "values": [
              {
                "stringValue": "NONE"
              }
            ]
          },
          {
            "structuredProperty": {
              "urn": "urn:li:structuredProperty:io.coldlineage.policy.businessCriticality",
              "definition": {
                "qualifiedName": "io.coldlineage.policy.businessCriticality",
                "displayName": "Business Criticality"
              }
            },
            "values": [
              {
                "numberValue": 0.35
              }
            ]
          }
        ]
      }
    }
  },
  "extensions": {}
}
```

---

## The mutations, as sent

### 1. `coldlineageUpsertProps`

Sent to `http://localhost:8090/api/graphql` at 2026-08-07T02:54:10.428580+00:00.

Document:

```graphql
mutation coldlineageUpsertProps($input: UpsertStructuredPropertiesInput!) {
  upsertStructuredProperties(input: $input) {
    properties {
      structuredProperty { urn }
      values { ... on StringValue { stringValue } ... on NumberValue { numberValue } }
    }
  }
}
```

Variables:

```json
{
  "input": {
    "assetUrn": "urn:li:dataset:(urn:li:dataPlatform:postgres,coldlineage.public.patient_encounters,PROD)",
    "structuredPropertyInputParams": [
      {
        "structuredPropertyUrn": "urn:li:structuredProperty:io.coldlineage.archive.state",
        "values": [
          {
            "stringValue": "PARTIALLY_ARCHIVED"
          }
        ]
      },
      {
        "structuredPropertyUrn": "urn:li:structuredProperty:io.coldlineage.archive.archivedThrough",
        "values": [
          {
            "stringValue": "2023-01-01"
          }
        ]
      },
      {
        "structuredPropertyUrn": "urn:li:structuredProperty:io.coldlineage.archive.objectUri",
        "values": [
          {
            "stringValue": "s3://coldlineage-archive/patient_encounters/2023-01-01/b7f8e22ba2c2/"
          }
        ]
      },
      {
        "structuredPropertyUrn": "urn:li:structuredProperty:io.coldlineage.archive.sha256",
        "values": [
          {
            "stringValue": "654b75b9db4dff806433e85dfb0cfe3ed4e29a17fc1a0044c2f14e16b5f39bf5"
          }
        ]
      },
      {
        "structuredPropertyUrn": "urn:li:structuredProperty:io.coldlineage.archive.restoreSla",
        "values": [
          {
            "stringValue": "on-demand, minutes"
          }
        ]
      },
      {
        "structuredPropertyUrn": "urn:li:structuredProperty:io.coldlineage.archive.lastRunId",
        "values": [
          {
            "stringValue": "1"
          }
        ]
      }
    ]
  }
}
```

GMS response:

```json
{
  "data": {
    "upsertStructuredProperties": {
      "properties": [
        {
          "structuredProperty": {
            "urn": "urn:li:structuredProperty:io.coldlineage.policy.retentionYears"
          },
          "values": [
            {
              "numberValue": 2.0
            }
          ]
        },
        {
          "structuredProperty": {
            "urn": "urn:li:structuredProperty:io.coldlineage.policy.legalHold"
          },
          "values": [
            {
              "stringValue": "NONE"
            }
          ]
        },
        {
          "structuredProperty": {
            "urn": "urn:li:structuredProperty:io.coldlineage.policy.businessCriticality"
          },
          "values": [
            {
              "numberValue": 0.35
            }
          ]
        },
        {
          "structuredProperty": {
            "urn": "urn:li:structuredProperty:io.coldlineage.archive.archivedThrough"
          },
          "values": [
            {
              "stringValue": "2023-01-01"
            }
          ]
        },
        {
          "structuredProperty": {
            "urn": "urn:li:structuredProperty:io.coldlineage.archive.objectUri"
          },
          "values": [
            {
              "stringValue": "s3://coldlineage-archive/patient_encounters/2023-01-01/b7f8e22ba2c2/"
            }
          ]
        },
        {
          "structuredProperty": {
            "urn": "urn:li:structuredProperty:io.coldlineage.archive.state"
          },
          "values": [
            {
              "stringValue": "PARTIALLY_ARCHIVED"
            }
          ]
        },
        {
          "structuredProperty": {
            "urn": "urn:li:structuredProperty:io.coldlineage.archive.restoreSla"
          },
          "values": [
            {
              "stringValue": "on-demand, minutes"
            }
          ]
        },
        {
          "structuredProperty": {
            "urn": "urn:li:structuredProperty:io.coldlineage.archive.sha256"
          },
          "values": [
            {
              "stringValue": "654b75b9db4dff806433e85dfb0cfe3ed4e29a17fc1a0044c2f14e16b5f39bf5"
            }
          ]
        },
        {
          "structuredProperty": {
            "urn": "urn:li:structuredProperty:io.coldlineage.archive.lastRunId"
          },
          "values": [
            {
              "stringValue": "1"
            }
          ]
        }
      ]
    }
  },
  "extensions": {}
}
```

### 2. `coldlineageDeprecate`

Sent to `http://localhost:8090/api/graphql` at 2026-08-07T02:54:10.471631+00:00.

Document:

```graphql
mutation coldlineageDeprecate($input: UpdateDeprecationInput!) {
  updateDeprecation(input: $input)
}
```

Variables:

```json
{
  "input": {
    "urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,coldlineage.public.patient_encounters,PROD)",
    "deprecated": true,
    "note": "ColdLineage: rows before 2023-01-01 (516,088 rows) were archived to s3://coldlineage-archive/patient_encounters/2023-01-01/b7f8e22ba2c2/ (sha256 654b75b9db4dff80...). Recent rows remain queryable in the warehouse. An unqualified scan of this table will NOT include the archived range. Rehydrate with: POST /api/restore {\"run_id\": 1}. Restore SLA: on-demand, minutes.",
    "decommissionTime": 1786071250428
  }
}
```

GMS response:

```json
{
  "data": {
    "updateDeprecation": true
  },
  "extensions": {}
}
```

### 3. `coldlineageAddLink`

Sent to `http://localhost:8090/api/graphql` at 2026-08-07T02:54:10.506369+00:00.

Document:

```graphql
mutation coldlineageAddLink($input: AddLinkInput!) {
  addLink(input: $input)
}
```

Variables:

```json
{
  "input": {
    "resourceUrn": "urn:li:dataset:(urn:li:dataPlatform:postgres,coldlineage.public.patient_encounters,PROD)",
    "linkUrl": "http://localhost:9000/coldlineage-archive/patient_encounters/2023-01-01/b7f8e22ba2c2/manifest.json",
    "label": "ColdLineage archive manifest (2023-01-01)"
  }
}
```

GMS response:

```json
{
  "data": {
    "addLink": true
  },
  "extensions": {}
}
```

### 4. `coldlineageCreateTag`

Sent to `http://localhost:8090/api/graphql` at 2026-08-07T02:54:10.519849+00:00.

Document:

```graphql
mutation coldlineageCreateTag($input: CreateTagInput!) {
  createTag(input: $input)
}
```

Variables:

```json
{
  "input": {
    "id": "cold-tier-archived",
    "name": "cold-tier-archived",
    "description": "Part of this dataset's history lives in cold storage. An unqualified scan will not return the archived range."
  }
}
```

GMS response:

```json
{
  "errors": [
    {
      "message": "This Tag already exists!",
      "locations": [
        {
          "line": 3,
          "column": 3
        }
      ],
      "path": [
        "createTag"
      ],
      "extensions": {
        "code": 400,
        "type": "BAD_REQUEST",
        "classification": "DataFetchingException"
      }
    }
  ],
  "data": {
    "createTag": null
  },
  "extensions": {}
}
```

### 5. `coldlineageAddTag`

Sent to `http://localhost:8090/api/graphql` at 2026-08-07T02:54:10.553578+00:00.

Document:

```graphql
mutation coldlineageAddTag($input: TagAssociationInput!) {
  addTag(input: $input)
}
```

Variables:

```json
{
  "input": {
    "tagUrn": "urn:li:tag:cold-tier-archived",
    "resourceUrn": "urn:li:dataset:(urn:li:dataPlatform:postgres,coldlineage.public.patient_encounters,PROD)"
  }
}
```

GMS response:

```json
{
  "data": {
    "addTag": true
  },
  "extensions": {}
}
```

---

## What the application reported

The `datahub_writeback` block of the `POST /api/execute` response -- one line per
contribution, each with its own status.

```json
{
  "mode": "live",
  "written": true,
  "operations": [
    {
      "op": "upsertStructuredProperties",
      "target": "urn:li:dataset:(urn:li:dataPlatform:postgres,coldlineage.public.patient_encounters,PROD)",
      "status": "ok",
      "detail": "6 typed properties written under io.coldlineage.archive.*"
    },
    {
      "op": "updateDeprecation",
      "target": "urn:li:dataset:(urn:li:dataPlatform:postgres,coldlineage.public.patient_encounters,PROD)",
      "status": "ok",
      "detail": "deprecation note carries the cutoff and restore path"
    },
    {
      "op": "addLink",
      "target": "urn:li:dataset:(urn:li:dataPlatform:postgres,coldlineage.public.patient_encounters,PROD)",
      "status": "ok",
      "detail": "s3://coldlineage-archive/patient_encounters/2023-01-01/b7f8e22ba2c2/manifest.json"
    },
    {
      "op": "addTag",
      "target": "urn:li:dataset:(urn:li:dataPlatform:postgres,coldlineage.public.patient_encounters,PROD)",
      "status": "ok",
      "detail": "cold-tier-archived"
    }
  ],
  "entity_url": "http://localhost:9002/dataset/urn:li:dataset:(urn:li:dataPlatform:postgres,coldlineage.public.patient_encounters,PROD)/"
}
```

---

## After

The same read-back query, run again after the archive completed. Read it next to the
Before block above:

- `io.coldlineage.policy.*` -- unchanged. These are the inputs we READ (retention floor, legal hold, business criticality).
- `io.coldlineage.archive.*` -- six new typed values we WROTE, sitting alongside them: state, archivedThrough, objectUri, sha256, restoreSla, lastRunId.
- `deprecation.note` -- names the cutoff (2023-01-01), the row count (516,088), the object URI, the checksum and the exact call that rehydrates it.
- `tags` -- `urn:li:tag:cold-tier-archived` added next to the estate's own PHI/PII/HIPAA tags.
- `institutionalMemory` -- a clickable link to the manifest.

```json
{
  "data": {
    "dataset": {
      "urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,coldlineage.public.patient_encounters,PROD)",
      "name": "patient_encounters",
      "deprecation": {
        "deprecated": true,
        "note": "ColdLineage: rows before 2023-01-01 (516,088 rows) were archived to s3://coldlineage-archive/patient_encounters/2023-01-01/b7f8e22ba2c2/ (sha256 654b75b9db4dff80...). Recent rows remain queryable in the warehouse. An unqualified scan of this table will NOT include the archived range. Rehydrate with: POST /api/restore {\"run_id\": 1}. Restore SLA: on-demand, minutes.",
        "decommissionTime": 1786071250428
      },
      "tags": {
        "tags": [
          {
            "tag": {
              "urn": "urn:li:tag:ColdLineageDemoEstate",
              "properties": {
                "name": "ColdLineageDemoEstate"
              }
            }
          },
          {
            "tag": {
              "urn": "urn:li:tag:PHI",
              "properties": {
                "name": "PHI"
              }
            }
          },
          {
            "tag": {
              "urn": "urn:li:tag:PII",
              "properties": {
                "name": "PII"
              }
            }
          },
          {
            "tag": {
              "urn": "urn:li:tag:HIPAA",
              "properties": {
                "name": "HIPAA"
              }
            }
          },
          {
            "tag": {
              "urn": "urn:li:tag:Tier2",
              "properties": {
                "name": "Tier2"
              }
            }
          },
          {
            "tag": {
              "urn": "urn:li:tag:cold-tier-archived",
              "properties": {
                "name": "cold-tier-archived"
              }
            }
          }
        ]
      },
      "institutionalMemory": {
        "elements": [
          {
            "url": "http://localhost:9000/coldlineage-archive/patient_encounters/2023-01-01/b7f8e22ba2c2/manifest.json",
            "label": "ColdLineage archive manifest (2023-01-01)"
          }
        ]
      },
      "structuredProperties": {
        "properties": [
          {
            "structuredProperty": {
              "urn": "urn:li:structuredProperty:io.coldlineage.policy.retentionYears",
              "definition": {
                "qualifiedName": "io.coldlineage.policy.retentionYears",
                "displayName": "Retention Floor (years)"
              }
            },
            "values": [
              {
                "numberValue": 2.0
              }
            ]
          },
          {
            "structuredProperty": {
              "urn": "urn:li:structuredProperty:io.coldlineage.policy.legalHold",
              "definition": {
                "qualifiedName": "io.coldlineage.policy.legalHold",
                "displayName": "Legal Hold"
              }
            },
            "values": [
              {
                "stringValue": "NONE"
              }
            ]
          },
          {
            "structuredProperty": {
              "urn": "urn:li:structuredProperty:io.coldlineage.policy.businessCriticality",
              "definition": {
                "qualifiedName": "io.coldlineage.policy.businessCriticality",
                "displayName": "Business Criticality"
              }
            },
            "values": [
              {
                "numberValue": 0.35
              }
            ]
          },
          {
            "structuredProperty": {
              "urn": "urn:li:structuredProperty:io.coldlineage.archive.archivedThrough",
              "definition": {
                "qualifiedName": "io.coldlineage.archive.archivedThrough",
                "displayName": "Archived Through"
              }
            },
            "values": [
              {
                "stringValue": "2023-01-01"
              }
            ]
          },
          {
            "structuredProperty": {
              "urn": "urn:li:structuredProperty:io.coldlineage.archive.objectUri",
              "definition": {
                "qualifiedName": "io.coldlineage.archive.objectUri",
                "displayName": "Archive Object URI"
              }
            },
            "values": [
              {
                "stringValue": "s3://coldlineage-archive/patient_encounters/2023-01-01/b7f8e22ba2c2/"
              }
            ]
          },
          {
            "structuredProperty": {
              "urn": "urn:li:structuredProperty:io.coldlineage.archive.state",
              "definition": {
                "qualifiedName": "io.coldlineage.archive.state",
                "displayName": "Archive State"
              }
            },
            "values": [
              {
                "stringValue": "PARTIALLY_ARCHIVED"
              }
            ]
          },
          {
            "structuredProperty": {
              "urn": "urn:li:structuredProperty:io.coldlineage.archive.restoreSla",
              "definition": {
                "qualifiedName": "io.coldlineage.archive.restoreSla",
                "displayName": "Restore SLA"
              }
            },
            "values": [
              {
                "stringValue": "on-demand, minutes"
              }
            ]
          },
          {
            "structuredProperty": {
              "urn": "urn:li:structuredProperty:io.coldlineage.archive.sha256",
              "definition": {
                "qualifiedName": "io.coldlineage.archive.sha256",
                "displayName": "Archive SHA-256"
              }
            },
            "values": [
              {
                "stringValue": "654b75b9db4dff806433e85dfb0cfe3ed4e29a17fc1a0044c2f14e16b5f39bf5"
              }
            ]
          },
          {
            "structuredProperty": {
              "urn": "urn:li:structuredProperty:io.coldlineage.archive.lastRunId",
              "definition": {
                "qualifiedName": "io.coldlineage.archive.lastRunId",
                "displayName": "Last Archive Run"
              }
            },
            "values": [
              {
                "stringValue": "1"
              }
            ]
          }
        ]
      }
    }
  },
  "extensions": {}
}
```

### Note on the manifest link

`institutionalMemory` rejects non-HTTP schemes outright (`URL scheme 's3' is not
allowed`), so the link is written as the object store's HTTP endpoint. The canonical
`s3://` URI is still recorded, in `io.coldlineage.archive.objectUri`:

- object: `s3://coldlineage-archive/patient_encounters/2023-01-01/b7f8e22ba2c2/`
- manifest: `s3://coldlineage-archive/patient_encounters/2023-01-01/b7f8e22ba2c2/manifest.json`
- sha256: `654b75b9db4dff806433e85dfb0cfe3ed4e29a17fc1a0044c2f14e16b5f39bf5`
