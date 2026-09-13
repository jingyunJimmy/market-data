import { Injectable, computed, effect, inject, signal, untracked } from '@angular/core';

import { BarsQuery, IssueDetailQuery, MarketDataApi, MissingQuery, VwapQuery } from './api';
import { ContractSummary, Frequency } from './models';

/** How much history to open on, by frequency -- enough to be useful, small enough to stay fast. */
const DEFAULT_RANGE_DAYS: Record<Frequency, number> = { minute: 30, daily: 365 };

/** Missing timestamps per page. A thin contract has six figures of them, so this is paged. */
export const MISSING_PAGE_SIZE = 500;

/** Evidence rows per page. Small enough to read without scrolling the card away. */
export const DETAIL_PAGE_SIZE = 50;

function shiftDays(isoDate: string, days: number): string {
  const d = new Date(`${isoDate}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

/**
 * The dashboard's single source of truth: the filter selection, and the three
 * resources that follow from it.
 *
 * Components read these signals and write the filters; nothing else holds state.
 */
@Injectable({ providedIn: 'root' })
export class DashboardStore {
  private readonly api = inject(MarketDataApi);

  // -- filters --------------------------------------------------------- //

  readonly contract = signal<string | null>(null);
  readonly frequency = signal<Frequency>('minute');
  readonly start = signal('');
  readonly end = signal('');
  readonly vwapWindow = signal(15);

  /** Where the missing-timestamp listing is scrolled to. Reset whenever the filters move. */
  readonly missingOffset = signal(0);

  /**
   * The one finding whose evidence is open, by issue code, or null for none.
   *
   * Single expansion rather than a set: each open finding owns a pager, and two
   * pagers on screen at once is a worse experience than closing one first.
   */
  readonly expandedIssue = signal<string | null>(null);
  readonly detailOffset = signal(0);

  // -- catalogue ------------------------------------------------------- //

  readonly catalogue = this.api.contracts();

  readonly contractNames = computed(() =>
    [...new Set(this.catalogue.value().map((c) => c.contract))].sort(),
  );

  /** The catalogue entry for the current contract + frequency, if data exists for it. */
  readonly selection = computed(() =>
    this.catalogue
      .value()
      .find((c) => c.contract === this.contract() && c.frequency === this.frequency()),
  );

  readonly unreachable = computed(() => this.catalogue.error() !== undefined);

  // -- derived queries -------------------------------------------------- //

  private readonly barsQuery = computed<BarsQuery | undefined>(() => {
    const contract = this.contract();
    const [start, end] = [this.start(), this.end()];
    if (!contract || !start || !end) return undefined;
    return { contract, frequency: this.frequency(), start, end };
  });

  private readonly vwapQuery = computed<VwapQuery | undefined>(() => {
    const bars = this.barsQuery();
    // VWAP is defined over intraday bars; there is nothing to roll on daily data.
    if (!bars || bars.frequency !== 'minute') return undefined;
    return { ...bars, windowMinutes: this.vwapWindow() };
  });

  private readonly missingQuery = computed<MissingQuery | undefined>(() => {
    const bars = this.barsQuery();
    if (!bars) return undefined;
    return { ...bars, offset: this.missingOffset(), limit: MISSING_PAGE_SIZE };
  });

  readonly dailyBars = this.api.dailyBars(this.barsQuery);
  readonly vwap = this.api.vwap(this.vwapQuery);
  readonly quality = this.api.quality(this.barsQuery);
  private readonly detailQuery = computed<IssueDetailQuery | undefined>(() => {
    const bars = this.barsQuery();
    const code = this.expandedIssue();
    if (!bars || !code) return undefined;
    return { ...bars, code, offset: this.detailOffset(), limit: DETAIL_PAGE_SIZE };
  });

  readonly missing = this.api.missingTimestamps(this.missingQuery);
  readonly issueDetails = this.api.issueDetails(this.detailQuery);

  constructor() {
    // Land on a real contract as soon as the catalogue arrives.
    effect(() => {
      const names = this.contractNames();
      if (names.length && !names.includes(untracked(this.contract) ?? '')) {
        this.contract.set(names[0]);
      }
    });

    // Any change of contract or frequency re-opens the default window, so the
    // date range always points at data that exists.
    effect(() => {
      const selection = this.selection();
      if (selection) this.resetRange(selection);
    });

    // A new filter means different lists, so page one -- and a finding that was
    // open may not even exist in the new report. Untracked so that writing these
    // signals does not re-enter the effect.
    effect(() => {
      this.barsQuery();
      untracked(() => {
        this.missingOffset.set(0);
        this.detailOffset.set(0);
        this.expandedIssue.set(null);
      });
    });
  }

  /** Open a finding's evidence, or close it if it is the one already open. */
  toggleIssue(code: string): void {
    this.expandedIssue.update((open) => (open === code ? null : code));
    this.detailOffset.set(0);
  }

  nextDetailPage(): void {
    this.detailOffset.update((o) => o + DETAIL_PAGE_SIZE);
  }

  previousDetailPage(): void {
    this.detailOffset.update((o) => Math.max(0, o - DETAIL_PAGE_SIZE));
  }

  /** Step the missing-timestamp listing. Callers clamp against `total` themselves. */
  nextMissingPage(): void {
    this.missingOffset.update((o) => o + MISSING_PAGE_SIZE);
  }

  previousMissingPage(): void {
    this.missingOffset.update((o) => Math.max(0, o - MISSING_PAGE_SIZE));
  }

  private resetRange(selection: ContractSummary): void {
    const earliest = shiftDays(selection.last_date, -DEFAULT_RANGE_DAYS[selection.frequency]);
    this.start.set(earliest > selection.first_date ? earliest : selection.first_date);
    this.end.set(selection.last_date);
  }
}
