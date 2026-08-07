# ColdLineage architecture

```mermaid
flowchart TD
  DH[DataHub Context Graph] --> A[ColdLineage Agent]
  QH[Warehouse query history] --> A
  A --> T[Temperature scorer]
  A --> E[Evidence graph]
  A --> S[What-if lineage simulation]
  S --> H{Human approval}
  H -->|approve| X[Archive executor]
  X --> P[Parquet + SHA-256 manifest]
  P --> M[(MinIO / S3 cold tier)]
  X --> V[Verification]
  V --> D[(Hot PostgreSQL)]
  V --> W[DataHub writeback]
  M --> R[Restore service]
  R --> D2[(Temporary rehydration table)]
  W --> DH
```

## Trust boundaries
- The reasoning layer never receives unrestricted DDL/DML authority.
- The executor exposes constrained operations: preview, simulate, execute, restore.
- Human approval gates destructive movement.
- Archive verification occurs before hot data removal.
- Restore verifies SHA-256 before rehydration.
