/**
 * The wire contract, mirrored from backend/app/domain/models.py.
 *
 * Every decision-bearing value arrives with a Provenance saying which system
 * supplied it. The UI is required to surface that -- a number with no source
 * chip is a number nobody can defend.
 */

/** Known provenance sources. The trailing `string & {}` keeps literal
 *  autocompletion while letting the UI render a source the backend adds later
 *  instead of crashing on it. */
export type ProvenanceSource =
  | 'datahub:lineage'
  | 'datahub:usage'
  | 'datahub:queries'
  | 'datahub:structured_properties'
  | 'datahub:tags'
  | 'datahub:ownership'
  | 'datahub:schema'
  | 'datahub:deprecation'
  | 'warehouse:postgres'
  | 'cassette:recorded'
  | 'unavailable'
  | (string & {});

export interface Provenance {
  source: ProvenanceSource;
  detail: string;
  observed_at: string | null;
}

// ---------------------------------------------------------------------------
// Health / DataHub mode
// ---------------------------------------------------------------------------

export type DataHubMode = 'live' | 'replay';

export interface DataHubStatus {
  mode: DataHubMode;
  reachable: boolean;
  gms_url: string;
  recorded_at: string | null;
  detail: string;
  entity_count: number | null;
}

export interface HealthResponse {
  ok: boolean;
  service: string;
  version: string;
  datahub: DataHubStatus;
}

// ---------------------------------------------------------------------------
// Datasets
// ---------------------------------------------------------------------------

export type TemperatureClass = 'HOT' | 'WARM' | 'COOL' | 'COLD' | 'FROZEN';
export type ArchiveState = 'HOT' | 'PARTIALLY_ARCHIVED' | 'REHYDRATING';

export interface TemperatureBreakdown {
  score: number;
  classification: TemperatureClass;
  recency_component: number;
  frequency_component: number;
  downstream_component: number;
  criticality_component: number;
  inputs: Record<string, string>;
}

export interface Blocker {
  code: string;
  message: string;
  provenance: Provenance;
}

/** The three states a row can be in. Only `policy_held` answers to configuration;
 *  `in_use` is fixed by what downstream SQL reads. See backend/app/services/bands.py. */
export interface RowBands {
  archivable: number;
  policy_held: number;
  in_use: number;
  total: number;
  evidence_bound: string | null;
  policy_floor: string | null;
  cutoff: string | null;
  binding: 'evidence' | 'policy' | 'legal_hold' | 'unbounded' | 'unmeasured';
  reason: string;
  provenance: Provenance;
}

export interface DatasetSummary {
  id: number;
  urn: string;
  name: string;
  platform: string;
  domain: string | null;
  owners: string[];
  tags: string[];
  sensitive: boolean;
  row_count: number | null;
  size_bytes: number | null;
  date_column: string | null;
  min_date: string | null;
  max_date: string | null;
  temperature: TemperatureBreakdown;
  archive_eligible: boolean;
  blockers: Blocker[];
  downstream_count: number;
  archive_state: ArchiveState;
  archived_through: string | null;
  signals_live: boolean;
  bands: RowBands | null;
}

// ---------------------------------------------------------------------------
// Consumers and their history windows -- the core differentiator
// ---------------------------------------------------------------------------

export type WindowDerivation =
  | 'sql_predicate'
  | 'declared_property'
  | 'no_date_filter'
  | 'no_queries_observed'
  | 'not_a_query_consumer';

export interface ConsumerWindow {
  consumer_urn: string;
  consumer_name: string;
  consumer_type: string;
  platform: string | null;
  degree: number;
  earliest_date_read: string | null;
  derivation: WindowDerivation;
  predicate: string | null;
  evidence_sql: string | null;
  query_last_seen: string | null;
  query_run_count: number | null;
  provenance: Provenance;
}

export type ImpactState = 'safe' | 'tight' | 'blocked' | 'unknown';

