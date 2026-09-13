import { TestBed } from '@angular/core/testing';
import { Component, WritableSignal, input, signal } from '@angular/core';
import { beforeEach, describe, expect, it } from 'vitest';

import { Chart, ChartSeries } from '../shared/chart';
import { DailyBar, VwapSeries } from '../core/models';
import { DashboardStore } from '../core/store';
import { Analytics } from './analytics';

/**
 * Stands in for the real chart, which draws to a canvas jsdom does not have.
 * Swapping it out leaves exactly what is worth asserting: the series the panel
 * hands the chart, which is where the null handling and unit conversion live.
 */
@Component({ selector: 'app-chart', template: '' })
class ChartStub {
  readonly series = input.required<readonly ChartSeries[]>();
  readonly height = input(340);
  readonly intraday = input(false);
  static last: ChartStub[] = [];
  constructor() {
    ChartStub.last.push(this);
  }
}

function resource<T>(value: T) {
  return { value: signal(value), error: signal<unknown>(undefined), isLoading: signal(false) };
}

function bar(over: Partial<DailyBar> = {}): DailyBar {
  return {
    contract: 'CLZ24',
    trading_date: '2024-06-03',
    open: 78.0,
    high: 78.5,
    low: 77.5,
    close: 78.2,
    volume: 1000,
    open_interest: null,
    bar_count: 390,
    ...over,
  };
}

