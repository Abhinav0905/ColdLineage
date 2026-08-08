'use client';

import { ArrowRight, RefreshCw } from 'lucide-react';
import Link from 'next/link';
import { useMemo, useState } from 'react';

import {
  Chip,
  EmptyState,
  ErrorState,
  HeatLegend,
  LoadingState,
  TemperatureMeter,
  TemperaturePill,
} from '@/components/Primitives';
import RowBandChart from '@/components/RowBandChart';
import { api } from '@/lib/api';
import { EMPTY, exactBytes, formatBytes, formatCount } from '@/lib/format';
import type { ArchiveState, DatasetSummary } from '@/lib/types';
import { useResource } from '@/lib/useResource';

type SortKey = 'coldest' | 'largest' | 'name' | 'consumers';

const SORTS: Array<{ key: SortKey; label: string }> = [
  { key: 'coldest', label: 'Coldest first' },
  { key: 'largest', label: 'Largest measured first' },
  { key: 'consumers', label: 'Most downstream consumers' },
  { key: 'name', label: 'Name' },
];

const ARCHIVE_STATE_LABEL: Record<ArchiveState, string> = {
  HOT: 'hot',
  PARTIALLY_ARCHIVED: 'partially archived',
  REHYDRATING: 'rehydrating',
};

export default function Overview() {
  const datasets = useResource<DatasetSummary[]>((signal) => api.datasets(signal), []);
  const [sort, setSort] = useState<SortKey>('coldest');

  const rows = useMemo(() => {
    const list = [...(datasets.data ?? [])];
    switch (sort) {
      case 'largest':
        return list.sort((a, b) => (b.size_bytes ?? -1) - (a.size_bytes ?? -1));
      case 'consumers':
        return list.sort((a, b) => b.downstream_count - a.downstream_count);
      case 'name':
        return list.sort((a, b) => a.name.localeCompare(b.name));
      case 'coldest':
      default:
        return list.sort((a, b) => a.temperature.score - b.temperature.score);
    }
  }, [datasets.data, sort]);

  const stats = useMemo(() => {
    const list = datasets.data ?? [];
    const measured = list.filter((item) => item.size_bytes !== null);
    const measuredBytes = measured.reduce((sum, item) => sum + (item.size_bytes ?? 0), 0);
    const measuredRows = list
      .filter((item) => item.row_count !== null)
      .reduce((sum, item) => sum + (item.row_count ?? 0), 0);
    return {
      total: list.length,
      measuredCount: measured.length,
      measuredBytes,
      measuredRows,
      eligible: list.filter((item) => item.archive_eligible).length,
      partial: list.filter((item) => item.archive_state === 'PARTIALLY_ARCHIVED').length,
      consumers: list.reduce((sum, item) => sum + item.downstream_count, 0),
      staleSignals: list.filter((item) => !item.signals_live).length,
    };
  }, [datasets.data]);

  return (
    <>
      <div className="page-head">
        <div>
          <div className="eyebrow">Estate</div>
          <h1 className="page-title">Temperature across the catalog</h1>
          <p className="page-lede">
            DataHub can tell you a table is cold. It cannot tell you that <em>half</em> a table is
            cold — and it cannot move a single byte. Every figure below is measured or read from
            DataHub; nothing on this page is estimated.
          </p>
        </div>
        <button type="button" className="btn" onClick={datasets.reload}>
          <RefreshCw size={15} aria-hidden="true" />
          Refresh
        </button>
      </div>

      {datasets.error ? (
        <ErrorState
          title="Could not load the dataset estate"
          message={datasets.error}
          onRetry={datasets.reload}
        />
      ) : null}

      {datasets.loading && !datasets.data ? <LoadingState label="Loading /api/datasets…" /> : null}

      {datasets.data ? (
        <div className={datasets.refreshing ? 'is-refreshing' : undefined}>
          <div className="kpis">
            <div className="kpi">
              <div className="kpi-label">Datasets in scope</div>
              <div className="kpi-value">{formatCount(stats.total)}</div>
              <div className="kpi-note">
                {stats.staleSignals === 0
                  ? 'all reporting live signals'
                  : `${formatCount(stats.staleSignals)} without live signals`}
              </div>
            </div>

            <div className="kpi">
              <div className="kpi-label">Measured footprint</div>
              <div className="kpi-value" title={exactBytes(stats.measuredBytes)}>
                {formatBytes(stats.measuredBytes)}
              </div>
              <div className="kpi-note">
                summed from {formatCount(stats.measuredCount)} of {formatCount(stats.total)} datasets
                that report a measured size
              </div>
            </div>

            <div className="kpi">
              <div className="kpi-label">Rows under management</div>
              <div className="kpi-value">{formatCount(stats.measuredRows)}</div>
              <div className="kpi-note">warehouse row counts, not estimates</div>
            </div>

            <div className="kpi">
              <div className="kpi-label">Archive-eligible</div>
              <div className="kpi-value">{formatCount(stats.eligible)}</div>
              <div className="kpi-note">
                {formatCount(stats.partial)} already partially archived ·{' '}
                {formatCount(stats.consumers)} downstream consumers tracked
              </div>
            </div>
          </div>

          <RowBandChart datasets={rows} />

          <div className="section">
            <div className="section-head">
              <h2 className="section-title">Estate temperature</h2>
              <div className="row">
                <HeatLegend />
                <label className="field-label" htmlFor="estate-sort">
                  Sort
                </label>
                <select
                  id="estate-sort"
                  className="select"
                  value={sort}
                  onChange={(event) => setSort(event.target.value as SortKey)}
                >
                  {SORTS.map((option) => (
                    <option key={option.key} value={option.key}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </div>
            </div>

            {rows.length === 0 ? (
              <EmptyState>
                The backend returned no datasets. Seed the demo estate, or point ColdLineage at a
                catalog that has some.
              </EmptyState>
            ) : (
              <div className="table-scroll">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Dataset</th>
                      <th className="num">Rows</th>
                      <th className="num">Size</th>
                      <th>Measured date range</th>
                      <th>Temperature</th>
                      <th className="num">Downstream</th>
                      <th>Archive state</th>
                      <th>Blockers</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((dataset) => (
                      <tr key={dataset.id}>
                        <td>
                          <div className="cell-primary">{dataset.name}</div>
                          <div className="cell-sub">
                            {dataset.platform}
                            {dataset.domain ? ` · ${dataset.domain}` : ''}
                            {dataset.owners.length > 0 ? ` · ${dataset.owners.join(', ')}` : ''}
                          </div>
                          <div className="row" style={{ gap: 6, marginTop: 4 }}>
                            {dataset.sensitive ? <Chip tone="warning">sensitive</Chip> : null}
                            {dataset.tags.slice(0, 3).map((tag) => (
                              <Chip key={tag}>{tag}</Chip>
                            ))}
                            {!dataset.signals_live ? (
                              <Chip tone="warning">signals not live</Chip>
                            ) : null}
                          </div>
                        </td>

                        <td className="num">{formatCount(dataset.row_count)}</td>

                        <td className="num" title={exactBytes(dataset.size_bytes)}>
                          {formatBytes(dataset.size_bytes)}
                        </td>

                        <td>
                          {dataset.min_date && dataset.max_date ? (
                            <>
                              <div className="mono nowrap">
                                {dataset.min_date} → {dataset.max_date}
                              </div>
                              <div className="cell-sub mono">
                                on {dataset.date_column ?? 'unknown column'}
                              </div>
                            </>
                          ) : (
                            <span className="muted">no date range measured</span>
                          )}
                        </td>

                        <td>
                          <div className="row" style={{ gap: 8 }}>
                            <TemperaturePill classification={dataset.temperature.classification} />
                            <TemperatureMeter temperature={dataset.temperature} />
                          </div>
                          <div className="cell-sub tnum">
                            score {dataset.temperature.score.toFixed(1)}
                          </div>
                        </td>

                        <td className="num">{formatCount(dataset.downstream_count)}</td>

                        <td>
                          <Chip
                            tone={
                              dataset.archive_state === 'PARTIALLY_ARCHIVED'
                                ? 'good'
                                : dataset.archive_state === 'REHYDRATING'
                                  ? 'warning'
                                  : 'neutral'
                            }
                          >
                            {ARCHIVE_STATE_LABEL[dataset.archive_state] ?? dataset.archive_state}
                          </Chip>
                          {dataset.archive_state === 'PARTIALLY_ARCHIVED' ? (
                            <div className="cell-sub mono">
                              archived through {dataset.archived_through ?? EMPTY}
                            </div>
                          ) : null}
                        </td>

                        <td>
                          {dataset.blockers.length === 0 ? (
                            <Chip tone={dataset.archive_eligible ? 'good' : 'neutral'}>
                              {dataset.archive_eligible ? 'eligible' : 'none'}
                            </Chip>
                          ) : (
                            <div className="row" style={{ gap: 6 }}>
                              {dataset.blockers.map((blocker) => (
                                <span
                                  className="chip critical"
                                  key={blocker.code}
                                  title={`${blocker.message} (${blocker.provenance.source})`}
                                >
                                  <i className="chip-dot" aria-hidden="true" />
                                  <span className="mono">{blocker.code}</span>
                                </span>
                              ))}
                            </div>
                          )}
                        </td>

                        <td>
                          <Link
                            className="btn small"
                            href={`/candidates?dataset=${dataset.id}`}
                            aria-label={`Open ${dataset.name} in the range simulator`}
                          >
                            Simulate
                            <ArrowRight size={14} aria-hidden="true" />
                          </Link>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      ) : null}
    </>
  );
}
