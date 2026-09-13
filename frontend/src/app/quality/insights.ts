import { Component, computed, inject } from '@angular/core';

import { DashboardStore } from '../core/store';
import { count } from '../core/format';
import { exchangeDateTime } from '../core/time';
import {
  Evidence,
  InsightsReport,
  Pattern,
  PatternClassification,
  Suggestion,
  SuggestionType,
} from '../core/models';

/** A pattern with the evidence rows it cites and the suggestions that hang off it. */
interface PatternView {
  pattern: Pattern;
  evidence: Evidence[];
  suggestions: Suggestion[];
}

const CLASSIFICATION_LABEL: Record<PatternClassification, string> = {
  expected_market_behavior: 'Expected market behaviour',
  data_source_defect: 'Data source defect',
  threshold_miscalibration: 'Threshold miscalibration',
  ingestion_artifact: 'Ingestion artefact',
  unknown: 'Undetermined',
};

const TYPE_LABEL: Record<SuggestionType, string> = {
  expected_window: 'Expected window',
  adjust_threshold: 'Adjust threshold',
  exclude_from_analytics: 'Exclude from analytics',
  reject_at_ingest: 'Reject at ingest',
  dedupe_policy: 'Dedupe policy',
  custom: 'Custom rule',
};

const pct = (share: number) => `${Math.round(share * 100)}%`;
const pad = (hour: number) => String(hour).padStart(2, '0');

/**
 * Recurring quality patterns and the rules they suggest, generated on request.
 *
 * Nothing is fetched until the button is pressed: generating reads every
 * occurrence behind every finding and asks an LLM, which can take a while and
 * cost money. The figures under each pattern are rendered from the evidence
 * the API returns, not quoted from the prose, so they are right even where the
 * wording is a model's.
 */
@Component({
  selector: 'app-insights',
  templateUrl: './insights.html',
  styleUrl: './insights.css',
})
export class Insights {
  protected readonly store = inject(DashboardStore);
  protected readonly count = count;

  protected readonly report = computed(() => this.store.insights.value());
  protected readonly loading = computed(() => this.store.insights.isLoading());
  protected readonly failed = computed(() => this.store.insights.error() !== undefined);

  /**
   * The API's reason for a failed run, typically that the model could not be
   * reached. "Could not generate" alone leaves the reader unable to tell a
   * retry from a configuration change.
   */
  protected readonly failureReason = computed(() => {
    const error = this.store.insights.error() as { error?: { detail?: unknown } } | undefined;
    const detail = error?.error?.detail;
    return typeof detail === 'string' ? detail : null;
  });

  protected readonly views = computed<PatternView[]>(() => {
    const report = this.report();
    if (!report) return [];
    const byId = new Map(report.evidence.map((e) => [e.id, e]));
    return report.patterns.map((pattern) => ({
      pattern,
      evidence: pattern.evidence_refs.flatMap((id) => byId.get(id) ?? []),
      suggestions: report.suggestions.filter((s) => s.pattern_id === pattern.id),
    }));
  });

  protected source(report: InsightsReport): string {
    return `AI-assisted · ${report.model ?? 'LLM'}`;
  }

  protected generatedAt(report: InsightsReport): string {
    return exchangeDateTime(report.generated_at);
  }

  protected classification(pattern: Pattern): string {
    return CLASSIFICATION_LABEL[pattern.classification] ?? pattern.classification;
  }

  protected typeLabel(suggestion: Suggestion): string {
    return TYPE_LABEL[suggestion.type] ?? suggestion.type;
  }

  /** A custom rule is prose, so it reads as a sentence rather than as a parameter table. */
  protected description(suggestion: Suggestion): string | null {
    return suggestion.type === 'custom' ? String(suggestion.params['description'] ?? '') : null;
  }

  protected params(suggestion: Suggestion): [string, string][] {
    if (suggestion.type === 'custom') return [];
    return Object.entries(suggestion.params).map(([key, value]) => [
      key.replaceAll('_', ' '),
      String(value),
    ]);
  }

  /** The headline figures of one evidence row, each only when the row has it. */
  protected facts(evidence: Evidence): string[] {
    const scope = this.report()?.scope;
    const noun = evidence.occurrences === 1 ? 'occurrence' : 'occurrences';
    const facts = [`${count(evidence.occurrences)} ${noun}`];
    if (evidence.analysed < evidence.occurrences) {
      facts.push(`${count(evidence.analysed)} analysed`);
    }
    if (evidence.days_affected != null) {
      facts.push(
        evidence.days_affected_share != null && scope
          ? `${count(evidence.days_affected)} of ${count(scope.trading_days)} trading days`
          : `${count(evidence.days_affected)} days`,
      );
    }
    const top = evidence.top_hours[0];
    if (top) facts.push(`${pct(top.share)} in the ${pad(top.hour)}:00 CT hour`);
    if (evidence.median_duration_min != null) {
      facts.push(`median gap ${evidence.median_duration_min} min`);
    }
    if (evidence.share_after_gap && scope) {
      facts.push(
        `${pct(evidence.share_after_gap)} within ${scope.after_gap_window_min} min after a gap`,
      );
    }
    if (evidence.max_magnitude != null && evidence.magnitude_metric) {
      facts.push(`max ${evidence.magnitude_metric.replaceAll('_', ' ')} ${evidence.max_magnitude}`);
    }
    return facts;
  }
}
