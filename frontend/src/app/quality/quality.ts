import { Component, computed, inject } from '@angular/core';

import { DETAIL_PAGE_SIZE, DashboardStore, MISSING_PAGE_SIZE } from '../core/store';
import { DASH, count } from '../core/format';
import { exchangeClock, exchangeDate, exchangeDateTime } from '../core/time';
import { IssueDetail, MissingTimestamp, QualityIssue, Severity } from '../core/models';
import { Stat } from '../shared/stat';

/** A day's worth of absences, which is how the minute listing is read. */
interface MissingDay {
  date: string;
  entries: MissingTimestamp[];
}

@Component({
  selector: 'app-quality',
  imports: [Stat],
  templateUrl: './quality.html',
  styleUrl: './quality.css',
})
export class Quality {
  protected readonly store = inject(DashboardStore);
  protected readonly format = { count };

  protected readonly report = computed(() => this.store.quality.value());
  protected readonly issues = computed(() => this.report()?.issues ?? []);

  /** Findings are counted by occurrence, not by row: one issue can cover many bars. */
  private readonly totals = computed(() => {
    const totals: Record<Severity, number> = { error: 0, warning: 0, info: 0 };
    for (const issue of this.issues()) totals[issue.severity] += issue.count;
    return totals;
  });

  protected readonly errors = computed(() => this.totals().error);
  protected readonly warnings = computed(() => this.totals().warning);
  protected readonly infos = computed(() => this.totals().info);

  // -- evidence drill-down -------------------------------------------- //
  //
  // A finding summarises ("9,594 intra-session gaps"); expanding it lists the
  // occurrences behind it, paged. Everything below is driven by the shape of
  // `IssueDetail` rather than by category, so all four kinds of check render
  // through the same table.

  protected readonly detailPageSize = DETAIL_PAGE_SIZE;

  protected hasDetails(issue: QualityIssue): boolean {
    return issue.detail_total > 0;
  }

  protected isExpanded(issue: QualityIssue): boolean {
    return this.store.expandedIssue() === issue.code;
  }

  protected toggle(issue: QualityIssue): void {
    if (this.hasDetails(issue)) this.store.toggleIssue(issue.code);
  }

  /** The fetched page, but only once it belongs to the finding that is open. */
  private readonly page = computed(() => {
    const page = this.store.issueDetails.value();
    return page && page.code === this.store.expandedIssue() ? page : undefined;
  });

  private readonly openIssue = computed(
    () => this.issues().find((i) => i.code === this.store.expandedIssue()) ?? null,
  );

  /**
   * Rows for the detail table.
   *
   * Falls back to the sample the report already carries, so opening a finding
   * paints immediately instead of waiting on a round trip. The fetched page
   * takes over as soon as it lands, and is the only way past that sample.
   */
  protected readonly detailRows = computed<IssueDetail[]>(() => {
    const page = this.page();
    if (page) return page.details;
    return this.openIssue()?.details.slice(0, DETAIL_PAGE_SIZE) ?? [];
  });

  protected readonly detailTotal = computed(
    () => this.page()?.total ?? this.openIssue()?.detail_total ?? 0,
  );

  private readonly detailOffset = computed(() => this.page()?.offset ?? 0);

  protected readonly detailFrom = computed(() =>
    this.detailRows().length ? this.detailOffset() + 1 : 0,
  );
  protected readonly detailTo = computed(() => this.detailOffset() + this.detailRows().length);
  protected readonly canPageDetailsBack = computed(() => this.detailOffset() > 0);
  protected readonly canPageDetailsForward = computed(() => this.detailTo() < this.detailTotal());

  /**
   * Column headers for the detail table.
   *
   * Taken from the union of `values` keys across the rows, in first-seen order,
   * so each rule decides its own columns and the UI needs no per-rule case.
   */
  protected readonly detailColumns = computed<string[]>(() => {
    const seen = new Set<string>();
    for (const detail of this.detailRows()) {
      for (const key of Object.keys(detail.values)) seen.add(key);
    }
    return [...seen];
  });

