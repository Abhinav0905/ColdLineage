'use client';

import { CalendarRange, Loader2 } from 'lucide-react';
import type { ReactNode } from 'react';
import { useCallback, useMemo, useRef, useState } from 'react';

import { IMPACT_LABEL, StatusIcon } from '@/components/Primitives';
import {
  addDays,
  axisTicks,
  clampDate,
  describeSpan,
  diffDays,
  parseIsoDate,
  toIsoDate,
} from '@/lib/dates';
import { formatCount, truncate } from '@/lib/format';
import type {
  ConsumerImpact,
  ConsumerWindow,
  ImpactState,
  RangeVerdict,
  Recommendation,
  WindowDerivation,
} from '@/lib/types';

/* ==========================================================================
   Range Safety Timeline

   DataHub can say a table is cold. It cannot say that rows before a date are
   cold while the last 90 days are hot -- its model is dataset- and
   column-level. This chart is that missing statement, drawn:

     - the x axis is the dataset's measured min_date..max_date
     - one bar per downstream consumer, spanning the history that consumer
       still reads (derived by parsing its real SQL out of DataHub)
     - a consumer with no lower bound gets a hatched bar across the whole axis
       and the words "unbounded scan" -- it reads everything, so nothing is safe
     - the draggable cutoff tints everything to its left as the archive region

   The verdict is the server's. The client colours bars optimistically while a
   simulate call is in flight so the flip is instant on camera, and it labels
   that state "client preview" until the server answers. It never presents a
   guess as a server verdict.
   ========================================================================== */

const VIEW_W = 1180;
const LABEL_W = 296;
const PAD_RIGHT = 84;
const PLOT_W = VIEW_W - LABEL_W - PAD_RIGHT;
const PLOT_LEFT = LABEL_W;
const PLOT_RIGHT = LABEL_W + PLOT_W;
const HEADER_H = 56;
const ROW_H = 58;
const BAR_H = 18;
const FOOTER_H = 32;
const FLAG_W = 96;

/** Client-side preview threshold only. The server owns the real margin. */
const PREVIEW_TIGHT_DAYS = 45;

/* Token references, not hex. SVG `fill`/`stroke` resolve var() the same as CSS
   does, so this chart re-skins with the rest of the app instead of drifting out
   of step -- which is exactly what it had done: these were the pre-reskin navy
   palette, still teal and maroon after everything else had moved on. */
const STATE_COLOR: Record<ImpactState, string> = {
  safe: 'var(--good)',
  tight: 'var(--warning)',
  blocked: 'var(--critical)',
  unknown: 'var(--serious)',
};

/* The cutoff marker is chrome, not data -- it is where the operator put the line,
   not a measurement -- so it wears the achromatic accent. */
const CUTOFF_SAFE_COLOR = 'var(--accent)';

export const DERIVATION_LABEL: Record<WindowDerivation, string> = {
  sql_predicate: 'parsed SQL predicate',
  declared_property: 'declared property',
  no_date_filter: 'no date filter',
  no_queries_observed: 'no queries observed',
  not_a_query_consumer: 'not a query consumer',
};

/** Shorter forms, for the chart's fixed-width label gutter. */
const DERIVATION_SHORT: Record<WindowDerivation, string> = {
  sql_predicate: 'SQL predicate',
  declared_property: 'declared',
  no_date_filter: 'no date filter',
  no_queries_observed: 'no queries',
  not_a_query_consumer: 'not a query consumer',
};

export function derivationLabel(derivation: WindowDerivation | string): string {
  return DERIVATION_LABEL[derivation as WindowDerivation] ?? derivation;
}

function derivationShort(derivation: WindowDerivation | string): string {
  return DERIVATION_SHORT[derivation as WindowDerivation] ?? derivation;
}

export function isUnboundedWindow(window: ConsumerWindow): boolean {
  return (
    window.earliest_date_read === null &&
    (window.derivation === 'no_date_filter' || window.derivation === 'no_queries_observed')
  );
}

interface TimelineRow {
  window: ConsumerWindow;
  /** Server impact for the current cutoff, when we have a fresh verdict. */
  server: ConsumerImpact | null;
  state: ImpactState;
  headroomDays: number | null;
  unbounded: boolean;
  /** Left edge of the bar. Null means the bar spans the whole axis. */
  start: Date | null;
}

