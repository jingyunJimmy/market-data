import { describe, expect, it } from 'vitest';

import {
  exchangeClock,
  exchangeDate,
  exchangeDateTime,
  exchangeOffsetSeconds,
  exchangeWallSeconds,
} from './time';

const epoch = (iso: string) => Date.parse(iso) / 1000;

describe('exchangeDateTime', () => {
  it('renders an absent timestamp as a dash', () => {
    // The empty string is in here deliberately: the API sends one for an unknown
    // instant, and `new Date('')` would otherwise format as Invalid Date.
    expect(exchangeDateTime(null)).toBe('—');
    expect(exchangeDateTime(undefined)).toBe('—');
    expect(exchangeDateTime('')).toBe('—');
  });

  it('shows the instant in Chicago regardless of where the browser is', () => {
    // The offset on the input is honoured, not just stripped: both are 09:30 in Chicago.
    expect(exchangeDateTime('2024-03-04T15:30:00Z')).toBe('04/03/2024, 09:30');
    expect(exchangeDateTime('2024-03-04T17:30:00+02:00')).toBe('04/03/2024, 09:30');
  });

  it('applies daylight saving by date rather than a fixed offset', () => {
    // Chicago is UTC-6 in January and UTC-5 in July; a fixed offset gets one wrong.
    expect(exchangeDateTime('2024-01-15T15:30:00Z')).toBe('15/01/2024, 09:30');
    expect(exchangeDateTime('2024-07-15T15:30:00Z')).toBe('15/07/2024, 10:30');
  });
});

describe('exchangeDate', () => {
  it('takes the Chicago date, which lags the UTC date in the evening', () => {
    // 03:00Z on 4 June is still 22:00 on 3 June in Chicago -- the date the filter and
    // trading_date put that bar on.
    expect(exchangeDate('2024-06-04T03:00:00Z')).toBe('2024-06-03');
    expect(exchangeDate('2024-06-04T05:00:00Z')).toBe('2024-06-04');
  });
});

describe('exchangeClock', () => {
  it('renders midnight as 00:00, not 24:00', () => {
    // 05:00Z in June is Chicago midnight (CDT, UTC-5).
    expect(exchangeClock('2024-06-03T05:00:00Z')).toBe('00:00');
  });
});

describe('exchangeOffsetSeconds', () => {
  it('switches exactly at the daylight-saving boundary', () => {
    // Chicago springs forward at 08:00Z on 10 March 2024. The per-hour cache must not
    // smear the new offset back into the minute before.
    expect(exchangeOffsetSeconds(epoch('2024-03-10T07:59:00Z'))).toBe(-6 * 3600);
    expect(exchangeOffsetSeconds(epoch('2024-03-10T08:00:00Z'))).toBe(-5 * 3600);
  });
});

describe('exchangeWallSeconds', () => {
  it('shifts an instant so a UTC-labelled chart axis reads the Chicago wall clock', () => {
    // Read back as UTC, the shifted value is 09:00 -- Chicago's clock at 14:00Z in June.
    const shifted = new Date(exchangeWallSeconds('2024-06-03T14:00:00Z') * 1000);
    expect(shifted.toISOString()).toBe('2024-06-03T09:00:00.000Z');
  });
});
