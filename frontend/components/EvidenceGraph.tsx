'use client';

import { EmptyState, ProvenanceChip, StatusIcon } from '@/components/Primitives';
import type { Blocker, EvidenceItem } from '@/lib/types';

const STATUS_TONE = {
  pass: 'good',
  warn: 'warning',
  block: 'critical',
} as const;

/**
 * The evidence graph. Every row names one check, its outcome, and the system
 * that supplied the input -- the provenance chip is the point of this panel.
 * A reader should be able to see, without asking, that these inputs came out
 * of DataHub rather than out of a config file.
 */
export function EvidenceGraph({ evidence }: { evidence: EvidenceItem[] }) {
  if (evidence.length === 0) {
    return <EmptyState>The backend returned no evidence items for this dataset.</EmptyState>;
  }

  return (
    <div className="evidence-list">
      {evidence.map((item, index) => (
        <div className={`evidence-row ${item.status}`} key={`${item.kind}-${item.label}-${index}`}>
          <span className="evidence-icon">
            <StatusIcon tone={STATUS_TONE[item.status]} />
          </span>
          <span>
            <span className="evidence-kind">{item.kind}</span>
            <div className="evidence-label">{item.label}</div>
            {item.provenance.detail ? (
              <div className="cell-sub">{item.provenance.detail}</div>
            ) : null}
          </span>
          <ProvenanceChip provenance={item.provenance} />
        </div>
      ))}
    </div>
  );
}

export function BlockerList({ blockers }: { blockers: Blocker[] }) {
  if (blockers.length === 0) {
    return (
      <div className="callout good">
        <div className="callout-head">
          <StatusIcon tone="good" />
          No policy blockers
        </div>
        Nothing in retention policy, legal hold, or classification stops an archive of this dataset.
      </div>
    );
  }

  return (
    <div className="evidence-list">
      {blockers.map((blocker) => (
        <div className="evidence-row block" key={`${blocker.code}-${blocker.message}`}>
          <span className="evidence-icon">
            <StatusIcon tone="critical" />
          </span>
          <span>
            <span className="evidence-kind mono">{blocker.code}</span>
            <div className="evidence-label">{blocker.message}</div>
          </span>
          <ProvenanceChip provenance={blocker.provenance} />
        </div>
      ))}
    </div>
  );
}
