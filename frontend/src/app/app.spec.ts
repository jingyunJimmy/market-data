import { TestBed } from '@angular/core/testing';
import { Component, WritableSignal, signal } from '@angular/core';
import { beforeEach, describe, expect, it } from 'vitest';

import { Analytics } from './analytics/analytics';
import { App } from './app';
import { ContractSummary } from './core/models';
import { DashboardStore } from './core/store';
import { Quality } from './quality/quality';

@Component({ selector: 'app-analytics', template: 'ANALYTICS' })
class AnalyticsStub {}

@Component({ selector: 'app-quality', template: 'QUALITY' })
class QualityStub {}

function summary(over: Partial<ContractSummary> = {}): ContractSummary {
  return {
    contract: 'CLZ24',
    root: 'CL',
    exchange: 'NYMEX',
    frequency: 'minute',
    first_date: '2024-01-01',
    last_date: '2024-06-30',
    bars: 12345,
    ...over,
  };
}

/**
 * The shell: filters, tabs, and the three states the app can open in.
 *
 * The empty states matter more than they look -- they are what a reviewer sees
 * first if the API is down or nothing has been ingested, so each has to say
 * what to run rather than just that something is missing.
 */
describe('App', () => {
  let store: {
    contract: WritableSignal<string | null>;
    frequency: WritableSignal<'minute' | 'daily'>;
    start: WritableSignal<string>;
    end: WritableSignal<string>;
    vwapWindow: WritableSignal<number>;
    contractNames: WritableSignal<string[]>;
    selection: WritableSignal<ContractSummary | undefined>;
    unreachable: WritableSignal<boolean>;
    catalogue: { isLoading: WritableSignal<boolean> };
  };

  beforeEach(() => {
    store = {
      contract: signal<string | null>('CLZ24'),
      frequency: signal<'minute' | 'daily'>('minute'),
      start: signal('2024-06-01'),
      end: signal('2024-06-30'),
      vwapWindow: signal(15),
      contractNames: signal(['CLZ24', 'NGZ24']),
      selection: signal<ContractSummary | undefined>(summary()),
      unreachable: signal(false),
      catalogue: { isLoading: signal(false) },
    };

    TestBed.configureTestingModule({
      imports: [App],
      providers: [{ provide: DashboardStore, useValue: store }],
    });
    TestBed.overrideComponent(App, {
      remove: { imports: [Analytics, Quality] },
      add: { imports: [AnalyticsStub, QualityStub] },
    });
  });

  function render() {
    const fixture = TestBed.createComponent(App);
    fixture.detectChanges();
    return fixture;
  }

  function text(fixture: ReturnType<typeof render>): string {
    return fixture.nativeElement.textContent ?? '';
  }

  function query<T extends Element>(fixture: ReturnType<typeof render>, selector: string): T {
    return fixture.nativeElement.querySelector(selector) as T;
  }

  describe('empty states', () => {
    it('tells the reader how to start the API when it cannot be reached', () => {
      // The message carries the command to run, because "cannot reach the API" on its
      // own leaves the reader guessing what to start.
      store.unreachable.set(true);

      const rendered = text(render());
      expect(rendered).toContain('Cannot reach the API');
      expect(rendered).toContain('market-data-serve');
    });

    it('tells the reader how to ingest when the store is empty', () => {
      // Reachable but empty is a different failure from unreachable and has a different
      // fix, so it gets its own message rather than a shared one.
      store.contractNames.set([]);

      const rendered = text(render());
      expect(rendered).toContain('No data ingested yet');
      expect(rendered).toContain('scripts/ingest.py');
    });

    it('says it is still loading rather than claiming the store is empty', () => {
      // Both states have no contracts. Without the loading check, every session would
      // flash "nothing ingested" before the catalogue lands.
      store.contractNames.set([]);
      store.catalogue.isLoading.set(true);

      const rendered = text(render());
      expect(rendered).toContain('Loading the contract catalogue');
      expect(rendered).not.toContain('No data ingested yet');
    });

    it('hides the filters entirely while there is nothing to filter', () => {
      // A row of empty dropdowns invites the reader to fiddle with filters when the
      // actual problem is upstream of the dashboard.
      store.contractNames.set([]);

      expect(query(render(), '.filters')).toBeNull();
    });
  });

  describe('the context header', () => {
    it('summarises the selected contract', () => {
      // The header is what makes a screenshot self-describing: which exchange, how many
      // bars, and over what dates.
      const rendered = text(render());
      expect(rendered).toContain('NYMEX');
      expect(rendered).toContain('12,345');
      expect(rendered).toContain('2024-01-01');
    });

    it('renders an unknown exchange as a dash rather than a blank cell', () => {
      // The vendor leaves the exchange blank on some rows, and an empty cell reads as a
      // rendering bug rather than as missing metadata.
      store.selection.set(summary({ exchange: '' }));

      expect(query(render(), '.context dd').textContent).toBe('—');
    });

    it('states once, under the title, that every date and time is Chicago time', () => {
      // One note for the whole page rather than a tag on every card. It has to hold in
      // every state, so it sits outside the empty-state branches.
      store.contractNames.set([]);

      expect(query(render(), '.topbar__note').textContent).toContain('Chicago time');
    });
  });

  describe('the filters', () => {
    it('writes a contract choice straight back to the store', () => {
      // No local copy of the selection: the template writes the store signal directly,
      // so there is nothing that can drift out of sync with it.
      const fixture = render();
      const select = query<HTMLSelectElement>(fixture, '.filters select');

      select.value = 'NGZ24';
      select.dispatchEvent(new Event('change'));

      expect(store.contract()).toBe('NGZ24');
    });

    it('writes the VWAP window back as a number, not as text', () => {
      // `+value(...)`: a string here would be sent as `window_minutes=60` but
      // compared as a string everywhere else.
      const fixture = render();
      const select = Array.from(fixture.nativeElement.querySelectorAll('.filters select')).at(
        -1,
      ) as HTMLSelectElement;

      select.value = '60';
      select.dispatchEvent(new Event('change'));

      expect(store.vwapWindow()).toBe(60);
    });

    it('disables the VWAP window on daily data, where it does not apply', () => {
      // Disabled rather than hidden, so the control does not shift the rest of the
      // filter row sideways as the frequency changes.
      store.frequency.set('daily');

      const select = Array.from(render().nativeElement.querySelectorAll('.filters select')).at(
        -1,
      ) as HTMLSelectElement;

      expect(select.disabled).toBe(true);
    });

    it('stops the date inputs from crossing each other', () => {
      // Native min and max bounds rather than validation after the fact, so an
      // impossible range cannot be picked in the first place.
      const fixture = render();
      const [start, end] = Array.from(
        fixture.nativeElement.querySelectorAll('input[type=date]'),
      ) as HTMLInputElement[];

      expect(start.max).toBe('2024-06-30'); // no later than the end
      expect(end.min).toBe('2024-06-01'); // no earlier than the start
      expect(start.min).toBe('2024-01-01'); // nor outside the data
      expect(end.max).toBe('2024-06-30');
    });
  });

  describe('tabs', () => {
    it('opens on the analytics panel', () => {
      // Analytics first: the quality report is the follow-up to what the prices did,
      // not the opening question.
      expect(text(render())).toContain('ANALYTICS');
    });

    it('switches to the quality panel and swaps the panels over', () => {
      // Only one panel is in the DOM at a time, so the hidden one is not holding a
      // chart open or issuing requests behind the other.
      const fixture = render();
      const quality = Array.from(
        fixture.nativeElement.querySelectorAll('.tabs button'),
      )[1] as HTMLButtonElement;

      quality.click();
      fixture.detectChanges();

      expect(text(fixture)).toContain('QUALITY');
      expect(text(fixture)).not.toContain('ANALYTICS');
    });
  });
});
