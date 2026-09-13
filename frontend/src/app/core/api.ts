import { httpResource } from '@angular/common/http';
import { Injectable } from '@angular/core';

import {
  ContractSummary,
  DailyBar,
  Frequency,
  InsightsReport,
  IssueDetailPage,
  MissingTimestampPage,
  QualityReport,
  VwapSeries,
} from './models';

/** Proxied to the FastAPI service by `proxy.conf.json` during development. */
const BASE = '/api';

export interface BarsQuery {
  contract: string;
  frequency: Frequency;
  start: string;
  end: string;
}

export interface VwapQuery extends BarsQuery {
  windowMinutes: number;
}

/** The missing-timestamp listing is paged rather than capped, so it carries a window. */
export interface MissingQuery extends BarsQuery {
  offset: number;
  limit: number;
}

/** One finding's evidence, paged past the sample the report itself carries. */
export interface IssueDetailQuery extends MissingQuery {
  code: string;
}

/** A request to generate insights for a selection. */
export interface InsightsQuery extends BarsQuery {
  /**
   * Which press of the button this is. Never sent: a new number makes a new
   * request object, which is what makes the resource POST again for unchanged
   * filters when the user regenerates.
   */
  run: number;
}

/**
 * The API boundary: every endpoint the dashboard talks to, in one place.
 *
 * Each method takes a reactive query function and returns an `httpResource`,
 * so a request is issued whenever the filters change and skipped entirely
 * while the query is undefined (nothing selected yet).
 */
@Injectable({ providedIn: 'root' })
export class MarketDataApi {
  contracts() {
    return httpResource<ContractSummary[]>(() => `${BASE}/contracts`, { defaultValue: [] });
  }

  dailyBars(query: () => BarsQuery | undefined) {
    return httpResource<DailyBar[]>(
      () => {
        const q = query();
        return (
          q && {
            url: `${BASE}/analytics/daily-ohlcv`,
            params: { contract: q.contract, start: q.start, end: q.end, source: q.frequency },
          }
        );
      },
      { defaultValue: [] },
    );
  }

  vwap(query: () => VwapQuery | undefined) {
    return httpResource<VwapSeries | undefined>(() => {
      const q = query();
      return (
        q && {
          url: `${BASE}/analytics/vwap`,
          params: {
            contract: q.contract,
            start: q.start,
            end: q.end,
            window_minutes: q.windowMinutes,
          },
        }
      );
    });
  }

  /**
   * Evidence for one finding.
   *
   * The report already carries a capped sample of every issue's details, so
   * this is only needed to page past it -- which is exactly what the detail
   * table's pager does.
   */
  issueDetails(query: () => IssueDetailQuery | undefined) {
    return httpResource<IssueDetailPage | undefined>(() => {
      const q = query();
      return (
        q && {
          url: `${BASE}/quality/issue-details`,
          params: {
            contract: q.contract,
            code: q.code,
            frequency: q.frequency,
            start: q.start,
            end: q.end,
            limit: q.limit,
            offset: q.offset,
          },
        }
      );
    });
  }

  /** Separate from `quality()` because of volume: the report summarises, this enumerates. */
  missingTimestamps(query: () => MissingQuery | undefined) {
    return httpResource<MissingTimestampPage | undefined>(() => {
      const q = query();
      return (
        q && {
          url: `${BASE}/quality/missing-timestamps`,
          params: {
            contract: q.contract,
            frequency: q.frequency,
            start: q.start,
            end: q.end,
            limit: q.limit,
            offset: q.offset,
          },
        }
      );
    });
  }

  quality(query: () => BarsQuery | undefined) {
    return httpResource<QualityReport | undefined>(() => {
      const q = query();
      return (
        q && {
          url: `${BASE}/quality/report`,
          params: {
            contract: q.contract,
            frequency: q.frequency,
            start: q.start,
            end: q.end,
          },
        }
      );
    });
  }

  /**
   * Generate recurring-pattern insights. A POST, because generating is work with
   * a cost; the store only supplies a query once the user has asked for it.
   */
  insights(query: () => InsightsQuery | undefined) {
    return httpResource<InsightsReport | undefined>(() => {
      const q = query();
      return (
        q && {
          url: `${BASE}/insights`,
          method: 'POST',
          params: {
            contract: q.contract,
            frequency: q.frequency,
            start: q.start,
            end: q.end,
          },
        }
      );
    });
  }
}
