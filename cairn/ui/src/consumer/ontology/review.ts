/** The review queue's state, without React: filtering, the cursor and selection, and its keyboard shortcuts. */

import type { Proposal } from './api';

export interface QueueFilter {
    kind: '' | 'term' | 'relationship' | 'metric';
    minConfidence: number;
}

export function filterQueue(items: Proposal[], f: QueueFilter): Proposal[] {
    return items.filter((p) => (!f.kind || p.kind === f.kind) && p.confidence >= f.minConfidence);
}

/** Proposals at or above `threshold`, for "accept all above confidence X" (shown before anything is decided). */
export function aboveThreshold(items: Proposal[], threshold: number): Proposal[] {
    return items.filter((p) => p.confidence >= threshold);
}

export interface QueueState {
    cursor: number;
    selected: string[];
}

export type QueueAction =
    | { type: 'move'; by: number; count: number }
    | { type: 'toggle'; id: string }
    | { type: 'selectAll'; ids: string[] }
    | { type: 'clear' }
    | { type: 'removed'; ids: string[]; count: number };

export function queueReducer(s: QueueState, a: QueueAction): QueueState {
    switch (a.type) {
        case 'move':
            return { ...s, cursor: a.count ? Math.min(Math.max(s.cursor + a.by, 0), a.count - 1) : 0 };
        case 'toggle':
            return { ...s, selected: s.selected.includes(a.id) ? s.selected.filter((x) => x !== a.id) : [...s.selected, a.id] };
        case 'selectAll':
            return { ...s, selected: s.selected.length === a.ids.length ? [] : [...a.ids] };
        case 'clear':
            return { ...s, selected: [] };
        case 'removed':
            return { cursor: Math.min(s.cursor, Math.max(a.count - 1, 0)), selected: s.selected.filter((x) => !a.ids.includes(x)) };
        default:
            return s;
    }
}

/** What a key does in the queue: j/k (or arrows) move, x selects, a accepts, r rejects, o opens. */
export type KeyIntent = { move: number } | { toggle: true } | { decide: 'accept' | 'reject' } | { open: true } | null;

export function keyIntent(key: string, mods: { ctrl?: boolean; meta?: boolean; alt?: boolean } = {}): KeyIntent {
    if (mods.ctrl || mods.meta || mods.alt) return null;
    switch (key) {
        case 'j':
        case 'ArrowDown':
            return { move: 1 };
        case 'k':
        case 'ArrowUp':
            return { move: -1 };
        case 'x':
        case ' ':
            return { toggle: true };
        case 'a':
            return { decide: 'accept' };
        case 'r':
            return { decide: 'reject' };
        case 'o':
        case 'Enter':
            return { open: true };
        default:
            return null;
    }
}

/** The proposals a decision applies to: the selection, or the one under the cursor when nothing is selected. */
export function targets(items: Proposal[], s: QueueState): string[] {
    if (s.selected.length) return items.filter((p) => s.selected.includes(p.id)).map((p) => p.id);
    const p = items[s.cursor];
    return p ? [p.id] : [];
}
