import { Component, input } from '@angular/core';

/** A single headline figure. Callers pass pre-formatted text; this stays dumb. */
@Component({
  selector: 'app-stat',
  template: `
    <span class="label">{{ label() }}</span>
    <span class="value" [class]="'value--' + tone()">{{ value() }}</span>
  `,
  styles: `
    :host {
      display: flex;
      flex-direction: column;
      gap: 0.35rem;
      padding: 0.9rem 1.1rem;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
    }
    .label {
      font-size: 0.72rem;
      font-weight: 600;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: var(--muted);
    }
    .value {
      font-size: 1.35rem;
      font-variant-numeric: tabular-nums;
      color: var(--text);
    }
    .value--up {
      color: var(--up);
    }
    .value--down {
      color: var(--down);
    }
    .value--error {
      color: var(--down);
    }
    .value--warning {
      color: var(--warn);
    }
  `,
})
export class Stat {
  readonly label = input.required<string>();
  readonly value = input.required<string>();
  readonly tone = input<'neutral' | 'up' | 'down' | 'error' | 'warning'>('neutral');
}
