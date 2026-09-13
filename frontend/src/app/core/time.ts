/**
 * Exchange-time display.
 *
 * Storage and the API are UTC throughout. The UI shows everything in Chicago
 * time instead, because the date filter, `trading_date` and the daily candles
 * are already keyed on the Chicago calendar -- so a chart and the filter above
 * it agree on where a day starts and ends.
 */

import { DASH } from './format';

/** Must match the backend's `daily_bar_tz` setting, which derives `trading_date`. */
export const EXCHANGE_TZ = 'America/Chicago';

const dateTimeFormat = new Intl.DateTimeFormat('en-GB', {
  dateStyle: 'short',
  timeStyle: 'short',
  timeZone: EXCHANGE_TZ,
});

const clockFormat = new Intl.DateTimeFormat('en-GB', { timeStyle: 'short', timeZone: EXCHANGE_TZ });

// Only ever read through formatToParts, so numeric fields and a 23-hour cycle
// (midnight is 00, never 24) matter more than how it would print.
const partsFormat = new Intl.DateTimeFormat('en-US', {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hourCycle: 'h23',
  timeZone: EXCHANGE_TZ,
});

function wallParts(epochMs: number): Record<string, number> {
  const out: Record<string, number> = {};
  for (const part of partsFormat.formatToParts(epochMs)) {
    if (part.type !== 'literal') out[part.type] = Number(part.value);
  }
  return out;
}

const pad = (n: number) => String(n).padStart(2, '0');

/** Date and clock time in Chicago, e.g. `03/06/2024, 09:00`. */
export function exchangeDateTime(iso: string | null | undefined): string {
  // The empty string counts as absent: `new Date('')` formats as Invalid Date.
  return iso ? dateTimeFormat.format(new Date(iso)) : DASH;
}

/** Clock time alone in Chicago, e.g. `09:00`. */
export function exchangeClock(iso: string): string {
  return clockFormat.format(new Date(iso));
}

/** The Chicago calendar date `iso` falls on, as `YYYY-MM-DD`. */
export function exchangeDate(iso: string): string {
  const p = wallParts(Date.parse(iso));
  return `${p['year']}-${pad(p['month'])}-${pad(p['day'])}`;
}

const offsets = new Map<number, number>();

/**
 * Seconds Chicago is ahead of UTC at `epochSeconds` (negative: -6h or -5h).
 *
 * Cached per UTC hour, which is exact: the offset is a whole number of hours and
 * daylight saving switches on the hour.
 */
export function exchangeOffsetSeconds(epochSeconds: number): number {
  const hourStart = Math.floor(epochSeconds / 3600) * 3600;
  let offset = offsets.get(hourStart);
  if (offset === undefined) {
    const p = wallParts(hourStart * 1000);
    const wall = Date.UTC(p['year'], p['month'] - 1, p['day'], p['hour'], p['minute'], p['second']);
    offset = wall / 1000 - hourStart;
    if (offsets.size > 100_000) offsets.clear();
    offsets.set(hourStart, offset);
  }
  return offset;
}

/**
 * `iso` as the epoch seconds of its Chicago wall-clock time.
 *
 * lightweight-charts has no notion of a time zone and labels every timestamp as
 * UTC, so a chart is put into Chicago time by shifting its data by the offset.
 * This is the approach the library itself recommends.
 */
export function exchangeWallSeconds(iso: string): number {
  const utc = Date.parse(iso) / 1000;
  return utc + exchangeOffsetSeconds(utc);
}
