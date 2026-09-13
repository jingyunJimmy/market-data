import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { Injector, runInInjectionContext, signal } from '@angular/core';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { BarsQuery, MarketDataApi } from './api';

/**
 * The wire contract with FastAPI.
 *
 * These assert the exact paths and query-parameter names the backend routes
 * declare, because a rename on either side is otherwise invisible until the
 * dashboard silently shows nothing.
 */
describe('MarketDataApi', () => {
  let api: MarketDataApi;
  let http: HttpTestingController;
  let injector: Injector;

  const query: BarsQuery = {
    contract: 'CLZ24',
    frequency: 'minute',
    start: '2024-06-01',
    end: '2024-06-30',
  };

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    api = TestBed.inject(MarketDataApi);
    injector = TestBed.inject(Injector);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  /** Create a resource and let it issue its request. */
  function issue(create: () => { value: unknown }) {
    runInInjectionContext(injector, create);
    TestBed.tick();
  }

  it('requests the catalogue from the contracts route', () => {
    // The only route with no parameters, so it is the one request that fires before
    // anything has been selected.
    issue(() => api.contracts());

    http.expectOne('/api/contracts').flush([]);
  });

  it('sends the selected frequency to daily-ohlcv as `source`', () => {
    // The route aggregates whichever stored frequency it is pointed at, so the
    // parameter is named for the *input* bars, not the output.
    issue(() => api.dailyBars(() => query));

    const req = http.expectOne((r) => r.url === '/api/analytics/daily-ohlcv');
    expect(req.request.params.get('contract')).toBe('CLZ24');
    expect(req.request.params.get('start')).toBe('2024-06-01');
    expect(req.request.params.get('end')).toBe('2024-06-30');
    expect(req.request.params.get('source')).toBe('minute');
    req.flush([]);
  });

  it('sends the VWAP window as `window_minutes`', () => {
    // The client names it windowMinutes and FastAPI names it window_minutes. This is
    // the line that proves the translation happens.
    issue(() => api.vwap(() => ({ ...query, windowMinutes: 60 })));

    const req = http.expectOne((r) => r.url === '/api/analytics/vwap');
    expect(req.request.params.get('window_minutes')).toBe('60');
    req.flush(null);
  });

  it('sends the paging window on the missing-timestamp listing', () => {
    // Paging belongs to the server here, not to the table: a thin contract is short
    // six figures of bars, far too many to hold in the browser.
    issue(() => api.missingTimestamps(() => ({ ...query, offset: 500, limit: 500 })));

    const req = http.expectOne((r) => r.url === '/api/quality/missing-timestamps');
    expect(req.request.params.get('frequency')).toBe('minute');
    expect(req.request.params.get('offset')).toBe('500');
    expect(req.request.params.get('limit')).toBe('500');
    req.flush(null);
  });

  it('identifies a finding by its code when paging its evidence', () => {
    // By code rather than by row index, because the report's ordering is not part of
    // its contract and page two has to be the same finding.
    issue(() =>
      api.issueDetails(() => ({ ...query, code: 'intra_session_gap', offset: 50, limit: 50 })),
    );

    const req = http.expectOne((r) => r.url === '/api/quality/issue-details');
    expect(req.request.params.get('code')).toBe('intra_session_gap');
    expect(req.request.params.get('offset')).toBe('50');
    req.flush(null);
  });

  it('requests the quality report for the selected frequency', () => {
    // Frequency is part of the question rather than a display preference: the minute
    // rules and the daily rules are different checks.
    issue(() => api.quality(() => query));

    const req = http.expectOne((r) => r.url === '/api/quality/report');
    expect(req.request.params.get('frequency')).toBe('minute');
    req.flush(null);
  });

  it('issues no request at all while nothing is selected', () => {
    // An undefined query must mean "do not ask", not "ask for everything".
    issue(() => api.dailyBars(() => undefined));
    issue(() => api.vwap(() => undefined));
    issue(() => api.quality(() => undefined));
    issue(() => api.missingTimestamps(() => undefined));
    issue(() => api.issueDetails(() => undefined));

    http.expectNone(() => true);
  });

  it('re-requests when the filters move', () => {
    // The resource tracks whichever signals the query function reads, so moving a
    // filter refetches on its own with no subscription written anywhere.
    const contract = signal('CLZ24');
    issue(() => api.dailyBars(() => ({ ...query, contract: contract() })));

    http.expectOne((r) => r.params.get('contract') === 'CLZ24').flush([]);

    contract.set('NGZ24');
    TestBed.tick();

    http.expectOne((r) => r.params.get('contract') === 'NGZ24').flush([]);
  });
});
