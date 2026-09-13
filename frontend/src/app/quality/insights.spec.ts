import { HttpErrorResponse } from '@angular/common/http';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { DashboardStore } from '../core/store';
import { Evidence, InsightsReport } from '../core/models';
import { Insights } from './insights';

function evidence(over: Partial<Evidence> = {}): Evidence {
  return {
    id: 'E1',
    contract: 'ESZ25',
    code: 'intra_session_gap',
    category: 'gap',
    severity: 'warning',
    occurrences: 9594,
    analysed: 9594,
    per_1k_bars: 324.24,
    first_seen: '2025-11-20T21:00:00Z',
    last_seen: '2025-12-10T21:00:00Z',
    expected_interval_s: 60,
    days_affected: 20,
    days_affected_share: 0.741,
    top_hours: [{ hour: 16, count: 8702, share: 0.907 }],
    weekday_counts: {},
    median_duration_min: 61,
    p90_duration_min: null,
    magnitude_metric: null,
    max_magnitude: null,
    share_after_gap: null,
    period: null,
    period_counts: [],
    ...over,
  };
}

function report(over: Partial<InsightsReport> = {}): InsightsReport {
  return {
    scope: {
      contract: 'ESZ25',
      frequency: 'minute',
      start: '2025-11-19',
      end: '2025-12-19',
      bars_checked: 29589,
      trading_days: 27,
      timezone: 'America/Chicago',
      after_gap_window_min: 5,
    },
    model: 'qwen2.5:7b',
    generated_at: '2025-12-19T15:00:00Z',
    evidence: [evidence()],
    patterns: [
      {
        id: 'P1',
        title: 'Gaps recur at 16:00 CT',
        classification: 'expected_market_behavior',
        contracts: ['ESZ25'],
        evidence_refs: ['E1'],
        explanation: 'A scheduled pause, not lost data.',
        confidence: 'high',
      },
    ],
    suggestions: [
      {
        pattern_id: 'P1',
        type: 'expected_window',
        kind: 'validation',
        params: {
          contract: 'ESZ25',
          code: 'intra_session_gap',
          start_ct: '16:00',
          end_ct: '17:00',
        },
        rationale: 'Leaves only the gaps outside the halt.',
      },
    ],
    rejected: [],
    ...over,
  };
}

