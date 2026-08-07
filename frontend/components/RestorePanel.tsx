'use client';

import { RefreshCw, Undo2 } from 'lucide-react';
import { useCallback, useState } from 'react';

import { Chip, EmptyState, ErrorState, LoadingState, StatusIcon } from '@/components/Primitives';
import { api, errorMessage } from '@/lib/api';
import { EMPTY, exactBytes, formatBytes, formatCount, formatTimestamp } from '@/lib/format';
import type { RestoreResponse, RunRow } from '@/lib/types';
import { useResource } from '@/lib/useResource';

export default function RestorePanel() {
  const runs = useResource<RunRow[]>((signal) => api.runs(signal), []);

  const [temporary, setTemporary] = useState<Record<number, boolean>>({});
  const [busy, setBusy] = useState<number | null>(null);
  const [results, setResults] = useState<Record<number, RestoreResponse>>({});
  const [errors, setErrors] = useState<Record<number, string>>({});

  const restore = useCallback(
    async (runId: number) => {
      setBusy(runId);
      setErrors((previous) => {
        const next = { ...previous };
        delete next[runId];
        return next;
      });
      try {
        const result = await api.restore(runId, Boolean(temporary[runId]));
        setResults((previous) => ({ ...previous, [runId]: result }));
        runs.reload();
      } catch (cause) {
        setErrors((previous) => ({ ...previous, [runId]: errorMessage(cause) }));
      } finally {
        setBusy(null);
      }
    },
    [runs, temporary],
  );

  const rows = [...(runs.data ?? [])].sort((a, b) => b.id - a.id);

  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow">Rehydration</div>
          <h1 className="page-title">Restore an archived range</h1>
          <p className="page-lede">
            Every archive run is reversible. A restore reads the object back, recomputes its
            checksum, and reports whether that checksum matched — if it did not, the run says so.
          </p>
        </div>
        <button type="button" className="btn" onClick={runs.reload}>
          <RefreshCw size={15} aria-hidden="true" />
          Refresh
        </button>
      </div>

      {runs.error ? (
        <ErrorState title="Could not load runs" message={runs.error} onRetry={runs.reload} />
      ) : null}

      {runs.loading && !runs.data ? <LoadingState label="Loading /api/runs…" /> : null}

      {runs.data && rows.length === 0 ? (
        <EmptyState>No archive runs have been executed yet.</EmptyState>
      ) : null}

      {rows.length > 0 ? (
        <div className={runs.refreshing ? 'is-refreshing' : undefined}>
          <div className="table-scroll">
            <table className="table">
              <thead>
                <tr>
                  <th className="num">Run</th>
                  <th>Dataset</th>
                  <th>Cutoff</th>
                  <th>Status</th>
                  <th className="num">Rows</th>
                  <th className="num">Bytes</th>
                  <th>Object</th>
                  <th>Approved by</th>
                  <th>Restore</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((run) => {
                  const result = results[run.id];
                  const error = errors[run.id];
                  return (
                    <tr key={run.id}>
                      <td className="num mono">#{run.id}</td>

                      <td>
                        <div className="cell-primary">{run.dataset_name}</div>
                        <div className="cell-sub mono" title={run.dataset_urn}>
                          {run.dataset_urn.length > 48
                            ? `${run.dataset_urn.slice(0, 47)}…`
                            : run.dataset_urn}
                        </div>
                      </td>

                      <td className="mono nowrap">{run.cutoff_date}</td>

                      <td>
                        <Chip
                          tone={
                            run.status.toLowerCase().includes('fail')
                              ? 'critical'
                              : run.restored_at
                                ? 'warning'
                                : 'good'
                          }
                        >
                          {run.status}
                        </Chip>
                        <div className="cell-sub">{formatTimestamp(run.created_at)}</div>
                        {run.restored_at ? (
                          <div className="cell-sub">restored {formatTimestamp(run.restored_at)}</div>
                        ) : null}
                      </td>

                      <td className="num">{formatCount(run.rows_archived)}</td>

                      <td className="num" title={exactBytes(run.bytes_archived)}>
                        {formatBytes(run.bytes_archived)}
                      </td>

                      <td>
                        <div className="mono" style={{ fontSize: 13, overflowWrap: 'anywhere' }}>
                          {run.object_uri ?? EMPTY}
                        </div>
                        <div className="cell-sub mono" title={run.checksum ?? undefined}>
                          {run.checksum ? `sha256 ${run.checksum.slice(0, 16)}…` : 'no checksum'}
                        </div>
                      </td>

                      <td>{run.approved_by ?? EMPTY}</td>

                      <td>
                        <div className="stack" style={{ gap: 8 }}>
                          <label className="check">
                            <input
                              type="checkbox"
                              checked={Boolean(temporary[run.id])}
                              onChange={(event) =>
                                setTemporary((previous) => ({
                                  ...previous,
                                  [run.id]: event.target.checked,
                                }))
                              }
                            />
                            temporary
                          </label>
                          <button
                            type="button"
                            className="btn small"
                            disabled={busy === run.id}
                            onClick={() => void restore(run.id)}
                          >
                            <Undo2 size={14} aria-hidden="true" />
                            {busy === run.id ? 'Restoring…' : 'Restore'}
                          </button>

                          {result ? (
                            <div className={`callout ${result.verified ? 'good' : 'critical'}`}>
                              <div className="callout-head">
                                <StatusIcon tone={result.verified ? 'good' : 'critical'} />
                                {result.verified
                                  ? 'Checksum verified'
                                  : 'CHECKSUM DID NOT MATCH'}
                              </div>
                              <div className="mono" style={{ fontSize: 13 }}>
                                {result.table}
                              </div>
                              <div style={{ fontSize: 13 }}>
                                {formatCount(result.rows)} rows restored
                              </div>
                              <div
                                className="mono"
                                style={{ fontSize: 13, overflowWrap: 'anywhere' }}
                              >
                                {result.sha256}
                              </div>
                            </div>
                          ) : null}

                          {error ? (
                            <ErrorState title={`Restore of run #${run.id} failed`} message={error} />
                          ) : null}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}
    </>
  );
}
