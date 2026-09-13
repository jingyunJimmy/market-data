import { TestBed } from '@angular/core/testing';
import { Component, signal } from '@angular/core';
import { describe, expect, it } from 'vitest';

import { Stat } from './stat';

@Component({
  imports: [Stat],
  template: '<app-stat [label]="label()" [value]="value()" [tone]="tone()" />',
})
class Host {
  readonly label = signal('Errors');
  readonly value = signal('12');
  readonly tone = signal<'neutral' | 'up' | 'down' | 'error' | 'warning'>('neutral');
}

describe('Stat', () => {
  function render() {
    const fixture = TestBed.createComponent(Host);
    fixture.detectChanges();
    return fixture;
  }

  it('shows the caller’s label and pre-formatted value verbatim', () => {
    // The component stays dumb: formatting decisions belong to the caller.
    const el = render().nativeElement;
    expect(el.querySelector('.label').textContent).toBe('Errors');
    expect(el.querySelector('.value').textContent).toBe('12');
  });

  it('carries the tone as a class, so colour is styling rather than logic', () => {
    // The tone is a vocabulary the stylesheet owns. Asserting the class rather than a
    // colour keeps the test from breaking every time the palette is retuned.
    const fixture = render();
    expect(fixture.nativeElement.querySelector('.value').className).toContain('value--neutral');

    fixture.componentInstance.tone.set('error');
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('.value').className).toContain('value--error');
  });
});