describe('Analytics', () => {
  let store: {
    dailyBars: ReturnType<typeof resource<DailyBar[]>>;
    vwap: ReturnType<typeof resource<VwapSeries | undefined>>;
    frequency: WritableSignal<'minute' | 'daily'>;
    vwapWindow: WritableSignal<number>;
  };

  beforeEach(() => {
    ChartStub.last = [];
    store = {
      dailyBars: resource<DailyBar[]>([]),
      vwap: resource<VwapSeries | undefined>(undefined),
      frequency: signal<'minute' | 'daily'>('minute'),
      vwapWindow: signal(15),
    };

    TestBed.configureTestingModule({
      imports: [Analytics],
      providers: [{ provide: DashboardStore, useValue: store }],
    });
    TestBed.overrideComponent(Analytics, {
      remove: { imports: [Chart] },
      add: { imports: [ChartStub] },
    });
  });

  function render() {
    const fixture = TestBed.createComponent(Analytics);
    fixture.detectChanges();
    return fixture;
  }

  function text(fixture: ReturnType<typeof render>): string {
    return fixture.nativeElement.textContent ?? '';
  }

  /**
   * Group 1 -- the four figures above the charts.
   *
   * Each is a single number the reader takes at face value, so these are as much
   * about what the panel declines to compute as about what it does.
   */
  describe('the headline figures', () => {
    it('measures the range change close to close, not high to low', () => {
      // The 500 high on the second bar is the trap: a change taken from extremes would
      // report a move the contract never closed at.
      store.dailyBars.value.set([
        bar({ trading_date: '2024-06-03', close: 100 }),
        bar({ trading_date: '2024-06-04', high: 500, close: 110 }),
      ]);

      expect(text(render())).toContain('+10.00%');
    });

    it('reports a fall with its sign', () => {
      // Paired with the test above so the sign is proven in both directions rather than
      // inferred from the gain alone.
      store.dailyBars.value.set([
        bar({ close: 100 }),
        bar({ trading_date: '2024-06-04', close: 90 }),
      ]);

      expect(text(render())).toContain('-10.00%');
    });

    it('declines to compute a change from a single session', () => {
      // One close is a level, not a change. Reporting 0% would read as "flat", which is
      // a claim the data does not support.
      store.dailyBars.value.set([bar({ close: 100 })]);

      expect(text(render())).toContain('—');
    });

    it('declines to divide by an opening close of zero', () => {
      // Dividing by a zero opening close gives an infinite percentage, which renders as
      // a nonsense figure instead of as a missing one.
      store.dailyBars.value.set([
        bar({ close: 0 }),
        bar({ trading_date: '2024-06-04', close: 10 }),
      ]);

      expect(text(render())).toContain('—');
    });

    it('sums volume across the range, treating an absent volume as none', () => {
      // Null coalesces to zero for the sum only. A headline total over many sessions is
      // still worth showing when one of them is missing its volume.
      store.dailyBars.value.set([
        bar({ volume: 1000 }),
        bar({ trading_date: '2024-06-04', volume: null }),
        bar({ trading_date: '2024-06-05', volume: 500 }),
      ]);

      expect(text(render())).toContain('1,500');
    });
  });

  /**
   * Group 2 -- what the panel hands the OHLCV chart.
   *
   * The chart is stubbed, so this is the only place the filtering and the keying
   * that sit between a stored bar and a drawn candle are checked.
   */
  describe('the candlestick series', () => {
    it('drops a bar that is missing any of its four prices', () => {
      // lightweight-charts cannot draw a partial candle, and a zero-filled one
      // would be a lie about the session.
      store.dailyBars.value.set([
        bar({ trading_date: '2024-06-03' }),
        bar({ trading_date: '2024-06-04', high: null }),
      ]);

      render();
      const series = ChartStub.last[0].series()[0];
      expect(series.kind).toBe('candlestick');
      expect(series.data).toHaveLength(1);
    });

    it('keys candles by trading date rather than by timestamp', () => {
      // The key is the stored trading_date string, a Chicago calendar date, so the chart
      // needs no time-zone conversion for candles.
      store.dailyBars.value.set([bar({ trading_date: '2024-06-03' })]);

      render();
      expect(ChartStub.last[0].series()[0].data[0]).toMatchObject({ time: '2024-06-03' });
    });

    it('draws volume on the same chart, keyed by the same trading date', () => {
      // One chart, so a volume bar sits directly under the candle for its session.
      store.dailyBars.value.set([bar({ trading_date: '2024-06-03', volume: 1200 })]);

      render();
      const [, volume] = ChartStub.last[0].series();
      expect(volume.kind).toBe('volume');
      expect(volume.data[0]).toMatchObject({ time: '2024-06-03', value: 1200 });
    });

    it('keeps the volume of a session whose candle was dropped, and drops an absent volume', () => {
      // Missing prices do not erase what traded, and a null volume is not drawn as zero.
      store.dailyBars.value.set([
        bar({ trading_date: '2024-06-03', high: null, volume: 800 }),
        bar({ trading_date: '2024-06-04', volume: null }),
      ]);

      render();
      const [candles, volume] = ChartStub.last[0].series();
      expect(candles.data).toHaveLength(1);
      expect(volume.data).toEqual([expect.objectContaining({ time: '2024-06-03', value: 800 })]);
    });

    it('colours a volume bar by whether its session closed down', () => {
      store.dailyBars.value.set([
        bar({ trading_date: '2024-06-03', open: 78, close: 79 }),
        bar({ trading_date: '2024-06-04', open: 79, close: 78 }),
      ]);

      render();
      const [, volume] = ChartStub.last[0].series();
      const [up, down] = volume.data as { color?: string }[];
      expect(up.color).not.toBe(down.color);
    });
  });

  /**
   * Group 3 -- the rolling VWAP card.
   *
   * The daily fallback, the epoch-second conversion the line series needs, and the
   * two labels that stop a partial or rewindowed series from being misread.
   */
  describe('the VWAP panel', () => {
    function series(over: Partial<VwapSeries> = {}): VwapSeries {
      return {
        contract: 'CLZ24',
        window_minutes: 15,
        total_points: 2,
        truncated: false,
        points: [
          { ts: '2024-06-03T14:00:00Z', typical_price: 78.0, vwap: 77.9 },
          { ts: '2024-06-03T14:01:00Z', typical_price: 78.1, vwap: 78.0 },
        ],
        ...over,
      };
    }

    it('explains itself on daily data instead of drawing an empty chart', () => {
      // It also asserts no chart is put in intraday mode, since the daily branch should
      // not be rendering an intraday time axis at all.
      store.frequency.set('daily');

      const fixture = render();
      expect(text(fixture)).toContain('needs minute bars');
      expect(ChartStub.last.every((c) => c.intraday() === false)).toBe(true);
    });

    it('hands the chart Chicago wall-clock seconds, because it labels every time as UTC', () => {
      // 14:00Z on 3 June is 09:00 in Chicago (CDT, UTC-5). The chart has no time zones,
      // so the point goes in at 09:00 "UTC" for the axis to read 09:00.
      store.dailyBars.value.set([bar()]);
      store.vwap.value.set(series());

      render();
      const [typical, vwap] = ChartStub.last[1].series();
      const chicago0900 = Date.UTC(2024, 5, 3, 9, 0) / 1000;
      expect(typical.data[0]).toMatchObject({ time: chicago0900, value: 78.0 });
      expect(vwap.data[0]).toMatchObject({ time: chicago0900, value: 77.9 });
    });

    it('drops a point whose VWAP is undefined without dropping its typical price', () => {
      // VWAP is null across a zero-volume window; the typical price still exists.
      store.dailyBars.value.set([bar()]);
      store.vwap.value.set(
        series({
          points: [
            { ts: '2024-06-03T14:00:00Z', typical_price: 78.0, vwap: null },
            { ts: '2024-06-03T14:01:00Z', typical_price: 78.1, vwap: 78.0 },
          ],
        }),
      );

      render();
      const [typical, vwap] = ChartStub.last[1].series();
      expect(typical.data).toHaveLength(2);
      expect(vwap.data).toHaveLength(1);
    });

    it('drops the repeated hour when Chicago falls back from daylight saving', () => {
      // 06:30Z and 07:30Z on 3 November 2024 are both 01:30 in Chicago. The chart throws on
      // a time that does not increase, so the second pass is dropped.
      store.vwap.value.set(
        series({
          points: [
            { ts: '2024-11-03T06:30:00Z', typical_price: 78.0, vwap: 77.9 },
            { ts: '2024-11-03T07:30:00Z', typical_price: 78.1, vwap: 78.0 },
            { ts: '2024-11-03T08:30:00Z', typical_price: 78.2, vwap: 78.1 },
          ],
        }),
      );

      render();
      const [typical] = ChartStub.last.at(-1)!.series();
      expect(typical.data).toHaveLength(2);
      expect(typical.data[1]).toMatchObject({ value: 78.2 });
    });

    it('names the window the user actually chose', () => {
      // The heading is the only place the window appears, so a stale 15 there would
      // mislabel a chart that is otherwise correct.
      store.vwapWindow.set(60);

      expect(text(render())).toContain('Rolling 60-minute VWAP');
    });
  });
});
