'use client';

import { ExternalLink, FileCheck2, Play, RefreshCw, ShieldCheck } from 'lucide-react';
import { useRouter, useSearchParams } from 'next/navigation';
import { useCallback, useEffect, useMemo, useState } from 'react';

import ConsumerImpactTable from '@/components/ConsumerImpactTable';
import DatasetContextPanel from '@/components/DatasetContextPanel';
import { BlockerList, EvidenceGraph } from '@/components/EvidenceGraph';
import ExecutionReport from '@/components/ExecutionReport';
import {
  Chip,
  Confidence,
  EmptyState,
  ErrorState,
  LoadingState,
  StatusIcon,
  TemperaturePill,
} from '@/components/Primitives';
import RangeSafetyTimeline from '@/components/RangeSafetyTimeline';
import TemperaturePanel from '@/components/TemperaturePanel';
import { ApiError, api, errorMessage, isAbort } from '@/lib/api';
import { addDays, clampDate, parseIsoDate, toIsoDate } from '@/lib/dates';
import { exactBytes, formatBytes, formatCount, formatTimestamp, formatUsd } from '@/lib/format';
import type {
  ArchivePlan,
  DatasetDetail,
  DatasetSummary,
  ExecuteResponse,
  RangeVerdict,
} from '@/lib/types';
import { useResource } from '@/lib/useResource';

const SIMULATE_DEBOUNCE_MS = 250;
/** Where the cutoff starts before anyone touches it: two years back from the
 *  newest row, clamped into the dataset's measured range. */
const DEFAULT_CUTOFF_LOOKBACK_DAYS = 730;

export default function CandidateWorkbench() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const datasets = useResource<DatasetSummary[]>((signal) => api.datasets(signal), []);

  const list = useMemo(() => datasets.data ?? [], [datasets.data]);

  /* The URL is the single source of truth for selection, so the deep link from
     the Overview table lands on the dataset it names -- by id, never by row
     position. */
  const rawParam = searchParams.get('dataset');
  const paramId = rawParam !== null && /^\d+$/.test(rawParam) ? Number(rawParam) : null;
  const selectedId =
    paramId !== null && list.some((item) => item.id === paramId)
      ? paramId
      : list.length > 0
        ? list[0].id
        : null;

  const select = useCallback(
    (id: number) => router.replace(`/candidates?dataset=${id}`, { scroll: false }),
    [router],
  );

  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow">Range simulation</div>
          <h1 className="page-title">Is this date range safe to move?</h1>
          <p className="page-lede">
            Every downstream consumer&apos;s real history window, derived by parsing its actual SQL
            out of DataHub. Drag the cutoff: the moment it crosses a window somebody still reads,
            the plan is refused.
          </p>
        </div>
        <button type="button" className="btn" onClick={datasets.reload}>
          <RefreshCw size={15} aria-hidden="true" />
          Reload datasets
        </button>
      </div>

      {datasets.error ? (
        <ErrorState
          title="Could not load the dataset list"
          message={datasets.error}
          onRetry={datasets.reload}
        />
      ) : null}

      {datasets.loading && !datasets.data ? <LoadingState label="Loading /api/datasets…" /> : null}

      {datasets.data && list.length === 0 ? (
        <EmptyState>The backend returned no datasets, so there is nothing to simulate.</EmptyState>
      ) : null}

      {list.length > 0 ? (
        <div className="workbench">
          <div className="card">
            <div className="section-head">
              <h2 className="section-title">Datasets</h2>
              <span className="section-note">{list.length}</span>
            </div>
            <div className="picker">
              {list.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  className="picker-item"
                  aria-pressed={item.id === selectedId}
                  onClick={() => select(item.id)}
                >
                  <span className="picker-name">{item.name}</span>
                  <span className="picker-meta">
                    <TemperaturePill classification={item.temperature.classification} />
                    <span title={exactBytes(item.size_bytes)}>{formatBytes(item.size_bytes)}</span>
                    <span>{formatCount(item.downstream_count)} downstream</span>
                  </span>
                  {item.archive_state === 'PARTIALLY_ARCHIVED' ? (
                    <span className="picker-meta mono">
                      archived through {item.archived_through ?? 'unknown'}
                    </span>
                  ) : null}
                </button>
              ))}
            </div>
          </div>

          {selectedId === null ? (
            <EmptyState>Select a dataset.</EmptyState>
          ) : (
            <DatasetStudio key={selectedId} datasetId={selectedId} />
          )}
        </div>
      ) : null}
    </>
  );
}

