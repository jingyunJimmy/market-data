/** Display formatting. Nulls become an em dash so empty cells read as "no value". */

export const DASH = '—';

const decimal = new Intl.NumberFormat('en-GB', { maximumFractionDigits: 4 });
const whole = new Intl.NumberFormat('en-GB', { maximumFractionDigits: 0 });

export function price(value: number | null | undefined): string {
  return value == null ? DASH : decimal.format(value);
}

export function count(value: number | null | undefined): string {
  return value == null ? DASH : whole.format(value);
}

export function percent(value: number | null | undefined): string {
  return value == null ? DASH : `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`;
}
