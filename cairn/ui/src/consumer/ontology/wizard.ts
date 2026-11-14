/** The compile wizard's steps, without React: scope, mode, cost, then the run as it progresses. */

import type { Estimate, OntologyRun, Scope } from './api';

export type ScopeKind = 'all' | 'schemas' | 'tables';

export interface WizardState {
    step: 0 | 1 | 2 | 3;
    scope: ScopeKind;
    schemas: string[];
    tables: string[];
    mode: 'incremental' | 'full';
}

export const STEPS = ['Scope', 'Mode', 'Cost', 'Run'] as const;

export const INITIAL: WizardState = { step: 0, scope: 'all', schemas: [], tables: [], mode: 'incremental' };

/** The scope as the API takes it. */
export function scopeBody(s: WizardState): Scope {
    if (s.scope === 'schemas') return { schemas: s.schemas };
    if (s.scope === 'tables') return { tables: s.tables };
    return {};
}

/** Whether the current step is complete enough to go on. */
export function canNext(s: WizardState, estimate: Estimate | null = null): boolean {
    if (s.step === 0) return s.scope === 'all' || (s.scope === 'schemas' ? s.schemas.length > 0 : s.tables.length > 0);
    if (s.step === 1) return s.mode === 'incremental' || s.mode === 'full';
    if (s.step === 2) return !!estimate;
    return false;
}

export function next(s: WizardState): WizardState {
    return { ...s, step: Math.min(3, s.step + 1) as WizardState['step'] };
}

export function back(s: WizardState): WizardState {
    return { ...s, step: Math.max(0, s.step - 1) as WizardState['step'] };
}

/** Percent done of a run: the tables, then the stages after them (relationships, metrics, bridges, publishing). */
export function percent(run: OntologyRun | null): number {
    if (!run) return 0;
    if (run.state === 'completed') return 100;
    const p = run.progress;
    if (!p || !p.tables_total) return run.state === 'running' ? 1 : 0;
    const tables = (p.tables_done / p.tables_total) * 80;
    const after = ({ tables: 0, relationships: 5, metrics: 10, bridges: 15, publishing: 19 } as Record<string, number>)[p.phase] ?? 0;
    return Math.min(99, Math.round(tables + after));
}

export function phaseLabel(run: OntologyRun | null): string {
    if (!run) return '';
    if (run.state !== 'running') return run.state;
    const p = run.progress;
    if (!p) return 'starting';
    if (p.phase === 'tables') return `compiling tables: ${p.tables_done} of ${p.tables_total}`;
    return ({ relationships: 'finding relationships', metrics: 'suggesting metrics', bridges: 'bridge tables', publishing: 'publishing' } as
        Record<string, string>)[p.phase] ?? p.phase;
}

export function isFinal(run: OntologyRun | null): boolean {
    return !!run && run.state !== 'running';
}

export function resumable(run: OntologyRun | null): boolean {
    return !!run && ['cancelled', 'interrupted', 'failed'].includes(run.state);
}
