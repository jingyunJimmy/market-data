import { describe, expect, it } from 'vitest';

import { count, percent, price } from './format';

/** What every formatter renders when there is nothing to render. */
const DASH = '—';

describe('price', () => {
  it('renders an absent value as a dash, never as zero', () => {
    // "no trade" and "traded at zero" are different claims about a bar.
    expect(price(null)).toBe(DASH);
    expect(price(undefined)).toBe(DASH);
    expect(price(0)).toBe('0');
  });

  it('keeps four decimals, which is finer than any contract tick', () => {
    // Four decimals is a cap, not a pad: the fifth place rounds away, and a whole
    // price stays short instead of being padded out to 78.1000.
    expect(price(78.12345)).toBe('78.1235');
    expect(price(78.1)).toBe('78.1');
  });

  it('groups thousands so a five-figure price stays readable', () => {
    // Index futures trade in the thousands, where an ungrouped five figures is hard
    // to scan in a dense table of prices.
    expect(price(12345.5)).toBe('12,345.5');
  });
});

describe('count', () => {
  it('renders an absent value as a dash', () => {
    // The same rule as price, and it matters most here: a bar with no volume field
    // is not a bar that traded nothing.
    expect(count(null)).toBe(DASH);
    expect(count(undefined)).toBe(DASH);
  });

  it('shows whole units, because a fractional volume is meaningless', () => {
    // An averaged or aggregated volume can arrive fractional, so it rounds to the
    // nearest whole unit rather than truncating towards zero.
    expect(count(1234)).toBe('1,234');
    expect(count(1234.7)).toBe('1,235');
  });
});

describe('percent', () => {
  it('renders an absent value as a dash', () => {
    // The range change is null whenever it cannot be computed, which is a different
    // claim from a flat +0.00%.
    expect(percent(null)).toBe(DASH);
    expect(percent(undefined)).toBe(DASH);
  });

  it('signs a gain explicitly so direction reads without comparing to a baseline', () => {
    // Left to itself a gain would render bare. The explicit plus means direction is
    // readable without knowing what the baseline was.
    expect(percent(1.5)).toBe('+1.50%');
    expect(percent(-1.5)).toBe('-1.50%');
  });

  it('treats zero as non-negative rather than dropping the sign', () => {
    // Zero sits on the signed side, so a column of figures keeps one shape and flat
    // does not look like a special case.
    expect(percent(0)).toBe('+0.00%');
  });
});