describe('Insights', () => {
  let store: {
    insights: {
      value: ReturnType<typeof signal<InsightsReport | undefined>>;
      error: ReturnType<typeof signal<unknown>>;
      isLoading: ReturnType<typeof signal<boolean>>;
    };
    insightsRequested: ReturnType<typeof signal<boolean>>;
    generateInsights: ReturnType<typeof vi.fn>;
  };

  beforeEach(() => {
    store = {
      insights: {
        value: signal<InsightsReport | undefined>(undefined),
        error: signal<unknown>(undefined),
        isLoading: signal(false),
      },
      insightsRequested: signal(false),
      generateInsights: vi.fn(() => store.insightsRequested.set(true)),
    };
    TestBed.configureTestingModule({
      imports: [Insights],
      providers: [{ provide: DashboardStore, useValue: store }],
    });
  });

  function render() {
    const fixture = TestBed.createComponent(Insights);
    fixture.detectChanges();
    return fixture;
  }

  function text(fixture: ReturnType<typeof render>): string {
    return (fixture.nativeElement.textContent ?? '').replace(/\s+/g, ' ');
  }

  function button(fixture: ReturnType<typeof render>): HTMLButtonElement {
    return fixture.nativeElement.querySelector('button.generate');
  }

  /** Generating costs a full read of the findings, and possibly a model call. */
  describe('on request only', () => {
    it('shows the button and nothing generated until it is pressed', () => {
      const fixture = render();

      expect(button(fixture).textContent).toContain('Generate insights');
      expect(text(fixture)).toContain('Generated on request');
      expect(fixture.nativeElement.querySelector('.pattern')).toBeNull();
    });

    it('asks the store to generate when the button is pressed', () => {
      const fixture = render();
      button(fixture).click();
      fixture.detectChanges();

      expect(store.generateInsights).toHaveBeenCalledOnce();
    });

    it('disables the button while a generation is running, so it cannot be stacked', () => {
      store.insightsRequested.set(true);
      store.insights.isLoading.set(true);
      const fixture = render();

      expect(button(fixture).disabled).toBe(true);
      expect(button(fixture).textContent).toContain('Generating');
    });

    it('offers to regenerate once a result is on screen', () => {
      store.insightsRequested.set(true);
      store.insights.value.set(report());

      expect(button(render()).textContent).toContain('Regenerate');
    });
  });

  describe('the result', () => {
    beforeEach(() => store.insightsRequested.set(true));

    it('renders each pattern with the figures from its evidence', () => {
      // The figures come from the evidence row, not from the explanation, which here
      // deliberately contains none.
      store.insights.value.set(report());
      const out = text(render());

      expect(out).toContain('Gaps recur at 16:00 CT');
      expect(out).toContain('Expected market behaviour');
      expect(out).toContain('9,594 occurrences');
      expect(out).toContain('20 of 27 trading days');
      expect(out).toContain('91% in the 16:00 CT hour');
    });

    it('lists a suggestion under its pattern with its kind and parameters', () => {
      store.insights.value.set(report());
      const fixture = render();
      const suggestion = fixture.nativeElement.querySelector('.pattern .suggestion');

      expect(suggestion.textContent).toContain('Expected window');
      expect(suggestion.querySelector('.kind').textContent).toContain('validation');
      const params = Array.from(suggestion.querySelectorAll('.params div')).map((d) => [
        (d as HTMLElement).querySelector('dt')?.textContent?.trim(),
        (d as HTMLElement).querySelector('dd')?.textContent?.trim(),
      ]);
      expect(params).toContainEqual(['start ct', '16:00']);
    });

    it('reads a custom rule as a sentence rather than a parameter table', () => {
      store.insights.value.set(
        report({
          suggestions: [
            {
              pattern_id: 'P1',
              type: 'custom',
              kind: 'validation',
              params: { description: 'Load the exchange holiday calendar.' },
              rationale: 'Separates holidays from outages.',
            },
          ],
        }),
      );
      const fixture = render();

      expect(text(fixture)).toContain('Load the exchange holiday calendar.');
      expect(fixture.nativeElement.querySelector('.params')).toBeNull();
    });

    it('labels model output as AI-assisted and names the model', () => {
      store.insights.value.set(report());

      expect(text(render())).toContain('AI-assisted · qwen2.5:7b');
    });

    it('passes on the reason a run failed, such as an unreachable model', () => {
      // "Could not generate" alone leaves the reader unable to tell a retry from a
      // configuration change.
      store.insights.error.set(
        new HttpErrorResponse({
          status: 503,
          error: { detail: 'LLM endpoint http://localhost:11434/v1 is unreachable' },
        }),
      );
      const out = text(render());

      expect(out).toContain('Could not generate insights');
      expect(out).toContain('is unreachable');
    });

    it('shows what verification removed, and why', () => {
      store.insights.value.set(
        report({
          rejected: [
            {
              stage: 'pattern',
              item: 'P2',
              reason: 'states 340, which its evidence does not contain',
            },
          ],
        }),
      );
      const out = text(render());

      expect(out).toContain('1 item removed by verification');
      expect(out).toContain('states 340');
    });

    it('states a clean result rather than showing an empty list', () => {
      store.insights.value.set(report({ patterns: [], suggestions: [] }));

      expect(text(render())).toContain('No recurring patterns in this range');
    });

    it('says so when generation failed', () => {
      store.insights.error.set(new Error('500'));

      expect(text(render())).toContain('Could not generate insights');
    });
  });
});
