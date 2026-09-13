import { TestBed } from '@angular/core/testing';
import { WritableSignal, signal } from '@angular/core';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { DashboardStore } from '../core/store';
import { IssueDetail, IssueDetailPage, MissingTimestampPage, QualityIssue } from '../core/models';
import { Quality } from './quality';

/** The subset of a resource the quality template reads. */
function resource<T>(value: T | undefined = undefined) {
  return {
    value: signal(value),
    error: signal<unknown>(undefined),
    isLoading: signal(false),
  };
}

function issue(over: Partial<QualityIssue> = {}): QualityIssue {
  return {
    category: 'gap',
    code: 'intra_session_gap',
    severity: 'warning',
    contract: 'CLZ24',
    frequency: 'minute',
    message: '9,594 intra-session gaps',
    count: 9594,
    start_ts: '2024-06-01T13:00:00Z',
    end_ts: '2024-06-30T20:00:00Z',
    details: [],
    detail_total: 0,
    detail_truncated: false,
    ...over,
  };
}

function detail(over: Partial<IssueDetail> = {}): IssueDetail {
  return { ts: '2024-06-03T14:00:00Z', end_ts: null, label: null, values: {}, ...over };
}

describe('Quality', () => {
  let store: {
    quality: ReturnType<typeof resource>;
    missing: ReturnType<typeof resource>;
    issueDetails: ReturnType<typeof resource>;
    frequency: WritableSignal<'minute' | 'daily'>;
    expandedIssue: WritableSignal<string | null>;
    toggleIssue: ReturnType<typeof vi.fn>;
    nextDetailPage: ReturnType<typeof vi.fn>;
    previousDetailPage: ReturnType<typeof vi.fn>;
    nextMissingPage: ReturnType<typeof vi.fn>;
    previousMissingPage: ReturnType<typeof vi.fn>;
  };

  beforeEach(() => {
    store = {
      quality: resource<unknown>(undefined),
      missing: resource<unknown>(undefined),
      issueDetails: resource<unknown>(undefined),
      frequency: signal<'minute' | 'daily'>('minute'),
      expandedIssue: signal<string | null>(null),
      toggleIssue: vi.fn((code: string) =>
        store.expandedIssue.update((open) => (open === code ? null : code)),
      ),
      nextDetailPage: vi.fn(),
      previousDetailPage: vi.fn(),
      nextMissingPage: vi.fn(),
      previousMissingPage: vi.fn(),
    };

    TestBed.configureTestingModule({
      imports: [Quality],
      providers: [{ provide: DashboardStore, useValue: store }],
    });
  });

  function render() {
    const fixture = TestBed.createComponent(Quality);
    fixture.detectChanges();
    return fixture;
  }

  function text(fixture: ReturnType<typeof render>): string {
    return fixture.nativeElement.textContent ?? '';
  }

  function rows(fixture: ReturnType<typeof render>, selector: string): HTMLElement[] {
    return Array.from(fixture.nativeElement.querySelectorAll(selector));
  }

  /**
   * Group 1 -- the three counters above the findings table.
   *
   * They are the only figures a reader takes in before scrolling, so the unit they
   * count matters, and no-issues must not look like could-not-load.
   */
  describe('the headline tallies', () => {
    it('counts occurrences rather than findings', () => {
      // One row standing for 9,594 gaps must not read as a single warning.
      store.quality.value.set({
        bars_checked: 100,
        issues: [
          issue({ code: 'a', severity: 'warning', count: 9594 }),
          issue({ code: 'b', severity: 'warning', count: 6 }),
          issue({ code: 'c', severity: 'error', count: 2 }),
        ],
      });

      const stats = rows(render(), 'app-stat').map((s) => s.textContent ?? '');

      expect(stats[1]).toContain('2'); // errors
      expect(stats[2]).toContain('9,600'); // warnings
    });

    it('shows a clean report as clean rather than as an empty table', () => {
      // A clean result has to be stated. An empty table looks like a check that never
      // ran.
      store.quality.value.set({ bars_checked: 100, issues: [] });

      expect(text(render())).toContain('No issues detected');
    });

    it('says so when the report could not be loaded', () => {
      // Otherwise a failed request renders identically to a clean report, which is the
      // most dangerous confusion this panel can cause.
      store.quality.error.set(new Error('boom'));

      expect(text(render())).toContain('Could not load the quality report');
    });
  });

  /**
   * Group 2 -- expanding one finding into the occurrences behind it.
   *
   * The table is driven by the shape of `IssueDetail` rather than by which rule
   * produced it, so these are mostly about which rows win and which columns appear.
   */
  describe('the evidence drill-down', () => {
    it('offers no drill-down for a finding that carries no evidence', () => {
      // No button at all, rather than a button that opens an empty card.
      store.quality.value.set({ bars_checked: 1, issues: [issue({ detail_total: 0 })] });

      expect(rows(render(), 'button.drill')).toHaveLength(0);
    });

    it('paints the report’s own sample before the fetched page lands', () => {
      // Otherwise expanding a finding shows an empty table for a round trip.
      store.quality.value.set({
        bars_checked: 1,
        issues: [issue({ detail_total: 900, details: [detail({ values: { missing_bars: 3 } })] })],
      });
      store.expandedIssue.set('intra_session_gap');

      expect(text(render())).toContain('missing bars');
    });

    it('takes the fetched page over the sample once it arrives', () => {
      // The "51-51 of 900" line is the proof: that range can only come from the fetched
      // page's own offset, never from the sample the report carries.
      store.quality.value.set({
        bars_checked: 1,
        issues: [issue({ detail_total: 900, details: [detail({ values: { missing_bars: 3 } })] })],
      });
      store.expandedIssue.set('intra_session_gap');
      store.issueDetails.value.set({
        contract: 'CLZ24',
        frequency: 'minute',
        code: 'intra_session_gap',
        total: 900,
        offset: 50,
        details: [detail({ values: { missing_bars: 7 } })],
      } satisfies IssueDetailPage);

      expect(text(render())).toContain('51–51 of 900');
    });

    it('ignores a page belonging to a finding that is no longer open', () => {
      // The open finding changes faster than the request returns.
      store.quality.value.set({ bars_checked: 1, issues: [issue({ detail_total: 900 })] });
      store.expandedIssue.set('intra_session_gap');
      store.issueDetails.value.set({
        contract: 'CLZ24',
        frequency: 'minute',
        code: 'price_return_outlier',
        total: 4,
        offset: 0,
        details: [detail({ values: { mad_score: 12.5 } })],
      } satisfies IssueDetailPage);

      expect(text(render())).not.toContain('mad score');
    });

    it('keeps a fractional figure fractional', () => {
      // A log return of 0.063 through the whole-number formatter reads as 0,
      // which turns the evidence into nonsense.
      store.quality.value.set({
        bars_checked: 1,
        issues: [issue({ detail_total: 1, details: [detail({ values: { log_return: 0.063 } })] })],
      });
      store.expandedIssue.set('intra_session_gap');

      expect(text(render())).toContain('0.063');
    });

    it('adds the Through column only when an occurrence actually spans a range', () => {
      // A column of blanks costs width on every other rule's evidence, so the table
      // asks its rows whether the column is needed.
      store.quality.value.set({
        bars_checked: 1,
        issues: [issue({ detail_total: 1, details: [detail({ values: { missing_bars: 1 } })] })],
      });
      store.expandedIssue.set('intra_session_gap');
      expect(rows(render(), '.evidence th').map((th) => th.textContent?.trim())).not.toContain(
        'Through',
      );

      store.quality.value.set({
        bars_checked: 1,
        issues: [
          issue({
            detail_total: 1,
            details: [detail({ end_ts: '2024-06-03T14:05:00Z', values: { missing_bars: 5 } })],
          }),
        ],
      });
      expect(rows(render(), '.evidence th').map((th) => th.textContent?.trim())).toContain(
        'Through',
      );
    });

    it('hides the evidence pager when everything fits on one page', () => {
      // Three rows under a page size of fifty have nowhere to page to.
      store.quality.value.set({
        bars_checked: 1,
        issues: [issue({ detail_total: 3, details: [detail({ values: { n: 1 } })] })],
      });
      store.expandedIssue.set('intra_session_gap');

      expect(rows(render(), '.evidence__pager')).toHaveLength(0);
    });
  });

  /**
   * Group 3 -- the absent-bar listing below the findings.
   *
   * A separate section and a separate request: the findings summarise, this
   * enumerates, which is why it is paged, grouped, and classified per row.
   */
  describe('the missing-timestamp listing', () => {
    function page(over: Partial<MissingTimestampPage> = {}): MissingTimestampPage {
      return {
        contract: 'CLZ24',
        frequency: 'minute',
        expected_interval_s: 60,
        total: 3,
        offset: 0,
        timestamps: [],
        ...over,
      };
    }

    it('groups minute absences under their date, so the times carry the meaning', () => {
      // Grouped by Chicago date and kept in first-seen order, so the headings follow the data
      // rather than being re-sorted into a different story.
      store.missing.value.set(
        page({
          total: 3,
          timestamps: [
            {
              contract: 'C',
              ts: '2024-06-03T14:00:00Z',
              label: null,
              classification: 'intra_session',
            },
            {
              contract: 'C',
              ts: '2024-06-03T14:01:00Z',
              label: null,
              classification: 'intra_session',
            },
            { contract: 'C', ts: '2024-06-04T14:00:00Z', label: null, classification: 'extended' },
          ],
        }),
      );

      const days = rows(render(), '.missing__day h3').map((h) => h.textContent?.trim());
      expect(days).toEqual(['2024-06-03', '2024-06-04']);
    });

    it('groups and times minute absences in Chicago, not UTC', () => {
      // 03:00Z on 4 June is 22:00 on 3 June in Chicago (CDT, UTC-5), so it belongs under
      // 3 June -- the date the filter and the daily candle put that bar on.
      store.missing.value.set(
        page({
          total: 1,
          timestamps: [
            {
              contract: 'C',
              ts: '2024-06-04T03:00:00Z',
              label: null,
              classification: 'intra_session',
            },
          ],
        }),
      );

      const fixture = render();
      expect(rows(fixture, '.missing__day h3').map((h) => h.textContent?.trim())).toEqual([
        '2024-06-03',
      ]);
      expect(rows(fixture, '.missing__times .chip').map((c) => c.textContent?.trim())).toEqual([
        '22:00',
      ]);
    });

    it('lists daily absences as plain dates with no grouping', () => {
      // Grouping dates by date would put exactly one row under every heading.
      store.frequency.set('daily');
      store.missing.value.set(
        page({
          frequency: 'daily',
          expected_interval_s: null,
          total: 1,
          timestamps: [
            {
              contract: 'C',
              ts: '2024-06-03T00:00:00Z',
              label: '2024-06-03',
              classification: 'missing_session',
            },
          ],
        }),
      );

      const fixture = render();
      expect(rows(fixture, '.missing__day')).toHaveLength(0);
      expect(text(fixture)).toContain('1 absent sessions');
    });

    it('reports a range with nothing missing as clean, not as empty', () => {
      // The same reasoning as the clean report above: silence is not an answer a
      // reviewer can act on.
      store.missing.value.set(page({ total: 0, timestamps: [] }));

      expect(text(render())).toContain('No unexplained absences');
    });

    it('disables the back button on the first page', () => {
      // Offset zero has nothing behind it, and forward stays live because 900 absences
      // do not fit in one page of 500.
      store.missing.value.set(
        page({
          total: 900,
          offset: 0,
          timestamps: [
            {
              contract: 'C',
              ts: '2024-06-03T14:00:00Z',
              label: null,
              classification: 'intra_session',
            },
          ],
        }),
      );

      const [back, next] = rows(render(), '.missing__pager button') as HTMLButtonElement[];
      expect(back.disabled).toBe(true);
      expect(next.disabled).toBe(false);
    });

    it('disables the forward button once the last row is on screen', () => {
      // Clamped against the total rather than against the page size, so a final short
      // page still ends the listing.
      store.missing.value.set(
        page({
          total: 1,
          offset: 0,
          timestamps: [
            {
              contract: 'C',
              ts: '2024-06-03T14:00:00Z',
              label: null,
              classification: 'intra_session',
            },
          ],
        }),
      );

      const [, next] = rows(render(), '.missing__pager button') as HTMLButtonElement[];
      expect(next.disabled).toBe(true);
    });
  });

  /**
   * Group 4 -- what the timestamps in each table mean.
   *
   * Minute findings are instants in Chicago time; daily findings are Chicago dates.
   */
  describe('the time basis', () => {
    function stamps(fixture: ReturnType<typeof render>): (string | undefined)[] {
      return rows(fixture, 'td.ts').map((td) => td.textContent?.trim());
    }

    it('prints minute findings in Chicago time', () => {
      store.quality.value.set({ bars_checked: 1, issues: [issue()] });

      const fixture = render();
      // 13:00Z and 20:00Z in June are 08:00 and 15:00 in Chicago (CDT, UTC-5).
      expect(stamps(fixture)).toEqual(['01/06/2024, 08:00', '30/06/2024, 15:00']);
    });

    it('prints daily findings as their date, without shifting it into Chicago', () => {
      // A daily ts is a session date stamped at 00:00 UTC. Converted to Chicago it would
      // read as 18:00 the evening before, which is a different session.
      store.frequency.set('daily');
      store.quality.value.set({
        bars_checked: 1,
        issues: [
          issue({
            frequency: 'daily',
            start_ts: '2024-06-03T00:00:00Z',
            end_ts: '2024-06-04T00:00:00Z',
          }),
        ],
      });

      const fixture = render();
      expect(stamps(fixture)).toEqual(['2024-06-03', '2024-06-04']);
    });
  });
});
