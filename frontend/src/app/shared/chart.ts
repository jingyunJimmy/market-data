import {
  Component,
  DestroyRef,
  ElementRef,
  afterNextRender,
  computed,
  effect,
  inject,
  input,
  signal,
} from '@angular/core';
import {
  CandlestickData,
  CandlestickSeries,
  HistogramData,
  HistogramSeries,
  IChartApi,
  ISeriesApi,
  LineData,
  LineSeries,
  LineWidth,
  SeriesType,
  Time,
  createChart,
} from 'lightweight-charts';

export type ChartSeries =
  | { kind: 'candlestick'; data: CandlestickData<Time>[] }
  | { kind: 'line'; color: string; data: LineData<Time>[] }
  /** Volume bars, drawn in a band along the bottom on a scale of their own. */
  | { kind: 'volume'; data: HistogramData<Time>[] };

const UP = '#12805c';
const DOWN = '#c0392b';
const GRID = '#eceef2';
const AXIS = '#8a93a3';
const TEXT = '#5a6373';
const VOLUME_SCALE = 'volume';
/** Share of the chart height the volume band takes, measured from the bottom. */
const VOLUME_BAND = 0.25;

/**
 * Renders one or more series onto a lightweight-charts canvas.
 *
 * Owning the chart lifecycle here -- creation, resize, teardown -- keeps every
 * panel free of imperative charting code.
 */
@Component({
  selector: 'app-chart',
  template: '',
  styles: ':host { display: block; width: 100%; }',
  host: { '[style.height.px]': 'height()' },
})
export class Chart {
  readonly series = input.required<readonly ChartSeries[]>();
  readonly height = input(340);
  /** Show clock time on the axis (intraday) rather than dates alone. */
  readonly intraday = input(false);

  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);
  private readonly chart = signal<IChartApi | undefined>(undefined);
  private handles: ISeriesApi<SeriesType>[] = [];

  constructor() {
    afterNextRender(() => this.chart.set(this.create()));

    effect(() => {
      const chart = this.chart();
      if (chart) this.draw(chart, this.series());
    });

    effect(() => {
      const chart = this.chart();
      chart?.applyOptions({ timeScale: { timeVisible: this.intraday() } });
    });

    inject(DestroyRef).onDestroy(() => this.chart()?.remove());
  }

  private create(): IChartApi {
    return createChart(this.host.nativeElement, {
      autoSize: true,
      // The in-chart logo is off; TradingView is credited in the README instead.
      layout: {
        background: { color: 'transparent' },
        textColor: TEXT,
        fontSize: 12,
        attributionLogo: false,
      },
      grid: { vertLines: { color: GRID }, horzLines: { color: GRID } },
      rightPriceScale: { borderColor: GRID },
      timeScale: { borderColor: GRID, secondsVisible: false },
      crosshair: { vertLine: { color: AXIS }, horzLine: { color: AXIS } },
    });
  }

  private draw(chart: IChartApi, specs: readonly ChartSeries[]): void {
    for (const handle of this.handles) chart.removeSeries(handle);
    const hasVolume = specs.some((s) => s.kind === 'volume');
    this.handles = specs.map((spec) => {
      if (spec.kind === 'volume') {
        const series = chart.addSeries(HistogramSeries, {
          priceFormat: { type: 'volume' },
          priceScaleId: VOLUME_SCALE,
          priceLineVisible: false,
          lastValueVisible: false,
        });
        series.priceScale().applyOptions({ scaleMargins: { top: 1 - VOLUME_BAND, bottom: 0 } });
        series.setData(spec.data);
        return series;
      }
      if (spec.kind === 'candlestick') {
        const series = chart.addSeries(CandlestickSeries, {
          upColor: UP,
          downColor: DOWN,
          borderUpColor: UP,
          borderDownColor: DOWN,
          wickUpColor: UP,
          wickDownColor: DOWN,
        });
        // Lift the candles clear of the volume band so the two never overlap.
        series.priceScale().applyOptions({
          scaleMargins: { top: 0.08, bottom: hasVolume ? VOLUME_BAND + 0.05 : 0.08 },
        });
        series.setData(spec.data);
        return series;
      }
      const series = chart.addSeries(LineSeries, {
        color: spec.color,
        lineWidth: 2 as LineWidth,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      series.setData(spec.data);
      return series;
    });
    if (specs.some((s) => s.data.length)) chart.timeScale().fitContent();
  }
}
