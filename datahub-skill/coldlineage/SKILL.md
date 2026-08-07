# ColdLineage: assess-data-temperature

Use this skill when asked whether a dataset or historical partition can be moved from hot storage to a colder tier.

## Goal
Return an evidence-backed recommendation, never a blind age-based deletion rule.

## Required context
Retrieve as much as available from DataHub:
- dataset schema and date/partition columns
- lineage and active downstream dependencies
- owners and domains
- PII/PHI or other classification tags
- retention or legal-hold context
- usage/query history
- certification/deprecation status

## Procedure
1. Resolve the exact dataset URN.
2. Identify the historical range under review.
3. Compute a temperature score from access recency, query frequency, downstream activity and business criticality.
4. Reject execution if a legal hold or policy blocker is present.
5. Simulate downstream impact before proposing a move.
6. Produce a plan with cutoff, estimated rows/bytes, evidence, blockers, confidence and restore requirement.
7. Require explicit human approval before execution.
8. After execution, verify row count, schema and SHA-256 manifest.
9. Write archive state, object URI, cutoff, checksum, restore SLA and decision provenance back to DataHub.

## Output contract
```json
{
  "dataset_urn": "...",
  "temperature": 14,
  "classification": "COLD",
  "archive_eligible": true,
  "confidence": 0.94,
  "evidence": [],
  "blockers": [],
  "simulation": {"recommendation":"ARCHIVE_WITH_REHYDRATION","impacts":[]},
  "requires_human_approval": true
}
```

## Safety
Never delete or detach source records until the cold object and manifest have been written and verified. Never infer row-level non-use from table-level usage telemetry. Preserve access controls and sensitive-data handling requirements.