function previewImpact(
  window: ConsumerWindow,
  cutoff: Date,
): { state: ImpactState; headroomDays: number | null } {
  if (isUnboundedWindow(window)) return { state: 'unknown', headroomDays: null };

  const earliest = parseIsoDate(window.earliest_date_read);
  if (!earliest) return { state: 'unknown', headroomDays: null };

  const headroomDays = diffDays(cutoff, earliest);
  if (headroomDays < 0) return { state: 'blocked', headroomDays };
  if (headroomDays <= PREVIEW_TIGHT_DAYS) return { state: 'tight', headroomDays };
  return { state: 'safe', headroomDays };
}

/** Kept short on purpose: this string is drawn into a 272px label gutter. */
function headroomSentence(row: TimelineRow): string {
  if (row.unbounded) return 'unbounded, nothing is safe';
  if (row.headroomDays === null) return 'no lower bound';
  if (row.headroomDays < 0) return `overlaps by ${describeSpan(row.headroomDays)}`;
  if (row.headroomDays === 0) return 'exactly on the boundary';
  return `clears by ${describeSpan(row.headroomDays)}`;
}

/** Ink that stays legible when set inside a filled mark. */
function inkOn(fill: string): string {
  return fill === '#d03b3b' || fill === '#0ca30c' ? '#ffffff' : '#06131a';
}

export interface RangeSafetyTimelineProps {
  datasetId: number;
  datasetName: string;
  dateColumn: string | null;
  minDate: string | null;
  maxDate: string | null;
  /** Lineage windows from the dataset context; used before the first verdict. */
  windows: ConsumerWindow[];
  cutoff: string;
  onCutoffChange: (cutoff: string) => void;
  verdict: RangeVerdict | null;
  pending: boolean;
  error: string | null;
}

