/** Date helpers. Everything is UTC midnight so that a "YYYY-MM-DD" from the
 *  backend never shifts a day depending on where the browser is. */

export const DAY_MS = 86_400_000;

export function parseIsoDate(value: string | null | undefined): Date | null {
  if (!value) return null;
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value);
  if (!match) return null;
  const parsed = new Date(
    Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])),
  );
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

export function toIsoDate(date: Date): string {
  return date.toISOString().slice(0, 10);
}

export function addDays(date: Date, days: number): Date {
  return new Date(date.getTime() + days * DAY_MS);
}

/** Signed whole days from `from` to `to`. Positive means `to` is later. */
export function diffDays(from: Date, to: Date): number {
  return Math.round((to.getTime() - from.getTime()) / DAY_MS);
}

export function clampDate(value: Date, min: Date, max: Date): Date {
  if (value.getTime() < min.getTime()) return min;
  if (value.getTime() > max.getTime()) return max;
  return value;
}

/** "74 days" / "13 months" / "3.2 years" -- for a magnitude, sign stripped. */
export function describeSpan(days: number): string {
  const magnitude = Math.abs(days);
  if (magnitude === 0) return '0 days';
  if (magnitude === 1) return '1 day';
  if (magnitude < 60) return `${magnitude} days`;
  if (magnitude < 730) {
    const months = Math.round(magnitude / 30.44);
    return months === 1 ? '1 month' : `${months} months`;
  }
  return `${(magnitude / 365.25).toFixed(1)} years`;
}

export interface AxisTick {
  date: Date;
  label: string;
  major: boolean;
}

const MONTH_LABELS = [
  'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
];

/**
 * Ticks for the time axis: whole years when the span is long, quarters when it
 * is short. Always at least the two endpoints' years so the axis is readable.
 */
export function axisTicks(min: Date, max: Date): AxisTick[] {
  const spanDays = Math.max(1, diffDays(min, max));
  const ticks: AxisTick[] = [];

  if (spanDays > 1100) {
    // Multi-year span: one tick per January.
    for (let year = min.getUTCFullYear(); year <= max.getUTCFullYear(); year += 1) {
      const date = new Date(Date.UTC(year, 0, 1));
      if (date.getTime() < min.getTime() || date.getTime() > max.getTime()) continue;
      ticks.push({ date, label: String(year), major: true });
    }
    return ticks;
  }

  // Shorter span: quarter starts, with the year shown on Q1.
  let year = min.getUTCFullYear();
  let month = Math.floor(min.getUTCMonth() / 3) * 3;
  for (let guard = 0; guard < 64; guard += 1) {
    const date = new Date(Date.UTC(year, month, 1));
    if (date.getTime() > max.getTime()) break;
    if (date.getTime() >= min.getTime()) {
      ticks.push({
        date,
        label: month === 0 ? String(year) : `${MONTH_LABELS[month]} ${year}`,
        major: month === 0,
      });
    }
    month += 3;
    if (month > 11) {
      month = 0;
      year += 1;
    }
  }
  return ticks;
}