  /** True when any row spans a range, so the "Through" column earns its place. */
  protected readonly hasRanges = computed(() =>
    this.detailRows().some((d) => d.end_ts && d.end_ts !== d.ts),
  );

  /** Header text: `missing_bars` reads better as "missing bars". */
  protected columnLabel(key: string): string {
    return key.replaceAll('_', ' ');
  }

  protected cell(detail: IssueDetail, key: string): string {
    const value = detail.values[key];
    if (value == null) return '—';
    // Fractional figures (a log return, a MAD score, a multiple of the median)
    // must not go through the whole-number formatter, which would round a
    // return of 0.063 to 0.
    if (typeof value === 'number') return Number.isInteger(value) ? count(value) : String(value);
    return value;
  }

  /**
   * Right-align a column only when it holds numbers.
   *
   * Decided per column rather than per cell so a column does not jitter, and
   * from the data rather than a hard-coded list of keys, because the rules
   * choose their own column names.
   */
  protected isNumericColumn(key: string): boolean {
    return this.detailRows().some((d) => typeof d.values[key] === 'number');
  }

  /**
   * A finding's timestamp: the instant in Chicago time for minute data, the date
   * alone for daily data. A daily `ts` is a session date stamped at 00:00 UTC, so
   * it is read as a UTC date -- converting it to Chicago would land on 18:00 the
   * evening before, which is a different session. Printing the 00:00 would also
   * present a date as an instant.
   */
  protected stamp(iso: string | null): string {
    if (!iso) return DASH;
    return this.isDaily() ? iso.slice(0, 10) : exchangeDateTime(iso);
  }

  /** `label` wins when a rule set one, e.g. a daily session's plain date. */
  protected when(detail: IssueDetail): string {
    return detail.label ?? this.stamp(detail.ts);
  }

  /** Blank rather than a repeat when the occurrence is a single instant. */
  protected through(detail: IssueDetail): string {
    return detail.end_ts && detail.end_ts !== detail.ts ? this.stamp(detail.end_ts) : '';
  }

  // -- missing timestamps --------------------------------------------- //
  //
  // A separate section from the findings above, and a separate request. The
  // findings summarise; this enumerates, and one thin contract can be short six
  // figures of bars, so the endpoint is paged rather than capped.

  protected readonly pageSize = MISSING_PAGE_SIZE;
  protected readonly missing = computed(() => this.store.missing.value());
  protected readonly missingTotal = computed(() => this.missing()?.total ?? 0);

  /** 1-based index of the first row on screen, for the "showing X to Y" line. */
  protected readonly missingFrom = computed(() =>
    this.missingTotal() ? (this.missing()?.offset ?? 0) + 1 : 0,
  );

  protected readonly missingTo = computed(() => {
    const page = this.missing();
    return page ? page.offset + page.timestamps.length : 0;
  });

  protected readonly canPageBack = computed(() => (this.missing()?.offset ?? 0) > 0);
  protected readonly canPageForward = computed(() => this.missingTo() < this.missingTotal());

  /**
   * Absences grouped by Chicago date, the calendar the date filter and trading dates use.
   *
   * A flat column of 500 identical-looking timestamps is unreadable; under a
   * date heading the times alone carry the information. Daily data needs no
   * grouping, so it renders as one row of dates instead (see the template).
   */
  protected readonly missingDays = computed<MissingDay[]>(() => {
    const days = new Map<string, MissingTimestamp[]>();
    for (const entry of this.missing()?.timestamps ?? []) {
      const date = entry.label ?? exchangeDate(entry.ts);
      const bucket = days.get(date);
      bucket ? bucket.push(entry) : days.set(date, [entry]);
    }
    return [...days].map(([date, entries]) => ({ date, entries }));
  });

  protected readonly isDaily = computed(() => this.store.frequency() === 'daily');

  /** Just the clock time: the date is already the heading above it. */
  protected timeOf(entry: MissingTimestamp): string {
    return exchangeClock(entry.ts);
  }

  /** Intra-session absences are the suspicious ones; extended ones are often holidays. */
  protected toneOf(entry: MissingTimestamp): string {
    return entry.classification === 'intra_session' ? 'suspect' : 'expectedish';
  }
}