export interface ConsumerImpact {
  window: ConsumerWindow;
  state: ImpactState;
  headroom_days: number | null;
  reason: string;
}

// ---------------------------------------------------------------------------
// Dataset detail
// ---------------------------------------------------------------------------

export interface EvidenceItem {
  kind: string;
  label: string;
  status: 'pass' | 'warn' | 'block';
  provenance: Provenance;
}

export interface DatasetContext {
  urn: string;
  name: string;
  platform: string;
  qualified_table: string;
  date_column: string | null;
  date_column_provenance: Provenance;

  owners: string[];
  domain: string | null;
  tags: string[];
  glossary_terms: string[];
  deprecated: boolean;

  retention_years: number | null;
  legal_hold: boolean;
  legal_hold_matter: string | null;
  business_criticality: number | null;
  policy_provenance: Provenance;

  last_query_at: string | null;
  query_count_30d: number | null;
  distinct_users_30d: number | null;
  usage_provenance: Provenance;

  downstream: ConsumerWindow[];

  row_count: number | null;
  size_bytes: number | null;
  physical_provenance: Provenance;

  sensitive: boolean;
}

export interface DatasetDetail extends DatasetSummary {
  context: DatasetContext;
  evidence: EvidenceItem[];
  confidence: number | null;
  datahub_url: string | null;
}

// ---------------------------------------------------------------------------
// Simulate / plan / execute
// ---------------------------------------------------------------------------

export type Recommendation =
  | 'SAFE_TO_ARCHIVE'
  | 'ARCHIVE_WITH_REHYDRATION'
  | 'DO_NOT_ARCHIVE';

export interface RangeVerdict {
  cutoff_date: string;
  recommendation: Recommendation;
  consumers: ConsumerImpact[];
  binding_constraint: ConsumerImpact | null;
  headroom_days: number | null;
  rationale: string;
}

export interface ArchivePlan {
  plan_hash: string;
  dataset_urn: string;
  cutoff_date: string;
  rows_in_scope: number;
  bytes_in_scope: number;
  verdict: RangeVerdict;
  blockers: Blocker[];
  monthly_savings_usd: number;
  requires_approval: boolean;
  created_at: string;
}

export interface ManifestPart {
  key?: string;
  rows?: number;
  bytes?: number;
  sha256?: string;
  [extra: string]: unknown;
}

export interface ArchiveManifest {
  dataset_urn: string;
  table: string;
  cutoff_date: string;
  rows: number;
  bytes: number;
  parts: ManifestPart[];
  sha256: string;
  columns: string[];
  object_uri: string;
  manifest_uri: string;
  verified_readback: boolean;
  created_at: string;
}

export interface VerificationReport {
  readback_sha256_match: boolean;
  readback_row_count: number;
  source_row_count: number;
  row_count_match: boolean;
  schema_match: boolean;
  passed: boolean;
  checked_at: string;
}

export interface WritebackOperation {
  op: string;
  target: string;
  status: 'ok' | 'failed' | 'skipped';
  detail: string;
}

export interface DataHubWriteback {
  mode: string;
  written: boolean;
  operations: WritebackOperation[];
  entity_url: string | null;
}

export interface ExecuteResponse {
  run_id: number;
  manifest: ArchiveManifest;
  verification: VerificationReport;
  datahub_writeback: DataHubWriteback;
}

export interface RestoreResponse {
  table: string;
  rows: number;
  sha256: string;
  verified: boolean;
}

export interface RunRow {
  id: number;
  dataset_urn: string;
  dataset_name: string;
  cutoff_date: string;
  status: string;
  rows_archived: number | null;
  bytes_archived: number | null;
  object_uri: string | null;
  checksum: string | null;
  approved_by: string | null;
  created_at: string;
  restored_at: string | null;
}

export interface AuditRow {
  id: number;
  event_type: string;
  dataset_urn: string | null;
  actor: string | null;
  detail: string | null;
  created_at: string;
}