/* ==========================================================================
   One dataset. Keyed on the id by the parent, so switching datasets resets
   the cutoff, the verdict, the plan and any execution result outright -- a
   stale plan hash must never survive a dataset change.
   ========================================================================== */

function DatasetStudio({ datasetId }: { datasetId: number }) {
  const detail = useResource<DatasetDetail>((signal) => api.dataset(datasetId, signal), [datasetId]);

  const [cutoff, setCutoff] = useState<string | null>(null);
  const [verdict, setVerdict] = useState<RangeVerdict | null>(null);
  const [simPending, setSimPending] = useState(false);
  const [simError, setSimError] = useState<string | null>(null);

  const [plan, setPlan] = useState<ArchivePlan | null>(null);
  const [planPending, setPlanPending] = useState(false);
  const [planError, setPlanError] = useState<string | null>(null);
  const [planErrorDetail, setPlanErrorDetail] = useState<unknown>(null);

  const [approvedBy, setApprovedBy] = useState('');
  const [execution, setExecution] = useState<ExecuteResponse | null>(null);
  const [execPending, setExecPending] = useState(false);
  const [execError, setExecError] = useState<string | null>(null);

  /* Seed the cutoff once, from the dataset's own measured range. */
  useEffect(() => {
    if (cutoff !== null || !detail.data) return;
    const min = parseIsoDate(detail.data.min_date);
    const max = parseIsoDate(detail.data.max_date);
    if (!min || !max) return;
    setCutoff(toIsoDate(clampDate(addDays(max, -DEFAULT_CUTOFF_LOOKBACK_DAYS), min, max)));
  }, [cutoff, detail.data]);

  /* Debounced simulate. The server is the authority on the verdict; the client
     only previews colours while this is in flight. */
  useEffect(() => {
    if (cutoff === null) return;

    const controller = new AbortController();
    let cancelled = false;
    setSimPending(true);

    const timer = window.setTimeout(() => {
      api
        .simulate(datasetId, cutoff, controller.signal)
        .then((result) => {
          if (cancelled) return;
          setVerdict(result);
          setSimError(null);
        })
        .catch((cause: unknown) => {
          if (cancelled || isAbort(cause)) return;
          setSimError(errorMessage(cause));
        })
        .finally(() => {
          if (!cancelled) setSimPending(false);
        });
    }, SIMULATE_DEBOUNCE_MS);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [cutoff, datasetId]);

  /* Moving the cutoff invalidates any plan built at the old one. */
  const changeCutoff = useCallback((next: string) => {
    setCutoff(next);
    setPlan(null);
    setPlanError(null);
    setPlanErrorDetail(null);
    setExecution(null);
    setExecError(null);
  }, []);

  const buildPlan = useCallback(async () => {
    if (cutoff === null) return;
    setPlanPending(true);
    setPlanError(null);
    setPlanErrorDetail(null);
    try {
      setPlan(await api.plan(datasetId, cutoff));
    } catch (cause) {
      setPlan(null);
      setPlanError(errorMessage(cause));
      if (cause instanceof ApiError) setPlanErrorDetail(cause.detail);
    } finally {
      setPlanPending(false);
    }
  }, [cutoff, datasetId]);

  const runExecute = useCallback(async () => {
    if (!plan || approvedBy.trim() === '') return;
    setExecPending(true);
    setExecError(null);
    try {
      const result = await api.execute(plan.plan_hash, approvedBy.trim());
      setExecution(result);
      detail.reload();
    } catch (cause) {
      setExecError(errorMessage(cause));
    } finally {
      setExecPending(false);
    }
  }, [approvedBy, detail, plan]);

  if (detail.error && !detail.data) {
    return (
      <ErrorState
        title={`Could not load dataset ${datasetId}`}
        message={detail.error}
        onRetry={detail.reload}
      />
    );
  }

  if (!detail.data) {
    return <LoadingState label={`Loading /api/datasets/${datasetId}…`} />;
  }

  const data = detail.data;
  const verdictFresh = verdict !== null && verdict.cutoff_date === cutoff && simError === null;
  const verdictAllows =
    verdictFresh && verdict !== null && verdict.recommendation !== 'DO_NOT_ARCHIVE';
  const planAllows =
    plan !== null && plan.blockers.length === 0 && plan.verdict.recommendation !== 'DO_NOT_ARCHIVE';
  const planMatchesCutoff = plan !== null && plan.cutoff_date === cutoff;
  const canApprove =
    planAllows &&
    planMatchesCutoff &&
    verdictAllows &&
    approvedBy.trim().length > 0 &&
    !execPending &&
    execution === null;

  const approvalBlockedReason = (() => {
    if (execution !== null) return 'This plan has already been executed.';
    if (plan === null) return 'Build a plan first.';
    if (!planMatchesCutoff) return 'The cutoff moved after this plan was built. Rebuild the plan.';
    if (plan.blockers.length > 0) return 'The plan carries policy blockers.';
    if (plan.verdict.recommendation === 'DO_NOT_ARCHIVE') {
      return 'The plan verdict is DO_NOT_ARCHIVE.';
    }
    if (!verdictFresh) return 'Waiting for the server to confirm this cutoff.';
    if (!verdictAllows) return 'The current verdict is DO_NOT_ARCHIVE.';
    if (approvedBy.trim().length === 0) return 'Enter who is approving this.';
    return null;
  })();

  return (
    <div className="stack">
      {/* ------------------------------------------------------ identity */}
      <div className="card">
        <div className="section-head" style={{ marginBottom: 6 }}>
          <div>
            <h2 className="section-title" style={{ fontSize: 20 }}>
              {data.name}
            </h2>
            <div className="muted mono" style={{ fontSize: 13, overflowWrap: 'anywhere' }}>
              {data.urn}
            </div>
          </div>
          <div className="row">
            <TemperaturePill classification={data.temperature.classification} />
            {data.archive_state === 'PARTIALLY_ARCHIVED' ? (
              <Chip tone="good">archived through {data.archived_through ?? 'unknown'}</Chip>
            ) : (
              <Chip>{data.archive_state.toLowerCase().replace(/_/g, ' ')}</Chip>
            )}
            {!data.signals_live ? <Chip tone="warning">signals not live</Chip> : null}
            {data.datahub_url ? (
              <a className="btn small" href={data.datahub_url} target="_blank" rel="noreferrer noopener">
                <ExternalLink size={14} aria-hidden="true" />
                View in DataHub
              </a>
            ) : (
              <span className="muted" style={{ fontSize: 13 }}>
                no DataHub deep link returned
              </span>
            )}
          </div>
        </div>
        <div className="row" style={{ gap: 18, fontSize: 14 }}>
          <span className="dim">
            rows <span className="tnum">{formatCount(data.row_count)}</span>
          </span>
          <span className="dim" title={exactBytes(data.size_bytes)}>
            size {formatBytes(data.size_bytes)}
          </span>
          <span className="dim">
            downstream <span className="tnum">{formatCount(data.downstream_count)}</span>
          </span>
          <span className="dim">
            confidence <Confidence value={data.confidence} />
          </span>
        </div>
      </div>

      {/* --------------------------------------------------------- HERO */}
      <RangeSafetyTimeline
        datasetId={datasetId}
        datasetName={data.name}
        dateColumn={data.date_column}
        minDate={data.min_date}
        maxDate={data.max_date}
        windows={data.context.downstream}
        cutoff={cutoff ?? data.min_date ?? ''}
        onCutoffChange={changeCutoff}
        verdict={verdict}
        pending={simPending}
        error={simError}
      />

      <div className="section">
        <div className="section-head">
          <h2 className="section-title">Consumer windows</h2>
          <span className="section-note">
            the table view of the chart above, including the SQL each window was parsed from
          </span>
        </div>
        <ConsumerImpactTable
          verdict={verdict}
          windows={data.context.downstream}
          fresh={verdictFresh}
        />
      </div>

      {/* ------------------------------------------------- evidence + heat */}
      <div className="split">
        <div className="stack">
          <div className="card">
            <div className="section-head">
              <h2 className="section-title">Evidence</h2>
              <span className="section-note">each row names the system it came from</span>
            </div>
            <EvidenceGraph evidence={data.evidence} />
          </div>

          <div className="card">
            <div className="section-head">
              <h2 className="section-title">Policy blockers</h2>
              <span className="section-note">{data.blockers.length} on this dataset</span>
            </div>
            <BlockerList blockers={data.blockers} />
          </div>
        </div>

        <div className="stack">
          <div className="card">
            <div className="section-head">
              <h2 className="section-title">Temperature breakdown</h2>
              <span className="section-note">deterministic, four components</span>
            </div>
            <TemperaturePanel temperature={data.temperature} />
          </div>

          <DatasetContextPanel context={data.context} />
        </div>
      </div>

      {/* ------------------------------------------------ plan + approve */}
      <div className="card">
        <div className="section-head">
          <h2 className="section-title">Plan and approve</h2>
          <span className="section-note">execution requires a plan hash and a named approver</span>
        </div>

        <div className="row" style={{ marginBottom: 12 }}>
          <button
            type="button"
            className="btn"
            onClick={() => void buildPlan()}
            disabled={planPending || cutoff === null}
          >
            <FileCheck2 size={15} aria-hidden="true" />
            {planPending ? 'Building plan…' : `Build plan for ${cutoff ?? '—'}`}
          </button>
          {plan && !planMatchesCutoff ? (
            <Chip tone="warning">plan is stale — built for {plan.cutoff_date}</Chip>
          ) : null}
        </div>

        {planError ? (
          <div className="stack">
            <ErrorState title="The backend refused to build this plan" message={planError} />
            {planErrorDetail && typeof planErrorDetail === 'object' ? (
              <details className="expander">
                <summary>Refusal detail from the API</summary>
                <div className="expander-body">
                  <pre className="code">{JSON.stringify(planErrorDetail, null, 2)}</pre>
                </div>
              </details>
            ) : null}
          </div>
        ) : null}

        {plan ? (
          <div className="stack">
            <dl className="kv">
              <dt>plan hash</dt>
              <dd className="mono">{plan.plan_hash}</dd>
              <dt>cutoff</dt>
              <dd className="mono">{plan.cutoff_date}</dd>
              <dt>rows in scope</dt>
              <dd className="tnum">{formatCount(plan.rows_in_scope)}</dd>
              <dt>bytes in scope</dt>
              <dd title={exactBytes(plan.bytes_in_scope)}>{formatBytes(plan.bytes_in_scope)}</dd>
              <dt>monthly saving</dt>
              <dd>{formatUsd(plan.monthly_savings_usd)}</dd>
              <dt>verdict</dt>
              <dd>{plan.verdict.recommendation.replace(/_/g, ' ')}</dd>
              <dt>created</dt>
              <dd>{formatTimestamp(plan.created_at)}</dd>
            </dl>

            {plan.blockers.length > 0 ? <BlockerList blockers={plan.blockers} /> : null}

            <div className="row" style={{ alignItems: 'flex-end', gap: 12 }}>
              <div className="field" style={{ minWidth: 260 }}>
                <label className="field-label" htmlFor="approved-by">
                  Approved by
                </label>
                <input
                  id="approved-by"
                  className="input"
                  type="text"
                  placeholder="you@company.com"
                  value={approvedBy}
                  onChange={(event) => setApprovedBy(event.target.value)}
                  autoComplete="off"
                />
              </div>

              <button
                type="button"
                className="btn primary"
                onClick={() => void runExecute()}
                disabled={!canApprove}
                title={plan.plan_hash}
              >
                <Play size={15} aria-hidden="true" />
                {execPending
                  ? 'Executing…'
                  : `Approve & execute plan ${plan.plan_hash.slice(0, 12)}`}
              </button>

              {approvalBlockedReason ? (
                <span className="row" style={{ gap: 6, fontSize: 13 }}>
                  <StatusIcon tone="warning" size={14} />
                  {approvalBlockedReason}
                </span>
              ) : (
                <span className="row" style={{ gap: 6, fontSize: 13 }}>
                  <ShieldCheck size={14} color="var(--good)" aria-hidden="true" />
                  Verified plan, server verdict {plan.verdict.recommendation.replace(/_/g, ' ')}
                </span>
              )}
            </div>
          </div>
        ) : (
          <div className="muted" style={{ fontSize: 14 }}>
            No plan built for this cutoff yet. A plan binds the dataset, the cutoff, the row count
            and the verdict into one hash, and execution will only accept that hash.
          </div>
        )}

        {execError ? (
          <div style={{ marginTop: 12 }}>
            <ErrorState title="Execution failed" message={execError} />
          </div>
        ) : null}
      </div>

      {execution ? <ExecutionReport result={execution} /> : null}
    </div>
  );
}
