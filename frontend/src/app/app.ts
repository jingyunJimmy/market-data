import { Component, inject, signal } from '@angular/core';

import { Analytics } from './analytics/analytics';
import { DashboardStore } from './core/store';
import { Quality } from './quality/quality';

type Tab = 'analytics' | 'quality';

@Component({
  selector: 'app-root',
  imports: [Analytics, Quality],
  templateUrl: './app.html',
  styleUrl: './app.css',
})
export class App {
  protected readonly store = inject(DashboardStore);
  protected readonly tab = signal<Tab>('analytics');
  protected readonly windows = [5, 15, 30, 60];

  protected value(event: Event): string {
    return (event.target as HTMLInputElement | HTMLSelectElement).value;
  }
}
