'use client';

import { Code2 } from 'lucide-react';

import { EmptyState, ImpactChip, ProvenanceChip } from '@/components/Primitives';
import { derivationLabel, isUnboundedWindow } from '@/components/RangeSafetyTimeline';
import { describeSpan } from '@/lib/dates';
import { EMPTY, formatCount, formatTimestamp } from '@/lib/format';
import type { ConsumerImpact, ConsumerWindow, RangeVerdict } from '@/lib/types';

/**
 * The table twin of the Range Safety Timeline. Everything the chart encodes as
 * position or colour is also readable here as text, including the verbatim SQL
 * that produced each window -- which is the evidence the whole verdict rests on.
 */
export default function ConsumerImpactTable({
  verdict,
  windows,
  fresh,
}: {
  verdict: RangeVerdict | null;
  windows: ConsumerWindow[];
  fresh: boolean;
}) {
  const impacts: ConsumerImpact[] = fresh && verdict ? verdict.consumers : [];
  const rows: Array<{ window: ConsumerWindow; impact: ConsumerImpact | null }> =
    impacts.length > 0
      ? impacts.map((impact) => ({ window: impact.window, impact }))
      : windows.map((window) => ({ window, impact: null }));

  if (rows.length === 0) {
    return (
      <EmptyState>
        DataHub lineage reports no downstream consumers for this dataset. With nothing reading it,
        no consumer can be violated — but note that &ldquo;no consumers found&rdquo; is not the same
        claim as &ldquo;no consumers exist&rdquo;.
      </EmptyState>
    );
  }

  return (
    <div className="table-scroll">
      <table className="table">
        <thead>
          <tr>
            <th>Consumer</th>
            <th>Reads back to</th>
            <th>How we know</th>
            <th>State</th>
            <th className="num">Headroom</th>
            <th>Evidence</th>
            <th>Provenance</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(({ window, impact }) => {
            const unbounded = isUnboundedWindow(window);
            const headroom = impact ? impact.headroom_days : null;
            return (
              <tr key={window.consumer_urn}>
                <td>
                  <div className="cell-primary">{window.consumer_name}</div>
                  <div className="cell-sub">
                    {window.consumer_type}
                    {window.platform ? ` · ${window.platform}` : ''} · {window.degree} hop
                    {window.degree === 1 ? '' : 's'}
                  </div>
                  <div className="cell-sub mono" title={window.consumer_urn}>
                    {window.consumer_urn.length > 52
                      ? `${window.consumer_urn.slice(0, 51)}…`
                      : window.consumer_urn}
                  </div>
                </td>

                <td className="mono nowrap">
                  {window.earliest_date_read ?? (
                    <span style={{ color: '#f2a483' }}>no lower bound</span>
                  )}
                </td>

                <td>
                  {derivationLabel(window.derivation)}
                  {unbounded ? <div className="cell-sub">unbounded scan</div> : null}
                  {window.query_run_count !== null || window.query_last_seen ? (
                    <div className="cell-sub">
                      {window.query_run_count !== null
                        ? `${formatCount(window.query_run_count)} runs`
                        : 'run count not reported'}
                      {window.query_last_seen
                        ? ` · last seen ${formatTimestamp(window.query_last_seen)}`
                        : ''}
                    </div>
                  ) : null}
                </td>

                <td>
                  {impact ? (
                    <>
                      <ImpactChip state={impact.state} />
                      <div className="cell-sub">{impact.reason}</div>
                    </>
                  ) : (
                    <span className="muted">not evaluated at this cutoff yet</span>
                  )}
                </td>

                <td className="num nowrap">
                  {headroom === null ? (
                    EMPTY
                  ) : (
                    <>
                      <div>{formatCount(headroom)} d</div>
                      <div className="cell-sub">{describeSpan(headroom)}</div>
                    </>
                  )}
                </td>

                <td style={{ maxWidth: 460 }}>
                  {window.predicate ? (
                    <div className="mono" style={{ fontSize: 13, marginBottom: 6 }}>
                      {window.predicate}
                    </div>
                  ) : (
                    <div className="cell-sub">no date predicate extracted</div>
                  )}
                  {window.evidence_sql ? (
                    <details className="expander">
                      <summary>
                        <Code2 size={14} aria-hidden="true" />
                        the query we parsed
                      </summary>
                      <div className="expander-body">
                        <pre className="code">{window.evidence_sql}</pre>
                      </div>
                    </details>
                  ) : null}
                </td>

                <td>
                  <ProvenanceChip provenance={window.provenance} />
                  {window.provenance.detail ? (
                    <div className="cell-sub">{window.provenance.detail}</div>
                  ) : null}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
