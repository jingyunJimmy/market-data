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

// -- intelligent insights (see `domain/insights/models.py`) --------------- //

export type PatternClassification =
  | 'expected_market_behavior'
  | 'data_source_defect'
  | 'threshold_miscalibration'
  | 'ingestion_artifact'
  | 'unknown';

export type Confidence = 'low' | 'medium' | 'high';

export type SuggestionType =
  | 'expected_window'
  | 'adjust_threshold'
  | 'exclude_from_analytics'
  | 'reject_at_ingest'
  | 'dedupe_policy'
  | 'custom';

/** Cleansing changes what data is used; validation changes what is reported. */
export type SuggestionKind = 'cleansing' | 'validation';

export interface HourShare {
  /** 0-23, exchange time. */
  hour: number;
  count: number;
  /** Of `Evidence.analysed`, 0-1. */
  share: number;
}

export interface PeriodCount {
  start: string;
  count: number;
}

/** Distribution statistics for one finding, computed by the backend. Patterns cite these by id. */
export interface Evidence {
  id: string;
  contract: string;
  code: string;
  category: string;
  severity: Severity;
  occurrences: number;
  /** Occurrences the statistics were computed from; below `occurrences` when capped. */
  analysed: number;
  per_1k_bars: number;
  first_seen: string | null;
  last_seen: string | null;
  expected_interval_s: number | null;
  days_affected: number | null;
  days_affected_share: number | null;
  top_hours: HourShare[];
  weekday_counts: Record<string, number>;
  median_duration_min: number | null;
  p90_duration_min: number | null;
  magnitude_metric: string | null;
  max_magnitude: number | null;
  share_after_gap: number | null;
  period: string | null;
  period_counts: PeriodCount[];
}

export interface InsightScope {
  contract: string | null;
  frequency: Frequency;
  start: string | null;
  end: string | null;
  bars_checked: number;
  trading_days: number;
  timezone: string;
  after_gap_window_min: number;
}

export interface Pattern {
  id: string;
  title: string;
  classification: PatternClassification;
  contracts: string[];
  evidence_refs: string[];
  explanation: string;
  confidence: Confidence;
}

export interface Suggestion {
  pattern_id: string;
  type: SuggestionType;
  kind: SuggestionKind;
  params: Record<string, string | number | boolean | null>;
  rationale: string;
}

/** Something a provider proposed that verification refused. */
export interface Rejection {
  stage: string;
  item: string | null;
  reason: string;
}

export interface InsightsReport {
  scope: InsightScope;
  /** The model that read the evidence. */
  model: string | null;
  generated_at: string;
  evidence: Evidence[];
  patterns: Pattern[];
  suggestions: Suggestion[];
  rejected: Rejection[];
}
