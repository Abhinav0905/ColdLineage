'use client';

import type { ReactNode } from 'react';

import { Chip, ProvenanceChip } from '@/components/Primitives';
import { EMPTY, exactBytes, formatBytes, formatCount, formatTimestamp } from '@/lib/format';
import type { DatasetContext } from '@/lib/types';

/**
 * The context ColdLineage assembled before it was willing to decide anything.
 * Each group carries the provenance the API attached to it, so a reader can see
 * which system supplied which fact.
 */
export default function DatasetContextPanel({ context }: { context: DatasetContext }) {
  return (
    <div className="stack">
      <Group title="Identity">
        <dl className="kv">
          <dt>urn</dt>
          <dd className="mono">{context.urn}</dd>
          <dt>table</dt>
          <dd className="mono">{context.qualified_table}</dd>
          <dt>platform</dt>
          <dd>{context.platform}</dd>
          <dt>domain</dt>
          <dd>{context.domain ?? <span className="muted">none</span>}</dd>
          <dt>owners</dt>
          <dd>
            {context.owners.length > 0 ? (
              context.owners.join(', ')
            ) : (
              <span className="muted">no owners in the catalog</span>
            )}
          </dd>
          <dt>tags</dt>
          <dd className="row" style={{ gap: 6 }}>
            {context.tags.length > 0 ? (
              context.tags.map((tag) => <Chip key={tag}>{tag}</Chip>)
            ) : (
              <span className="muted">none</span>
            )}
            {context.sensitive ? <Chip tone="warning">sensitive</Chip> : null}
            {context.deprecated ? <Chip tone="critical">deprecated</Chip> : null}
          </dd>
          {context.glossary_terms.length > 0 ? (
            <>
              <dt>glossary</dt>
              <dd>{context.glossary_terms.join(', ')}</dd>
            </>
          ) : null}
        </dl>
      </Group>

      <Group title="Date column" provenanceSlot={<ProvenanceChip provenance={context.date_column_provenance} />}>
        <dl className="kv">
          <dt>column</dt>
          <dd className="mono">
            {context.date_column ?? <span className="muted">not resolved</span>}
          </dd>
        </dl>
      </Group>

      <Group title="Policy" provenanceSlot={<ProvenanceChip provenance={context.policy_provenance} />}>
        <dl className="kv">
          <dt>retention</dt>
          <dd>
            {context.retention_years === null ? (
              <span className="muted">not declared</span>
            ) : (
              `${context.retention_years} years`
            )}
          </dd>
          <dt>legal hold</dt>
          <dd>
            {context.legal_hold ? (
              <Chip tone="critical">
                on{context.legal_hold_matter ? ` · ${context.legal_hold_matter}` : ''}
              </Chip>
            ) : (
              <Chip tone="good">off</Chip>
            )}
          </dd>
          <dt>criticality</dt>
          <dd>
            {context.business_criticality === null ? (
              <span className="muted">not declared</span>
            ) : (
              context.business_criticality.toFixed(2)
            )}
          </dd>
        </dl>
      </Group>

      <Group title="Usage" provenanceSlot={<ProvenanceChip provenance={context.usage_provenance} />}>
        <dl className="kv">
          <dt>last query</dt>
          <dd>{formatTimestamp(context.last_query_at)}</dd>
          <dt>queries (30d)</dt>
          <dd className="tnum">{formatCount(context.query_count_30d)}</dd>
          <dt>distinct users (30d)</dt>
          <dd className="tnum">{formatCount(context.distinct_users_30d)}</dd>
        </dl>
      </Group>

      <Group
        title="Physical"
        provenanceSlot={<ProvenanceChip provenance={context.physical_provenance} />}
      >
        <dl className="kv">
          <dt>rows</dt>
          <dd className="tnum">{formatCount(context.row_count)}</dd>
          <dt>size</dt>
          <dd title={exactBytes(context.size_bytes)}>{formatBytes(context.size_bytes)}</dd>
          <dt>downstream</dt>
          <dd className="tnum">
            {context.downstream.length > 0 ? formatCount(context.downstream.length) : EMPTY}
          </dd>
        </dl>
      </Group>
    </div>
  );
}

function Group({
  title,
  provenanceSlot,
  children,
}: {
  title: string;
  provenanceSlot?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="card tight">
      <div className="section-head" style={{ marginBottom: 8 }}>
        <h3 className="section-title" style={{ fontSize: 15 }}>
          {title}
        </h3>
        {provenanceSlot}
      </div>
      {children}
    </div>
  );
}
