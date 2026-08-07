/** Formatting helpers. Every one of these derives its output from the value it
 *  is given -- there is no place in this file for a constant that stands in for
 *  a measurement. `null` renders as an em dash, never as a plausible number. */

export const EMPTY = '—';

const COUNT_FORMAT = new Intl.NumberFormat('en-US');
const COMPACT_FORMAT = new Intl.NumberFormat('en-US', {
  notation: 'compact',
  maximumFractionDigits: 1,
});
const USD_FORMAT = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  maximumFractionDigits: 2,
});

const BYTE_UNITS = ['B', 'kB', 'MB', 'GB', 'TB', 'PB'];

/** SI byte formatting computed from the actual value (1 kB = 1000 B, which is
 *  the unit cloud storage is billed in). */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || Number.isNaN(bytes)) return EMPTY;
  if (bytes === 0) return '0 B';

  const magnitude = Math.abs(bytes);
  const exponent = Math.min(
    Math.max(Math.floor(Math.log10(magnitude) / 3), 0),
    BYTE_UNITS.length - 1,
  );
  const scaled = bytes / 1000 ** exponent;
  const digits = exponent === 0 ? 0 : scaled >= 100 ? 0 : scaled >= 10 ? 1 : 2;
  return `${scaled.toFixed(digits)} ${BYTE_UNITS[exponent]}`;
}

/** Exact byte count, for a `title` attribute beside the rounded value. */
export function exactBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return 'not measured';
  return `${COUNT_FORMAT.format(bytes)} bytes`;
}

export function formatCount(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return EMPTY;
  return COUNT_FORMAT.format(value);
}

export function formatCompact(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return EMPTY;
  return COMPACT_FORMAT.format(value);
}

export function formatUsd(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return EMPTY;
  return USD_FORMAT.format(value);
}

export function formatPercent(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return EMPTY;
  return `${(value * 100).toFixed(digits)}%`;
}

/**
 * Parse a backend timestamp. FastAPI serialises naive UTC datetimes without a
 * zone designator, and JavaScript would read those as *local* time and silently
 * shift them. Anything zone-less is therefore read as UTC, which is what the
 * backend means.
 */
export function parseTimestamp(value: string): Date {
  const hasZone = /(?:Z|z|[+-]\d{2}:?\d{2})$/.test(value);
  const normalised = value.includes('T') && !hasZone ? `${value}Z` : value;
  return new Date(normalised);
}

/** "2026-08-06 14:22 UTC" -- deterministic, so it cannot disagree between the
 *  server render and the browser render. */
export function formatTimestamp(value: string | null | undefined): string {
  if (!value) return EMPTY;
  const parsed = parseTimestamp(value);
  if (Number.isNaN(parsed.getTime())) return value;
  const iso = parsed.toISOString();
  return `${iso.slice(0, 10)} ${iso.slice(11, 16)} UTC`;
}

/**
 * Coerce an audit detail payload to text. The contract says this field is a
 * string, but a backend that hands back a JSON object must not white-screen the
 * page, so both shapes are handled.
 */
export function detailText(detail: unknown): string {
  if (detail === null || detail === undefined) return '';
  if (typeof detail === 'string') return detail;
  try {
    return JSON.stringify(detail, null, 2);
  } catch {
    return String(detail);
  }
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return EMPTY;
  return value.slice(0, 10);
}

export function truncate(value: string, max: number): string {
  if (value.length <= max) return value;
  return `${value.slice(0, Math.max(1, max - 1))}…`;
}

/** Last segment of a DataHub URN, which is the part a human reads. */
export function shortUrn(urn: string | null | undefined): string {
  if (!urn) return EMPTY;
  const inner = urn.replace(/^urn:li:[a-zA-Z]+:\(?/, '').replace(/\)$/, '');
  const parts = inner.split(',');
  return parts.length > 1 ? parts[parts.length - 2] || inner : inner;
}

export function shortHash(value: string | null | undefined, length = 12): string {
  if (!value) return EMPTY;
  return value.length <= length ? value : `${value.slice(0, length)}…`;
}
