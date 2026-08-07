'use client';

import { ExternalLink } from 'lucide-react';

import { StatusIcon } from '@/components/Primitives';
import { EMPTY, exactBytes, formatBytes, formatCount, formatTimestamp } from '@/lib/format';
import type { ExecuteResponse, VerificationReport } from '@/lib/types';

function Check({ label, ok, detail }: { label: string; ok: boolean; detail?: string }) {
  return (
    <div className={`evidence-row ${ok ? 'pass' : 'block'}`}>
      <span className="evidence-icon">
        <StatusIcon tone={ok ? 'good' : 'critical'} />
      </span>
      <span>
        <div className="evidence-label">{label}</div>
        {detail ? <div className="cell-sub">{detail}</div> : null}
      </span>
      <span className={`op-status ${ok ? 'ok' : 'failed'}`}>{ok ? 'PASS' : 'FAIL'}</span>
    </div>
  );
}

function Verification({ report }: { report: VerificationReport }) {
  return (
    <div className="stack">
      <div className={`callout ${report.passed ? 'good' : 'critical'}`}>
        <div className="callout-head">
          <StatusIcon tone={report.passed ? 'good' : 'critical'} />
          {report.passed
            ? 'Verification passed before any row was deleted'
            : 'Verification did NOT pass'}
        </div>
        Checked {formatTimestamp(report.checked_at)}. The object was written and read back, and the
        read-back was compared against the source, before the delete step was allowed to run.
      </div>

      <div className="evidence-list">
        <Check
          label="Read-back checksum matches the written object"
          ok={report.readback_sha256_match}
        />
        <Check
          label="Row count matches the source"
          ok={report.row_count_match}
          detail={`read back ${formatCount(report.readback_row_count)} rows · source reported ${formatCount(report.source_row_count)} rows`}
        />
        <Check label="Schema of the archived object matches the source" ok={report.schema_match} />
      </div>
    </div>
  );
}

export default function ExecutionReport({ result }: { result: ExecuteResponse }) {
  const { manifest, verification, datahub_writeback: writeback } = result;

  return (
    <div className="stack">
      <div className="section-head">
        <h2 className="section-title">Run #{result.run_id} — verification</h2>
        <span className="section-note">read-back verified before delete</span>
      </div>
      <Verification report={verification} />

      <div className="section-head">
        <h2 className="section-title">Archive manifest</h2>
        <span className="section-note">{manifest.parts.length} part(s)</span>
      </div>
      <div className="card">
        <dl className="kv">
          <dt>table</dt>
          <dd className="mono">{manifest.table}</dd>
          <dt>cutoff</dt>
          <dd className="mono">{manifest.cutoff_date}</dd>
          <dt>rows moved</dt>
          <dd className="tnum">{formatCount(manifest.rows)}</dd>
          <dt>bytes moved</dt>
          <dd title={exactBytes(manifest.bytes)}>{formatBytes(manifest.bytes)}</dd>
          <dt>object</dt>
          <dd className="mono">{manifest.object_uri}</dd>
          <dt>manifest</dt>
          <dd className="mono">{manifest.manifest_uri}</dd>
          <dt>sha256</dt>
          <dd className="mono">{manifest.sha256}</dd>
          <dt>read-back</dt>
          <dd>
            {manifest.verified_readback ? 'verified' : 'NOT verified'}
          </dd>
          <dt>columns</dt>
          <dd className="mono">{manifest.columns.join(', ') || EMPTY}</dd>
          <dt>created</dt>
          <dd>{formatTimestamp(manifest.created_at)}</dd>
        </dl>

        {manifest.parts.length > 0 ? (
          <details className="expander" style={{ marginTop: 12 }}>
            <summary>Per-part digests ({manifest.parts.length})</summary>
            <div className="expander-body">
              <div className="table-scroll">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Key</th>
                      <th className="num">Rows</th>
                      <th className="num">Bytes</th>
                      <th>sha256</th>
                    </tr>
                  </thead>
                  <tbody>
                    {manifest.parts.map((part, index) => (
                      <tr key={String(part.key ?? index)}>
                        <td className="mono">{String(part.key ?? EMPTY)}</td>
                        <td className="num">{formatCount(part.rows ?? null)}</td>
                        <td className="num">{formatBytes(part.bytes ?? null)}</td>
                        <td className="mono">{String(part.sha256 ?? EMPTY)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </details>
        ) : null}
      </div>

      <div className="section-head">
        <h2 className="section-title">Written back to DataHub</h2>
        <span className="section-note">mode {writeback.mode}</span>
      </div>
      <div className="card">
        <div className={`callout ${writeback.written ? 'good' : 'warning'}`}>
          <div className="callout-head">
            <StatusIcon tone={writeback.written ? 'good' : 'warning'} />
            {writeback.written
              ? 'Archive provenance was written to DataHub'
              : 'Nothing was written to DataHub for this run'}
          </div>
          The catalog now carries the receipt: which range moved, where it went, and who approved it.
        </div>

        <div className="table-scroll" style={{ marginTop: 12 }}>
          <table className="table">
            <thead>
              <tr>
                <th>Operation</th>
                <th>Target</th>
                <th>Status</th>
                <th>Detail</th>
              </tr>
            </thead>
            <tbody>
              {writeback.operations.length === 0 ? (
                <tr>
                  <td colSpan={4} className="muted">
                    The backend reported no write-back operations.
                  </td>
                </tr>
              ) : (
                writeback.operations.map((operation, index) => (
                  <tr key={`${operation.op}-${operation.target}-${index}`}>
                    <td className="mono">{operation.op}</td>
                    <td className="mono">{operation.target}</td>
                    <td>
                      <span className={`op-status ${operation.status}`}>
                        <StatusIcon
                          tone={
                            operation.status === 'ok'
                              ? 'good'
                              : operation.status === 'failed'
                                ? 'critical'
                                : 'neutral'
                          }
                          size={13}
                        />
                        {operation.status}
                      </span>
                    </td>
                    <td>{operation.detail}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>

        {writeback.entity_url ? (
          <div style={{ marginTop: 12 }}>
            <a
              className="btn"
              href={writeback.entity_url}
              target="_blank"
              rel="noreferrer noopener"
            >
              <ExternalLink size={15} aria-hidden="true" />
              Open the entity in DataHub
            </a>
          </div>
        ) : (
          <div className="muted" style={{ marginTop: 12, fontSize: 13 }}>
            No entity URL was returned, so there is no deep link to offer.
          </div>
        )}
      </div>
    </div>
  );
}
