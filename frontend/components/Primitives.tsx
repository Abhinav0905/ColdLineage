'use client';

import {
  AlertTriangle,
  CheckCircle2,
  HelpCircle,
  Loader2,
  RefreshCw,
  XCircle,
} from 'lucide-react';
import type { ReactNode } from 'react';

import { detailText, formatPercent } from '@/lib/format';
import type {
  ImpactState,
  Provenance,
  TemperatureBreakdown,
  TemperatureClass,
} from '@/lib/types';

/* -------------------------------------------------------------------------
   Provenance
   ------------------------------------------------------------------------- */

function provenanceFamily(source: string): string {
  if (source.startsWith('datahub:')) return 'src-datahub';
  if (source.startsWith('warehouse:')) return 'src-warehouse';
  if (source.startsWith('cassette:')) return 'src-cassette';
  return 'src-unavailable';
}

/**
 * The small monospace chip that names the system a value came from.
 * It is deliberately literal -- it prints the source string the API returned,
 * so a viewer can see for themselves that the input came from DataHub.
 */
export function ProvenanceChip({ provenance }: { provenance: Provenance | null | undefined }) {
  if (!provenance) {
    return (
      <span className="prov src-unavailable" title="No provenance supplied by the API">
        unavailable
      </span>
    );
  }
  const title = [provenance.detail, provenance.observed_at ? `observed ${provenance.observed_at}` : null]
    .filter(Boolean)
    .join(' · ');
  return (
    <span className={`prov ${provenanceFamily(provenance.source)}`} title={title || provenance.source}>
      {provenance.source}
    </span>
  );
}

/* -------------------------------------------------------------------------
   Temperature
   ------------------------------------------------------------------------- */

export const TEMPERATURE_ORDER: TemperatureClass[] = ['HOT', 'WARM', 'COOL', 'COLD', 'FROZEN'];

const TEMPERATURE_SWATCH: Record<TemperatureClass, string> = {
  HOT: 'var(--temp-hot)',
  WARM: 'var(--temp-warm)',
  COOL: 'var(--temp-cool)',
  COLD: 'var(--temp-cold)',
  FROZEN: 'var(--temp-frozen)',
};

export function TemperaturePill({ classification }: { classification: TemperatureClass }) {
  return (
    <span className={`temp ${classification}`}>
      <i className="temp-dot" aria-hidden="true" />
      {classification}
    </span>
  );
}

export function TemperatureMeter({ temperature }: { temperature: TemperatureBreakdown }) {
  const width = `${Math.max(0, Math.min(100, temperature.score))}%`;
  return (
    <div
      className="meter"
      role="img"
      aria-label={`Temperature score ${temperature.score.toFixed(1)} of 100, classified ${temperature.classification}`}
      title={`score ${temperature.score.toFixed(1)} / 100`}
    >
      <div className={`meter-fill ${temperature.classification}`} style={{ width }} />
    </div>
  );
}

/** Required by the semantic-heat exception: a multi-hue scale ships a legend. */
export function HeatLegend() {
  return (
    <div className="heat-legend">
      <span>temperature scale</span>
      {TEMPERATURE_ORDER.map((klass) => (
        <span className="swatch" key={klass}>
          <i style={{ background: TEMPERATURE_SWATCH[klass] }} aria-hidden="true" />
          {klass}
        </span>
      ))}
      <span className="muted">score 0-100, hotter = more recently and heavily read</span>
    </div>
  );
}

/* -------------------------------------------------------------------------
   Status glyphs -- a status colour is never allowed to travel alone
   ------------------------------------------------------------------------- */

export type StatusTone = 'good' | 'warning' | 'serious' | 'critical' | 'neutral';

const TONE_COLOR: Record<StatusTone, string> = {
  good: 'var(--good)',
  warning: 'var(--warning)',
  serious: 'var(--serious)',
  critical: 'var(--critical)',
  neutral: 'var(--ink-3)',
};

export function StatusIcon({ tone, size = 16 }: { tone: StatusTone; size?: number }) {
  const color = TONE_COLOR[tone];
  const props = { size, color, 'aria-hidden': true as const };
  if (tone === 'good') return <CheckCircle2 {...props} />;
  if (tone === 'warning') return <AlertTriangle {...props} />;
  if (tone === 'critical') return <XCircle {...props} />;
  if (tone === 'serious') return <AlertTriangle {...props} />;
  return <HelpCircle {...props} />;
}

export const IMPACT_TONE: Record<ImpactState, StatusTone> = {
  safe: 'good',
  tight: 'warning',
  blocked: 'critical',
  unknown: 'serious',
};

export const IMPACT_LABEL: Record<ImpactState, string> = {
  safe: 'SAFE',
  tight: 'TIGHT',
  blocked: 'BLOCKED',
  unknown: 'UNKNOWN',
};

export function ImpactChip({ state }: { state: ImpactState }) {
  const tone = IMPACT_TONE[state];
  return (
    <span className={`chip ${tone}`}>
      <StatusIcon tone={tone} size={13} />
      {IMPACT_LABEL[state]}
    </span>
  );
}

export function Chip({ tone = 'neutral', children }: { tone?: StatusTone; children: ReactNode }) {
  return (
    <span className={`chip ${tone === 'neutral' ? '' : tone}`}>
      {tone !== 'neutral' ? <i className="chip-dot" aria-hidden="true" /> : null}
      {children}
    </span>
  );
}

/* -------------------------------------------------------------------------
   Loading / empty / error
   ------------------------------------------------------------------------- */

export function LoadingState({ label }: { label: string }) {
  return (
    <div className="empty">
      <span className="row" style={{ justifyContent: 'center' }}>
        <Loader2 size={16} aria-hidden="true" />
        {label}
      </span>
    </div>
  );
}

export function EmptyState({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

/**
 * Inline failure. Never a white screen and never a silent fallback to
 * plausible-looking data -- the message is the message the API gave us.
 */
export function ErrorState({
  title,
  message,
  onRetry,
}: {
  title: string;
  message: string;
  onRetry?: () => void;
}) {
  return (
    <div className="error-box" role="alert">
      <div className="error-title">
        <StatusIcon tone="critical" />
        {title}
      </div>
      <div className="error-body">{message}</div>
      {onRetry ? (
        <div>
          <button type="button" className="btn small" onClick={onRetry}>
            <RefreshCw size={14} aria-hidden="true" />
            Retry
          </button>
        </div>
      ) : null}
    </div>
  );
}

/* -------------------------------------------------------------------------
   Misc
   ------------------------------------------------------------------------- */

export function Confidence({ value }: { value: number | null }) {
  if (value === null || value === undefined) {
    return <span className="muted">not reported</span>;
  }
  return <span className="tnum">{formatPercent(value, 0)}</span>;
}

/** Pretty-print a detail payload if it happens to be JSON, else show it raw. */
export function JsonDetail({ value }: { value: unknown }) {
  const raw = detailText(value);
  if (raw === '') return <span className="muted">no detail recorded</span>;
  let text = raw;
  try {
    text = JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    /* not JSON -- show verbatim */
  }
  return <pre className="code">{text}</pre>;
}
