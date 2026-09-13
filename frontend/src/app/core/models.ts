/** Types mirroring the FastAPI response models (see `src/market_data/domain/models.py`). */

export type Frequency = 'minute' | 'daily';
export type Severity = 'info' | 'warning' | 'error';

export interface ContractSummary {
  contract: string;
  root: string;
  exchange: string;
  frequency: Frequency;
  first_date: string;
  last_date: string;
  bars: number;
}

export interface DailyBar {
  contract: string;
  trading_date: string;
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  volume: number | null;
  open_interest: number | null;
  bar_count: number;
}

export interface VwapPoint {
  ts: string;
  typical_price: number | null;
  vwap: number | null;
}

export interface VwapSeries {
  contract: string;
  window_minutes: number;
  total_points: number;
  truncated: boolean;
  points: VwapPoint[];
}

/**
 * One concrete occurrence behind a `QualityIssue`.
 *
 * Deliberately category-agnostic so every check reports evidence the same way.
 * `values` is free-form: the detail table renders one column per key it finds,
 * so a rule that starts emitting new figures needs no change here.
 */
export interface IssueDetail {
  ts: string | null;
  /** Set only when the occurrence spans a range, e.g. a run of missing bars. */
  end_ts: string | null;
  /** Preferred over `ts` when present: a daily session is a date, not an instant. */
  label: string | null;
  values: Record<string, number | string | null>;
}

export interface QualityIssue {
  category: string;
  code: string;
  severity: Severity;
  contract: string;
  frequency: Frequency;
  message: string;
  count: number;
  start_ts: string | null;
  end_ts: string | null;
  /** Capped sample of the occurrences. Empty for rules not yet enumerating them. */
  details: IssueDetail[];
  detail_total: number;
  detail_truncated: boolean;
}

/** One page of a single finding's evidence, fetched when the user pages through it. */
export interface IssueDetailPage {
  contract: string | null;
  frequency: Frequency;
  code: string;
  total: number;
  offset: number;
  details: IssueDetail[];
}

/** One instant with no bar. Expected breaks (weekends, the daily halt) never appear. */
export interface MissingTimestamp {
  contract: string;
  ts: string;
  /** Preferred over `ts` for display when the unit is a date, not an instant. */
  label: string | null;
  /** `intra_session`, `extended` or `missing_session`. */
  classification: string;
}

/** One page of the flat missing-timestamp listing. Paged, because it can run to six figures. */
export interface MissingTimestampPage {
  contract: string | null;
  frequency: Frequency;
  /** The inferred bar interval the absences were measured against. Minute only. */
  expected_interval_s: number | null;
  total: number;
  offset: number;
  timestamps: MissingTimestamp[];
}

export interface QualityReport {
  contract: string | null;
  frequency: Frequency;
  start: string | null;
  end: string | null;
  bars_checked: number;
  issues: QualityIssue[];
}
