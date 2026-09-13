import { TestBed } from '@angular/core/testing';
import { Signal, signal } from '@angular/core';
import { beforeEach, describe, expect, it } from 'vitest';

import { BarsQuery, IssueDetailQuery, MarketDataApi, MissingQuery, VwapQuery } from './api';
import { ContractSummary } from './models';
import { DETAIL_PAGE_SIZE, DashboardStore, MISSING_PAGE_SIZE } from './store';

/**
 * The store's job is to turn a filter selection into the queries the API is
 * asked for. So the fake records every query function the store hands over and
 * evaluates it on demand, which is how these tests read the store's intent
 * without any HTTP in the picture.
 */
class FakeApi {
  readonly contractsValue = signal<ContractSummary[]>([]);
  readonly contractsError = signal<unknown>(undefined);

  barsQuery?: () => BarsQuery | undefined;
  vwapQuery?: () => VwapQuery | undefined;
  missingQuery?: () => MissingQuery | undefined;
  detailQuery?: () => IssueDetailQuery | undefined;

  contracts() {
    return { value: this.contractsValue, error: this.contractsError };
  }

  dailyBars(query: () => BarsQuery | undefined) {
    this.barsQuery = query;
    return this.resource();
  }

  quality(query: () => BarsQuery | undefined) {
    return this.resource();
  }

  vwap(query: () => VwapQuery | undefined) {
    this.vwapQuery = query;
    return this.resource();
  }

  missingTimestamps(query: () => MissingQuery | undefined) {
    this.missingQuery = query;
    return this.resource();
  }

  issueDetails(query: () => IssueDetailQuery | undefined) {
    this.detailQuery = query;
    return this.resource();
  }

  private resource() {
    return { value: signal(undefined), error: signal(undefined) };
  }
}

function summary(over: Partial<ContractSummary> = {}): ContractSummary {
  return {
    contract: 'CLZ24',
    root: 'CL',
    exchange: 'NYMEX',
    frequency: 'minute',
    first_date: '2024-01-01',
    last_date: '2024-06-30',
    bars: 1000,
    ...over,
  };
}

