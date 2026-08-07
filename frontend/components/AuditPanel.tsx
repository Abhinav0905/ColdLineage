'use client';

import { RefreshCw } from 'lucide-react';
import { useMemo, useState } from 'react';

import { Chip, EmptyState, ErrorState, JsonDetail, LoadingState } from '@/components/Primitives';
import { api } from '@/lib/api';
import { EMPTY, detailText, formatTimestamp, parseTimestamp } from '@/lib/format';
import type { AuditRow } from '@/lib/types';
import { useResource } from '@/lib/useResource';

function toneFor(eventType: string): 'good' | 'warning' | 'critical' | 'neutral' {
  const type = eventType.toLowerCase();
  if (type.includes('fail') || type.includes('error') || type.includes('refus')) return 'critical';
  if (type.includes('block') || type.includes('warn') || type.includes('restore')) return 'warning';
  if (type.includes('execute') || type.includes('verify') || type.includes('approve')) return 'good';
  return 'neutral';
}

export default function AuditPanel() {
  const audit = useResource<AuditRow[]>((signal) => api.audit(signal), []);
  const [filter, setFilter] = useState('');

  const rows = useMemo(() => {
    const list = [...(audit.data ?? [])];
    /* Newest first, by timestamp, falling back to insertion id. */
    list.sort((a, b) => {
      const byTime =
        parseTimestamp(b.created_at).getTime() - parseTimestamp(a.created_at).getTime();
      if (!Number.isNaN(byTime) && byTime !== 0) return byTime;
      return b.id - a.id;
    });
    const needle = filter.trim().toLowerCase();
    if (needle === '') return list;
    return list.filter((row) =>
      [row.event_type, row.dataset_urn, row.actor, detailText(row.detail)]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(needle)),
    );
  }, [audit.data, filter]);

  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow">Trace</div>
          <h1 className="page-title">Audit log</h1>
          <p className="page-lede">
            Every decision, refusal, execution, verification and write-back, newest first, exactly
            as the backend recorded it.
          </p>
        </div>
        <div className="row">
          <input
            className="input"
            type="search"
            placeholder="filter events, urns, actors"
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
            aria-label="Filter audit events"
          />
          <button type="button" className="btn" onClick={audit.reload}>
            <RefreshCw size={15} aria-hidden="true" />
            Refresh
          </button>
        </div>
      </div>

      {audit.error ? (
        <ErrorState title="Could not load the audit log" message={audit.error} onRetry={audit.reload} />
      ) : null}

      {audit.loading && !audit.data ? <LoadingState label="Loading /api/audit…" /> : null}

      {audit.data && rows.length === 0 ? (
        <EmptyState>
          {filter.trim() === ''
            ? 'The audit log is empty.'
            : `No audit events match “${filter.trim()}”.`}
        </EmptyState>
      ) : null}

      {rows.length > 0 ? (
        <div className={audit.refreshing ? 'is-refreshing' : undefined}>
          <div className="section-head">
            <h2 className="section-title">{rows.length} events</h2>
            <span className="section-note">newest first</span>
          </div>
          <div className="table-scroll">
            <table className="table">
              <thead>
                <tr>
                  <th className="num">#</th>
                  <th>When</th>
                  <th>Event</th>
                  <th>Dataset</th>
                  <th>Actor</th>
                  <th>Detail</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.id}>
                    <td className="num mono">{row.id}</td>
                    <td className="mono nowrap">{formatTimestamp(row.created_at)}</td>
                    <td>
                      <Chip tone={toneFor(row.event_type)}>
                        <span className="mono">{row.event_type}</span>
                      </Chip>
                    </td>
                    <td className="mono" style={{ fontSize: 13, maxWidth: 320, overflowWrap: 'anywhere' }}>
                      {row.dataset_urn ?? EMPTY}
                    </td>
                    <td>{row.actor ?? EMPTY}</td>
                    <td style={{ maxWidth: 560 }}>
                      <details className="expander">
                        <summary>
                          {(() => {
                            const text = detailText(row.detail).replace(/\s+/g, ' ').trim();
                            if (text === '') return 'no detail recorded';
                            return text.length > 84 ? `${text.slice(0, 84)}…` : text;
                          })()}
                        </summary>
                        <div className="expander-body">
                          <JsonDetail value={row.detail} />
                        </div>
                      </details>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}
    </>
  );
}