export default function RangeSafetyTimeline(props: RangeSafetyTimelineProps) {
  const {
    datasetId,
    datasetName,
    dateColumn,
    minDate: minDateIso,
    maxDate: maxDateIso,
    windows,
    cutoff,
    onCutoffChange,
    verdict,
    pending,
    error,
  } = props;

  const figureRef = useRef<HTMLDivElement | null>(null);
  const svgRef = useRef<SVGSVGElement | null>(null);
  const [dragging, setDragging] = useState(false);
  const [hover, setHover] = useState<{ index: number; x: number; y: number } | null>(null);

  const minDate = useMemo(() => parseIsoDate(minDateIso), [minDateIso]);
  const maxDate = useMemo(() => parseIsoDate(maxDateIso), [maxDateIso]);
  const cutoffDate = useMemo(() => parseIsoDate(cutoff), [cutoff]);

  /* A verdict is authoritative only for the cutoff it was computed at. */
  const fresh = verdict !== null && verdict.cutoff_date === cutoff && !error;

  const totalDays = minDate && maxDate ? Math.max(1, diffDays(minDate, maxDate)) : 1;

  const rows: TimelineRow[] = useMemo(() => {
    const impacts = verdict?.consumers ?? [];
    const byUrn = new Map(impacts.map((impact) => [impact.window.consumer_urn, impact]));
    const source: ConsumerWindow[] =
      impacts.length > 0 ? impacts.map((impact) => impact.window) : windows;
    const cutoffAt = parseIsoDate(cutoff);

    const built: TimelineRow[] = source.map((window) => {
      const server = fresh ? byUrn.get(window.consumer_urn) ?? null : null;
      const preview = cutoffAt
        ? previewImpact(window, cutoffAt)
        : { state: 'unknown' as ImpactState, headroomDays: null };
      const unbounded = isUnboundedWindow(window);
      return {
        window,
        server,
        state: server ? server.state : preview.state,
        headroomDays: server ? server.headroom_days : preview.headroomDays,
        unbounded,
        start: unbounded ? null : parseIsoDate(window.earliest_date_read),
      };
    });

    /* Fixed order: whoever reads furthest back sits at the top, and that order
       does not change as the cutoff moves. Rows must never reshuffle under the
       pointer mid-drag. */
    return built.sort((a, b) => {
      if (a.start === null && b.start === null) {
        return a.window.consumer_name.localeCompare(b.window.consumer_name);
      }
      if (a.start === null) return -1;
      if (b.start === null) return 1;
      return a.start.getTime() - b.start.getTime();
    });
  }, [verdict, windows, fresh, cutoff]);

  const scale = useCallback(
    (date: Date): number => {
      if (!minDate) return PLOT_LEFT;
      const ratio = diffDays(minDate, date) / totalDays;
      return PLOT_LEFT + Math.max(0, Math.min(1, ratio)) * PLOT_W;
    },
    [minDate, totalDays],
  );

  const commitFromClientX = useCallback(
    (clientX: number) => {
      const svg = svgRef.current;
      if (!svg || !minDate) return;
      const rect = svg.getBoundingClientRect();
      if (rect.width === 0) return;
      const unitsPerPixel = VIEW_W / rect.width;
      const xUnits = (clientX - rect.left) * unitsPerPixel;
      const ratio = Math.max(0, Math.min(1, (xUnits - PLOT_LEFT) / PLOT_W));
      onCutoffChange(toIsoDate(addDays(minDate, Math.round(ratio * totalDays))));
    },
    [minDate, onCutoffChange, totalDays],
  );

  const handleDateInput = useCallback(
    (value: string) => {
      const parsed = parseIsoDate(value);
      if (!parsed || !minDate || !maxDate) return;
      onCutoffChange(toIsoDate(clampDate(parsed, minDate, maxDate)));
    },
    [maxDate, minDate, onCutoffChange],
  );

  const previewRecommendation: Recommendation = rows.some(
    (row) => row.state === 'blocked' || row.state === 'unknown',
  )
    ? 'DO_NOT_ARCHIVE'
    : 'SAFE_TO_ARCHIVE';

  const recommendation: Recommendation =
    fresh && verdict ? verdict.recommendation : previewRecommendation;
  const dangerous = recommendation === 'DO_NOT_ARCHIVE';

  /* ------------------------------------------------------------- guards */

  if (!dateColumn || !minDate || !maxDate || !cutoffDate) {
    return (
      <div className="timeline">
        <div className="callout warning">
          <div className="callout-head">
            <StatusIcon tone="warning" />
            Range archiving is not applicable to this dataset
          </div>
          {dateColumn
            ? `DataHub reports a date column (${dateColumn}) but no measured min/max date range came back from the warehouse, so there is no axis to cut.`
            : 'No date column was resolved for this dataset, so there is no range to reason about. ColdLineage will not invent one.'}
        </div>
      </div>
    );
  }

  /* ------------------------------------------------------------ geometry */

  const rowCount = Math.max(rows.length, 1);
  const plotTop = HEADER_H;
  const plotHeight = rowCount * ROW_H;
  const plotBottom = plotTop + plotHeight;
  const viewHeight = plotBottom + FOOTER_H;

  const cutoffX = scale(cutoffDate);
  const ticks = axisTicks(minDate, maxDate);
  const archiveDays = diffDays(minDate, cutoffDate);
  const retainedDays = diffDays(cutoffDate, maxDate);
  const cutoffOffset = Math.max(0, Math.min(totalDays, diffDays(minDate, cutoffDate)));

  const markerColor = dangerous ? STATE_COLOR.blocked : CUTOFF_SAFE_COLOR;
  const archiveFill = dangerous ? 'rgba(208,59,59,0.22)' : 'rgba(95,227,192,0.14)';
  const flagX = Math.min(
    Math.max(cutoffX - FLAG_W / 2, PLOT_LEFT - 40),
    PLOT_RIGHT + 40 - FLAG_W,
  );

  const hoveredRow =
    hover && hover.index >= 0 && hover.index < rows.length ? rows[hover.index] : null;

  const serverHeadroom = fresh && verdict ? verdict.headroom_days : null;

  const bindingSentence = ((): string | null => {
    const binding = fresh && verdict ? verdict.binding_constraint : null;
    if (binding) {
      const name = binding.window.consumer_name;
      if (binding.headroom_days === null) return `Binding constraint: ${name} — ${binding.reason}`;
      if (binding.headroom_days < 0) {
        return `Binding constraint: the cutoff overlaps ${name} by ${describeSpan(binding.headroom_days)}`;
      }
      return `Binding constraint: clears ${name} by ${describeSpan(binding.headroom_days)}`;
    }
    const unboundedRow = rows.find((row) => row.unbounded);
    if (unboundedRow) {
      return `Binding constraint: ${unboundedRow.window.consumer_name} issues an unbounded scan`;
    }
    const tightest = rows
      .filter((row) => row.headroomDays !== null)
      .sort((a, b) => (a.headroomDays as number) - (b.headroomDays as number))[0];
    if (!tightest || tightest.headroomDays === null) return null;
    return tightest.headroomDays < 0
      ? `Binding constraint: the cutoff overlaps ${tightest.window.consumer_name} by ${describeSpan(tightest.headroomDays)}`
      : `Binding constraint: clears ${tightest.window.consumer_name} by ${describeSpan(tightest.headroomDays)}`;
  })();

  return (
    <div className="timeline">
      {/* -------------------------------------------------------- verdict */}
      <div className={`verdict ${recommendation}`} role="status" aria-live="polite">
        <div>
          <div className="verdict-word">
            <StatusIcon
              tone={
                recommendation === 'SAFE_TO_ARCHIVE'
                  ? 'good'
                  : recommendation === 'ARCHIVE_WITH_REHYDRATION'
                    ? 'warning'
                    : 'critical'
              }
              size={22}
            />
            {recommendation.replace(/_/g, ' ')}
          </div>
          <div className="verdict-rationale">
            {fresh && verdict
              ? verdict.rationale
              : `Client preview for cutoff ${cutoff}: ${
                  dangerous
                    ? 'this cutoff crosses at least one consumer read window.'
                    : 'no consumer read window is crossed.'
                } The server has not confirmed this cutoff yet.`}
          </div>
          {bindingSentence ? <div className="verdict-rationale">{bindingSentence}</div> : null}
          {serverHeadroom !== null ? (
            <div className="verdict-rationale">
              Headroom to the nearest consumer: {formatCount(serverHeadroom)} days (
              {describeSpan(serverHeadroom)}).
            </div>
          ) : null}
          {error ? (
            <div className="verdict-rationale" style={{ color: '#ff9d9d' }}>
              simulate failed — {error}
            </div>
          ) : null}
        </div>

        <div className="verdict-source">
          {fresh && verdict ? (
            <>
              <div className="live">server verdict</div>
              <div>POST /api/datasets/{datasetId}/simulate</div>
              <div>cutoff {verdict.cutoff_date}</div>
            </>
          ) : (
            <>
              <div>client preview</div>
              <div>
                {pending ? (
                  <span className="row" style={{ justifyContent: 'flex-end', gap: 6 }}>
                    <Loader2 size={12} aria-hidden="true" />
                    awaiting server…
                  </span>
                ) : error ? (
                  'server did not answer'
                ) : (
                  'not yet confirmed'
                )}
              </div>
              <div>cutoff {cutoff}</div>
            </>
          )}
        </div>
      </div>

      {/* --------------------------------------------------------- figure */}
      <div className="timeline-figure" ref={figureRef} style={{ maxWidth: VIEW_W }}>
        <svg
          ref={svgRef}
          className={`timeline-svg${dragging ? ' dragging' : ''}`}
          viewBox={`0 0 ${VIEW_W} ${viewHeight}`}
          role="img"
          aria-label={`Range safety timeline for ${datasetName}. Axis ${minDateIso} to ${maxDateIso}, cutoff ${cutoff}, ${rows.length} downstream consumers, current recommendation ${recommendation}.`}
        >
          <defs>
            <pattern
              id="cl-hatch-unbounded"
              width="8"
              height="8"
              patternUnits="userSpaceOnUse"
              patternTransform="rotate(45)"
            >
              <rect width="8" height="8" fill="rgba(236,131,90,0.18)" />
              <line x1="0" y1="0" x2="0" y2="8" stroke={STATE_COLOR.unknown} strokeWidth="2.6" />
            </pattern>
          </defs>

          <rect
            x={PLOT_LEFT}
            y={plotTop}
            width={PLOT_W}
            height={plotHeight}
            fill="#0f1c2a"
            stroke="#1d3549"
            strokeWidth="1"
          />

          {/* archive region: everything strictly left of the cutoff */}
          <rect
            x={PLOT_LEFT}
            y={plotTop}
            width={Math.max(0, cutoffX - PLOT_LEFT)}
            height={plotHeight}
            fill={archiveFill}
          />

          {hover && hoveredRow ? (
            <rect
              x={0}
              y={plotTop + hover.index * ROW_H}
              width={VIEW_W}
              height={ROW_H}
              fill="rgba(255,255,255,0.045)"
            />
          ) : null}

          {ticks.map((tick) => {
            const x = scale(tick.date);
            const anchor = x - PLOT_LEFT < 24 ? 'start' : PLOT_RIGHT - x < 24 ? 'end' : 'middle';
            return (
              <g key={tick.date.toISOString()}>
                <line x1={x} y1={plotTop} x2={x} y2={plotBottom} stroke="#16293b" strokeWidth="1" />
                <text x={x} y={18} fill="#7f9ab2" fontSize="13" textAnchor={anchor} className="tnum">
                  {tick.label}
                </text>
              </g>
            );
          })}

          {rows.map((row, index) => {
            const rowTop = plotTop + index * ROW_H;
            const barY = rowTop + (ROW_H - BAR_H) / 2;
            const color = STATE_COLOR[row.state];
            const meta = [
              row.window.consumer_type,
              row.window.platform,
              derivationShort(row.window.derivation),
            ]
              .filter(Boolean)
              .join(' · ');

            let bar: ReactNode;

            if (row.unbounded) {
              const text = 'unbounded scan — reads all history';
              const pillWidth = Math.min(text.length * 6.9 + 18, PLOT_W - 16);
              bar = (
                <>
                  <rect
                    x={PLOT_LEFT}
                    y={barY}
                    width={PLOT_W}
                    height={BAR_H}
                    rx={4}
                    fill="url(#cl-hatch-unbounded)"
                  />
                  <rect
                    x={PLOT_LEFT + 8}
                    y={barY + 1}
                    width={pillWidth}
                    height={BAR_H - 2}
                    rx={4}
                    fill="#050b12"
                    opacity="0.84"
                  />
                  <text
                    x={PLOT_LEFT + 17}
                    y={barY + BAR_H / 2 + 4}
                    fill="#f6cdb8"
                    fontSize="13"
                    fontWeight="600"
                  >
                    {text}
                  </text>
                </>
              );
            } else if (!row.start) {
              bar = (
                <>
                  <rect
                    x={PLOT_LEFT}
                    y={barY + BAR_H / 2 - 2}
                    width={PLOT_W}
                    height={4}
                    rx={2}
                    fill="#2c4a64"
                  />
                  <text x={PLOT_LEFT + 10} y={barY - 1} fill="#7f9ab2" fontSize="13">
                    no date-bounded read reported
                  </text>
                </>
              );
            } else {
              const startX = scale(row.start);
              const conflict = row.state === 'blocked' && cutoffX > startX + 1;
              if (conflict) {
                /* The solid block is exactly the data this consumer still reads
                   that the cutoff would move. A 2px surface gap separates it
                   from the part of its window that survives. */
                bar = (
                  <>
                    <rect
                      x={startX}
                      y={barY}
                      width={Math.max(2, cutoffX - 1 - startX)}
                      height={BAR_H}
                      rx={4}
                      fill={STATE_COLOR.blocked}
                    />
                    <rect
                      x={cutoffX + 1}
                      y={barY}
                      width={Math.max(0, PLOT_RIGHT - cutoffX - 1)}
                      height={BAR_H}
                      rx={4}
                      fill={STATE_COLOR.blocked}
                      opacity="0.3"
                    />
                  </>
                );
              } else {
                bar = (
                  <rect
                    x={startX}
                    y={barY}
                    width={Math.max(2, PLOT_RIGHT - startX)}
                    height={BAR_H}
                    rx={4}
                    fill={color}
                    opacity="0.92"
                  />
                );
              }
            }

            return (
              <g key={row.window.consumer_urn}>
                <rect x={8} y={rowTop + 14} width={8} height={8} rx={2} fill={color} />
                <text x={24} y={rowTop + 22} fill="#e9f2fb" fontSize="14" fontWeight="600">
                  {truncate(row.window.consumer_name, 32)}
                  <title>{row.window.consumer_urn}</title>
                </text>
                <text x={24} y={rowTop + 38} fill="#7f9ab2" fontSize="13">
                  {truncate(meta, 36)}
                  <title>{meta}</title>
                </text>
                <text x={24} y={rowTop + 53} fontSize="13">
                  <tspan fill={color} fontWeight="700">
                    {IMPACT_LABEL[row.state]}
                  </tspan>
                  <tspan fill="#b3c8dc"> · {truncate(headroomSentence(row), 28)}</tspan>
                </text>
                {bar}
              </g>
            );
          })}

          {rows.length === 0 ? (
            <text
              x={PLOT_LEFT + PLOT_W / 2}
              y={plotTop + plotHeight / 2 + 5}
              fill="#7f9ab2"
              fontSize="14"
              textAnchor="middle"
            >
              DataHub lineage reports no downstream consumers for this dataset
            </text>
          ) : null}

          {/* cutoff marker */}
          <g pointerEvents="none">
            <line
              x1={cutoffX}
              y1={plotTop - 8}
              x2={cutoffX}
              y2={plotBottom + 6}
              stroke={markerColor}
              strokeWidth="2"
            />
            <rect x={flagX} y={plotTop - 32} width={FLAG_W} height="24" rx="5" fill={markerColor} />
            <text
              x={flagX + FLAG_W / 2}
              y={plotTop - 15}
              fill={inkOn(markerColor)}
              fontSize="13"
              fontWeight="700"
              textAnchor="middle"
              className="tnum"
            >
              {cutoff}
            </text>
            <polygon
              points={`${cutoffX - 5},${plotBottom + 6} ${cutoffX + 5},${plotBottom + 6} ${cutoffX},${plotBottom + 14}`}
              fill={markerColor}
            />
          </g>

          {/* axis */}
          <text x={24} y={38} fill="#b3c8dc" fontSize="13" fontWeight="600">
            {truncate(`${datasetName}.${dateColumn}`, 36)}
          </text>
          <text x={24} y={plotBottom + 24} fill="#7f9ab2" fontSize="13">
            measured range
          </text>
          <text x={PLOT_LEFT} y={plotBottom + 24} fill="#7f9ab2" fontSize="13" className="tnum">
            {minDateIso}
          </text>
          <text
            x={PLOT_RIGHT}
            y={plotBottom + 24}
            fill="#7f9ab2"
            fontSize="13"
            textAnchor="end"
            className="tnum"
          >
            {maxDateIso}
          </text>

          {/* drag + hover surface, above everything else */}
          <rect
            x={PLOT_LEFT}
            y={plotTop}
            width={PLOT_W}
            height={plotHeight}
            fill="transparent"
            style={{ cursor: 'ew-resize' }}
            onPointerDown={(event) => {
              event.currentTarget.setPointerCapture(event.pointerId);
              setDragging(true);
              setHover(null);
              commitFromClientX(event.clientX);
            }}
            onPointerMove={(event) => {
              if (dragging) {
                commitFromClientX(event.clientX);
                return;
              }
              const svg = svgRef.current;
              const figure = figureRef.current;
              if (!svg || !figure) return;
              const svgRect = svg.getBoundingClientRect();
              if (svgRect.height === 0) return;
              const figureRect = figure.getBoundingClientRect();
              const unitsPerPixel = viewHeight / svgRect.height;
              const yUnits = (event.clientY - svgRect.top) * unitsPerPixel;
              const index = Math.floor((yUnits - plotTop) / ROW_H);
              if (index < 0 || index >= rows.length) {
                setHover(null);
                return;
              }
              setHover({
                index,
                x: event.clientX - figureRect.left,
                y: event.clientY - figureRect.top,
              });
            }}
            onPointerUp={(event) => {
              event.currentTarget.releasePointerCapture(event.pointerId);
              setDragging(false);
            }}
            onPointerCancel={() => setDragging(false)}
            onPointerLeave={() => setHover(null)}
          />
        </svg>

        {/* scrubber, aligned to the plot area of the SVG directly above it */}
        <div
          style={{
            paddingLeft: `${(LABEL_W / VIEW_W) * 100}%`,
            paddingRight: `${(PAD_RIGHT / VIEW_W) * 100}%`,
          }}
        >
          <input
            className={`scrub${dangerous ? ' blocked' : ''}`}
            type="range"
            min={0}
            max={totalDays}
            step={1}
            value={cutoffOffset}
            aria-label="Archive cutoff date"
            aria-valuetext={cutoff}
            onChange={(event) =>
              onCutoffChange(toIsoDate(addDays(minDate, Number(event.target.value))))
            }
          />
        </div>

        {hover && hoveredRow && !dragging ? (
          <div
            className="timeline-tip"
            style={{
              left: Math.max(
                8,
                Math.min(hover.x + 16, (figureRef.current?.clientWidth ?? 900) - 348),
              ),
              top: Math.max(8, hover.y - 12),
            }}
          >
            <div className="tip-value">{hoveredRow.window.consumer_name}</div>
            <div className="muted">
              {hoveredRow.window.consumer_type}
              {hoveredRow.window.platform ? ` · ${hoveredRow.window.platform}` : ''} · degree{' '}
              {hoveredRow.window.degree}
            </div>
            <div>
              reads back to{' '}
              <span className="mono">{hoveredRow.window.earliest_date_read ?? 'no lower bound'}</span>
            </div>
            <div className="muted">derivation {derivationLabel(hoveredRow.window.derivation)}</div>
            <div>
              <span style={{ color: STATE_COLOR[hoveredRow.state], fontWeight: 700 }}>
                {IMPACT_LABEL[hoveredRow.state]}
              </span>{' '}
              · {headroomSentence(hoveredRow)}
            </div>
            {hoveredRow.server ? <div className="muted">{hoveredRow.server.reason}</div> : null}
            <div className="muted mono" style={{ fontSize: 12 }}>
              {hoveredRow.window.provenance.source}
            </div>
          </div>
        ) : null}
      </div>

      {/* ------------------------------------------------------- controls */}
      <div className="timeline-controls">
        <div className="scrub-wrap">
          <span className="field-label">Archive region</span>
          <span className="dim">
            <span className="mono">{minDateIso}</span> → <span className="mono">{cutoff}</span> ·{' '}
            {formatCount(archiveDays)} days move cold · {formatCount(retainedDays)} days stay hot
          </span>
        </div>

        <div className="field">
          <label className="field-label" htmlFor="cutoff-date">
            Cutoff date
          </label>
          <input
            id="cutoff-date"
            className="input mono"
            type="date"
            value={cutoff}
            min={minDateIso ?? undefined}
            max={maxDateIso ?? undefined}
            onChange={(event) => handleDateInput(event.target.value)}
          />
        </div>

        <div className="field">
          <span className="field-label">Simulation</span>
          <span className="dim row" style={{ gap: 6 }}>
            <CalendarRange size={14} aria-hidden="true" />
            {pending ? 'simulating…' : fresh ? 'confirmed by server' : 'preview only'}
          </span>
        </div>
      </div>

      {/* --------------------------------------------------------- legend */}
      <div className="timeline-legend">
        <span className="key">
          <i style={{ background: STATE_COLOR.safe }} aria-hidden="true" />
          safe
        </span>
        <span className="key">
          <i style={{ background: STATE_COLOR.tight }} aria-hidden="true" />
          tight
        </span>
        <span className="key">
          <i style={{ background: STATE_COLOR.blocked }} aria-hidden="true" />
          blocked — the solid block is data the cutoff would move
        </span>
        <span className="key">
          <i className="hatch" aria-hidden="true" />
          unbounded scan
        </span>
        <span className="key">
          <i className="cutline" style={{ background: markerColor }} aria-hidden="true" />
          cutoff
        </span>
        <span className="muted">each bar spans the history that consumer still reads</span>
      </div>
    </div>
  );
}
