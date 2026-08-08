'use client';

/**
 * Where the estate's rows actually sit.
 *
 * One bar per table, length proportional to its measured row count, split into
 * the three states that decide each row's fate:
 *
 *   archivable      older than every consumer's reach AND past the retention floor
 *   held by policy  provably unread by anyone, but inside the retention window
 *   in use          a consumer can still reach it
 *
 * The chart exists to make one thing obvious: **only the middle band answers to
 * configuration.** Lower `io.coldlineage.policy.retentionYears` in DataHub and
 * the amber band slides left into green. Lower it to nothing and the blue band
 * does not move by a single row, because it is fixed by what consumer SQL reads.
 * A viewer who watches that happen understands the product.
 *
 * Nothing here is estimated. Every number is counted in the warehouse, and a
 * table whose bands could not be measured says so instead of drawing a shape.
 */

import { useCallback, useState } from 'react';

import { formatCount } from '@/lib/format';
import type { DatasetSummary, RowBands } from '@/lib/types';

type BandKey = 'archivable' | 'policy' | 'inuse';

const BANDS: Array<{
  key: BandKey;
  label: string;
  legend: string;
  pick: (b: RowBands) => number;
  why: string;
}> = [
  {
    key: 'archivable',
    label: 'archivable',
    legend: 'Archivable — unread and past the floor',
    pick: (b) => b.archivable,
    why: 'No consumer reads this far back, and it is older than the retention floor.',
  },
  {
    key: 'policy',
    label: 'held by policy',
    legend: 'Held by policy — unread, but inside retention',
    pick: (b) => b.policy_held,
    why: 'Provably unread, but retention policy requires it to stay hot. This is the only band configuration can move.',
  },
  {
    key: 'inuse',
    label: 'in use',
    legend: 'In use — a consumer still reads it',
    pick: (b) => b.in_use,
    why: 'A downstream consumer can still reach these rows. No configuration change makes this band smaller.',
  },
];

const SWATCH: Record<BandKey, string> = {
  archivable: 'var(--band-archivable)',
  policy: 'var(--band-policy)',
  inuse: 'var(--band-inuse)',
};

interface Hover {
  x: number;
  y: number;
  name: string;
  band: (typeof BANDS)[number];
  rows: number;
  share: number;
  bands: RowBands;
}

export default function RowBandChart({ datasets }: { datasets: DatasetSummary[] }) {
  const [hover, setHover] = useState<Hover | null>(null);

  const onMove = useCallback((event: React.MouseEvent, next: Omit<Hover, 'x' | 'y'>) => {
    setHover({ ...next, x: event.clientX, y: event.clientY });
  }, []);

  const measured = datasets.filter((d) => d.bands && (d.bands.total ?? 0) > 0);
  if (measured.length === 0) return null;

  // One shared scale, so bar length reads as data volume across tables rather
  // than each bar being its own 100%. Comparing tables is half the point.
  const widest = Math.max(...measured.map((d) => d.bands!.total));

  return (
    <div className="section">
      <div className="section-head">
        <h2 className="section-title">Where the rows actually sit</h2>
      </div>
      <p className="section-note">
        Bar length is the measured row count. Only the amber band answers to the retention
        setting — the blue band is fixed by what downstream SQL reads, so no configuration change
        makes it smaller.
      </p>

      <div>
        <div className="bandlegend">
          {BANDS.map((band) => (
            <span className="bandlegend-item" key={band.key}>
              <i
                className="bandlegend-swatch"
                style={{ background: SWATCH[band.key] }}
                aria-hidden="true"
              />
              {band.legend}
            </span>
          ))}
        </div>

        <div className="bandchart">
          {measured.map((dataset) => {
            const bands = dataset.bands!;
            const total = bands.total;
            const segments = BANDS.map((band) => ({ band, rows: band.pick(bands) })).filter(
              (s) => s.rows > 0,
            );

            return (
              <div className="bandrow" key={dataset.id}>
                <div className="bandrow-label">
                  <div className="bandrow-name" title={dataset.name}>
                    {dataset.name}
                  </div>
                  <div className="bandrow-meta">{formatCount(total)} rows</div>
                  {bands.cutoff ? (
                    <div className="bandrow-meta">
                      cutoff <span className="mono">{bands.cutoff}</span>
                    </div>
                  ) : null}
                </div>

                <div>
                  <div className="bandtrack">
                    <div
                      className="bandbar"
                      style={{ width: `${(total / widest) * 100}%` }}
                      role="img"
                      aria-label={segments
                        .map((s) => `${formatCount(s.rows)} rows ${s.band.label}`)
                        .join('; ')}
                    >
                      {segments.map(({ band, rows }) => {
                        const share = rows / total;
                        return (
                          <div
                            key={band.key}
                            className={`bandseg ${band.key}`}
                            style={{ flex: `${share} 1 0%` }}
                            tabIndex={0}
                            onMouseMove={(event) =>
                              onMove(event, {
                                name: dataset.name,
                                band,
                                rows,
                                share,
                                bands,
                              })
                            }
                            onMouseLeave={() => setHover(null)}
                            onFocus={() => setHover(null)}
                          >
                            {/* Selective direct labels: only where the segment is
                                wide enough to hold one without collision. */}
                            {share >= 0.14 && (total / widest) * share >= 0.09 ? (
                              <span className="bandseg-value">{Math.round(share * 100)}%</span>
                            ) : null}
                          </div>
                        );
                      })}
                    </div>
                  </div>

                  <div className="bandnote">{bands.reason}</div>
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {hover ? (
        <div
          className="bandtip"
          style={{
            left: Math.min(hover.x + 14, (globalThis.innerWidth ?? 1200) - 340),
            top: hover.y + 16,
          }}
          role="status"
        >
          <div className="bandtip-head">
            <i
              className="bandlegend-swatch"
              style={{ background: SWATCH[hover.band.key] }}
              aria-hidden="true"
            />
            {hover.name} · {hover.band.label}
          </div>
          <div className="bandtip-rows">
            {formatCount(hover.rows)} rows · {(hover.share * 100).toFixed(1)}%
          </div>
          <div className="bandtip-why">{hover.band.why}</div>
          {hover.band.key !== 'inuse' && hover.bands.policy_floor ? (
            <div className="bandtip-why">
              Retention floor <span className="mono">{hover.bands.policy_floor}</span>
              {hover.bands.evidence_bound ? (
                <>
                  {' '}· consumers read back to{' '}
                  <span className="mono">{hover.bands.evidence_bound}</span>
                </>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
