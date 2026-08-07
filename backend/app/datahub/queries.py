"""GraphQL documents issued against DataHub GMS.

Each read is a separate document rather than one large query, so that a field or
aspect missing on a given DataHub version degrades that single signal instead of
blanking the whole context. Whatever fails is reported as Source.UNAVAILABLE and
shows up in the UI as a missing input, never as a fabricated one.
"""

DATASET_ENTITY = """
query coldlineageDataset($urn: String!) {
  dataset(urn: $urn) {
    urn
    name
    platform { name properties { displayName } }
    properties {
      name
      qualifiedName
      description
      customProperties { key value }
    }
    editableProperties { description }
    schemaMetadata {
      name
      fields { fieldPath nativeDataType type description }
    }
    ownership {
      owners {
        owner {
          ... on CorpUser { urn username properties { displayName email } }
          ... on CorpGroup { urn name properties { displayName } }
        }
        ownershipType { urn info { name } }
      }
    }
    domain { domain { urn properties { name } } }
    tags { tags { tag { urn name properties { name } } } }
    glossaryTerms { terms { term { urn name properties { name } } } }
    deprecation { deprecated note decommissionTime actor }
    subTypes { typeNames }
  }
}
"""

# Structured properties are split out: older GMS builds reject the field inside the
# main document, and losing policy inputs must not cost us schema and ownership too.
DATASET_STRUCTURED_PROPERTIES = """
query coldlineageStructuredProps($urn: String!) {
  dataset(urn: $urn) {
    urn
    structuredProperties {
      properties {
        structuredProperty { urn definition { qualifiedName displayName valueType { urn } } }
        values {
          ... on StringValue { stringValue }
          ... on NumberValue { numberValue }
        }
      }
    }
  }
}
"""

DOWNSTREAM_LINEAGE = """
query coldlineageDownstream($urn: String!, $count: Int!) {
  searchAcrossLineage(
    input: { urn: $urn, direction: DOWNSTREAM, start: 0, count: $count, query: "*" }
  ) {
    total
    searchResults {
      degree
      entity {
        urn
        type
        # Each fragment aliases `properties` to a distinct name. Without the aliases
        # GraphQL rejects the document: Dataset.properties.name and
        # Dashboard.properties.name have different nullability, which is a
        # FieldsConflict when merged under one response key.
        ... on Dataset {
          name
          platform { name }
          datasetProperties: properties { name qualifiedName }
        }
        ... on Dashboard {
          dashboardPlatform: platform { name }
          dashboardProperties: properties { name }
        }
        ... on Chart {
          chartPlatform: platform { name }
          chartProperties: properties { name }
        }
        ... on MLModel {
          mlModelName: name
          mlModelPlatform: platform { name }
          mlModelProperties: properties { name }
        }
        ... on DataJob {
          jobId
          dataJobProperties: properties { name }
        }
        ... on DataFlow {
          flowId
          dataFlowProperties: properties { name }
        }
      }
    }
  }
}
"""

DATASET_USAGE = """
query coldlineageUsage($urn: String!) {
  dataset(urn: $urn) {
    urn
    usageStats(range: MONTH) {
      buckets { bucket duration metrics { totalSqlQueries uniqueUserCount } }
      aggregations {
        uniqueUserCount
        totalSqlQueries
        users { user { urn username } count }
      }
    }
  }
}
"""

# Real SQL text associated with a dataset. This is the input the history-window
# extractor parses; without it every consumer is treated as an unbounded scan.
DATASET_QUERIES = """
query coldlineageQueries($urn: String!, $count: Int!) {
  listQueries(input: { start: 0, count: $count, datasetUrn: $urn }) {
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
        # The ingestion side stamps coldlineage.consumer_urn / consumer_type /
        # last_run_at here, which is how a query is joined back to the lineage node
        # that issues it. Without it we would be matching on display names.
        customProperties { key value }
      }
      subjects { dataset { urn } }
    }
  }
}
"""

SEARCH_DATASETS = """
query coldlineageSearch($query: String!, $count: Int!) {
  searchAcrossEntities(
    input: { types: [DATASET], query: $query, start: 0, count: $count }
  ) {
    total
    searchResults { entity { urn type ... on Dataset { name platform { name } } } }
  }
}
"""

# ---------------------------------------------------------------------------
# Mutations -- the contribution back to the graph.
# ---------------------------------------------------------------------------

UPSERT_STRUCTURED_PROPERTIES = """
mutation coldlineageUpsertProps($input: UpsertStructuredPropertiesInput!) {
  upsertStructuredProperties(input: $input) {
    properties {
      structuredProperty { urn }
      values { ... on StringValue { stringValue } ... on NumberValue { numberValue } }
    }
  }
}
"""

UPDATE_DEPRECATION = """
mutation coldlineageDeprecate($input: UpdateDeprecationInput!) {
  updateDeprecation(input: $input)
}
"""

ADD_LINK = """
mutation coldlineageAddLink($input: AddLinkInput!) {
  addLink(input: $input)
}
"""

ADD_TAG = """
mutation coldlineageAddTag($input: TagAssociationInput!) {
  addTag(input: $input)
}
"""

# addTag refuses to apply a tag whose entity does not exist yet, so the tag has to be
# created once before it can ever be attached. Re-running this is harmless.
CREATE_TAG = """
mutation coldlineageCreateTag($input: CreateTagInput!) {
  createTag(input: $input)
}
"""

HEALTH = """
query coldlineageHealth {
  appConfig { appVersion }
}
"""
