import { Component, computed, inject } from '@angular/core';
import { Time, UTCTimestamp } from 'lightweight-charts';

import { DashboardStore } from '../core/store';
import { count, percent, price } from '../core/format';
import { exchangeWallSeconds } from '../core/time';
import { Chart, ChartSeries } from '../shared/chart';
import { Stat } from '../shared/stat';

const VWAP_COLOUR = '#1f5fd6';
const TYPICAL_COLOUR = '#9aa4b5';
// The candle colours at partial opacity, so the volume band stays secondary to price.
const VOLUME_UP_COLOUR = 'rgba(18, 128, 92, 0.45)';
const VOLUME_DOWN_COLOUR = 'rgba(192, 57, 43, 0.45)';

/**
 * Keep only strictly increasing times, which the chart requires (it throws otherwise).
 *
 * Chicago wall-clock time repeats for the hour it falls back from daylight saving,
 * so that hour's second pass is dropped. For CME contracts this never bites: the
 * repeated hour is 01:00-02:00 on a Sunday, inside the weekend closure.
 */
function ascending<T extends { time: UTCTimestamp }>(points: T[]): T[] {
  const kept: T[] = [];
  for (const point of points) {
    const last = kept.at(-1);
    if (!last || point.time > last.time) kept.push(point);
  }
  return kept;
}

@Component({
  selector: 'app-analytics',
  imports: [Chart, Stat],
  templateUrl: './analytics.html',
  styleUrl: './analytics.css',
})
export class Analytics {
  protected readonly store = inject(DashboardStore);
  protected readonly format = { count, percent, price };

  protected readonly bars = computed(() => this.store.dailyBars.value());

  protected readonly candles = computed<ChartSeries[]>(() => [
    {
      kind: 'candlestick',
      data: this.bars()
        .filter((b) => b.open != null && b.high != null && b.low != null && b.close != null)
        .map((b) => ({
          time: b.trading_date as Time,
          open: b.open!,
          high: b.high!,
          low: b.low!,
          close: b.close!,
        })),
    },
    {
      kind: 'volume',
      // Filtered on volume alone: a session missing a price still traded what it traded.
      data: this.bars()
        .filter((b) => b.volume != null)
        .map((b) => ({
          time: b.trading_date as Time,
          value: b.volume!,
          color:
            b.open != null && b.close != null && b.close < b.open
              ? VOLUME_DOWN_COLOUR
              : VOLUME_UP_COLOUR,
        })),
    },
  ]);

  protected readonly vwapSeries = computed<ChartSeries[]>(() => {
    const points = this.store.vwap.value()?.points ?? [];
    // Chicago wall-clock seconds rather than true epoch seconds: the chart labels
    // every time as UTC, so shifting the data is how it reads in Chicago time.
    const at = (iso: string) => exchangeWallSeconds(iso) as UTCTimestamp;
    return [
      {
        kind: 'line',
        color: TYPICAL_COLOUR,
        data: ascending(
          points
            .filter((p) => p.typical_price != null)
            .map((p) => ({ time: at(p.ts), value: p.typical_price! })),
        ),
      },
      {
        kind: 'line',
        color: VWAP_COLOUR,
        data: ascending(
          points.filter((p) => p.vwap != null).map((p) => ({ time: at(p.ts), value: p.vwap! })),
        ),
      },
    ];
  });

  /** Close-to-close move across the selected range, in percent. */
  protected readonly rangeChange = computed(() => {
    const closes = this.bars()
      .map((b) => b.close)
      .filter((c): c is number => c != null);
    if (closes.length < 2 || closes[0] === 0) return null;
    return ((closes[closes.length - 1] - closes[0]) / closes[0]) * 100;
  });

  protected readonly lastClose = computed(() => this.bars().at(-1)?.close ?? null);

  protected readonly totalVolume = computed(() =>
    this.bars().reduce((sum, b) => sum + (b.volume ?? 0), 0),
  );

  protected readonly tone = computed(() => {
    const change = this.rangeChange();
    return change == null ? 'neutral' : change >= 0 ? 'up' : 'down';
  });
}