describe('DashboardStore', () => {
  let api: FakeApi;
  let store: DashboardStore;

  beforeEach(() => {
    api = new FakeApi();
    TestBed.configureTestingModule({
      providers: [{ provide: MarketDataApi, useValue: api }],
    });
    store = TestBed.inject(DashboardStore);
  });

  /** Run the store's effects, which is what a change detection pass would do. */
  function settle() {
    TestBed.tick();
  }

  describe('opening state', () => {
    it('asks for nothing until a contract and a range exist', () => {
      // The store is constructed before the catalogue arrives, so its opening state has
      // to be "ask for nothing" rather than "ask with blanks".
      expect(store.contract()).toBeNull();
      expect(api.barsQuery!()).toBeUndefined();
    });

    it('lands on the first contract as soon as the catalogue arrives', () => {
      // Opening on an empty selector would make the dashboard look broken, so the store
      // chooses for the reader.
      api.contractsValue.set([summary({ contract: 'NGZ24' }), summary({ contract: 'CLZ24' })]);
      settle();

      expect(store.contract()).toBe('CLZ24'); // sorted, not arrival order
    });

    it('lists each contract once even though it appears per frequency', () => {
      // The catalogue carries one row per contract and frequency; the filter is a list
      // of contracts, so those rows collapse.
      api.contractsValue.set([
        summary({ contract: 'CLZ24', frequency: 'minute' }),
        summary({ contract: 'CLZ24', frequency: 'daily' }),
      ]);
      settle();

      expect(store.contractNames()).toEqual(['CLZ24']);
    });

    it('reports the API as unreachable when the catalogue request failed', () => {
      // The catalogue is the first request of the session, which makes its failure the
      // signal that the backend is not running at all.
      expect(store.unreachable()).toBe(false);
      api.contractsError.set(new Error('connection refused'));
      expect(store.unreachable()).toBe(true);
    });
  });

  describe('the default date range', () => {
    it('opens on the last 30 days of minute data, so the first view stays fast', () => {
      // Counted back from the contract's last date, not from today: the sample data is
      // historical, and today would land on an empty range.
      api.contractsValue.set([summary({ first_date: '2024-01-01', last_date: '2024-06-30' })]);
      settle();

      expect(store.end()).toBe('2024-06-30');
      expect(store.start()).toBe('2024-05-31');
    });

    it('opens on a year of daily data, where 30 days would be too few bars', () => {
      // 365 calendar days rather than twelve months, so the arithmetic does not depend
      // on which months the window happens to cross.
      api.contractsValue.set([
        summary({ frequency: 'daily', first_date: '2020-01-01', last_date: '2024-06-30' }),
      ]);
      store.frequency.set('daily');
      settle();

      expect(store.start()).toBe('2023-07-01'); // 365 days back across a leap year
    });

    it('never starts before the data does', () => {
      // Clamped to first_date, so a short contract opens on everything it has instead
      // of on a window that begins before it existed.
      api.contractsValue.set([summary({ first_date: '2024-06-20', last_date: '2024-06-30' })]);
      settle();

      expect(store.start()).toBe('2024-06-20');
    });

    it('re-opens the window when the contract changes, so the range points at real data', () => {
      // Keeping the old range would point at dates the new contract may not cover, and
      // the view would come back empty for no visible reason.
      api.contractsValue.set([
        summary({ contract: 'AAA', last_date: '2024-06-30' }),
        summary({ contract: 'BBB', first_date: '2020-01-01', last_date: '2021-03-31' }),
      ]);
      settle();
      expect(store.end()).toBe('2024-06-30');

      store.contract.set('BBB');
      settle();

      expect(store.end()).toBe('2021-03-31');
    });
  });

  describe('derived queries', () => {
    beforeEach(() => {
      api.contractsValue.set([summary()]);
      settle();
    });

    it('carries the whole selection into the bars query', () => {
      // Asserted as a whole object: a field dropped on the way through is how a query
      // silently stops matching what the filters say.
      expect(api.barsQuery!()).toEqual({
        contract: 'CLZ24',
        frequency: 'minute',
        start: '2024-05-31',
        end: '2024-06-30',
      });
    });

    it('asks for no VWAP on daily data, where a rolling window is undefined', () => {
      // Undefined rather than a request the backend would reject, because a trailing
      // intraday window over daily bars is not a meaningful question.
      expect(api.vwapQuery!()).toMatchObject({ windowMinutes: 15 });

      api.contractsValue.set([summary({ frequency: 'daily' })]);
      store.frequency.set('daily');
      settle();

      expect(api.vwapQuery!()).toBeUndefined();
    });

    it('passes the chosen VWAP window through', () => {
      // The window is the one filter only the VWAP query reads, so it has to reach that
      // query and leave the others alone.
      store.vwapWindow.set(60);
      expect(api.vwapQuery!()).toMatchObject({ windowMinutes: 60 });
    });

    it('asks for evidence only once a finding is open', () => {
      // Evidence is paged per finding, so fetching it before one is open would be a
      // request with no row to attach the answer to.
      expect(api.detailQuery!()).toBeUndefined();

      store.toggleIssue('intra_session_gap');

      expect(api.detailQuery!()).toMatchObject({ code: 'intra_session_gap', offset: 0 });
    });
  });

  describe('paging', () => {
    beforeEach(() => {
      api.contractsValue.set([summary()]);
      settle();
    });

    it('steps the missing listing a page at a time', () => {
      // The offset lives in the store rather than in the table, so the pager and the
      // request cannot disagree about which page is on screen.
      store.nextMissingPage();
      expect(api.missingQuery!()).toMatchObject({ offset: MISSING_PAGE_SIZE });

      store.previousMissingPage();
      expect(api.missingQuery!()).toMatchObject({ offset: 0 });
    });

    it('never pages before the start of the list', () => {
      // A negative offset would reach the backend as a validation error rather than as
      // a clamped first page.
      store.previousMissingPage();
      expect(store.missingOffset()).toBe(0);

      store.previousDetailPage();
      expect(store.detailOffset()).toBe(0);
    });

    it('closes an open finding when it is toggled again', () => {
      // One finding open at a time: each carries its own pager, and two pagers on
      // screen is worse than closing one first.
      store.toggleIssue('gap');
      expect(store.expandedIssue()).toBe('gap');

      store.toggleIssue('gap');
      expect(store.expandedIssue()).toBeNull();
    });

    it('switching findings starts the new one at its first page', () => {
      // Carrying the old offset over would open the new finding at page three of
      // evidence it may not have.
      store.toggleIssue('gap');
      store.nextDetailPage();
      expect(store.detailOffset()).toBe(DETAIL_PAGE_SIZE);

      store.toggleIssue('outlier');

      expect(store.expandedIssue()).toBe('outlier');
      expect(store.detailOffset()).toBe(0);
    });

    it('a new filter rewinds both pagers and closes the open finding', () => {
      // Otherwise page 40 of the previous contract's gaps would be requested
      // for a contract that may not have 40 pages, or that finding at all.
      store.toggleIssue('gap');
      store.nextDetailPage();
      store.nextMissingPage();

      store.end.set('2024-06-15');
      settle();

      expect(store.missingOffset()).toBe(0);
      expect(store.detailOffset()).toBe(0);
      expect(store.expandedIssue()).toBeNull();
    });
  });
});
