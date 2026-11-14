/** The command palette's model: what it can offer, and how what is typed ranks it (no dependency). */

export type PaletteKind = 'page' | 'tab' | 'action' | 'recent' | 'catalog' | 'search' | 'ask';

export interface PaletteItem {
    id: string;
    kind: PaletteKind;
    label: string;
    /** Shown after the label, dimmer (the hub of a tab, a dataset's platform). */
    detail?: string;
    /** Also matched, not shown (the area's description words, synonyms). */
    keywords?: string;
    /** A route to open. */
    to?: string;
    /** An action to run instead (opens the assistant, a form…). */
    run?: () => void;
    shortcut?: string;
    /** Ranked a little higher (the person's role uses it). */
    boost?: number;
}

const norm = (s: string) => s.toLowerCase().normalize('NFKD').replace(/[̀-ͯ]/g, '');
const isBoundary = (text: string, i: number) => i === 0 || /[\s/›>:_.\-(]/.test(text[i - 1]);

/**
 * How well `query` matches `text` (higher is better), or null. A whole substring scores most (at the start, or at
 * a word's start, more); otherwise the query's letters in order (`sqwf` → "SQL workflows"), with points for letters
 * at word starts and in runs.
 */
export function fuzzyScore(query: string, text: string): number | null {
    const q = norm(query.trim());
    const t = norm(text);
    if (!q) return 0;
    const at = t.indexOf(q);
    if (at >= 0) return 1000 - at * 2 + (at === 0 ? 300 : isBoundary(t, at) ? 150 : 0) + (q.length === t.length ? 200 : 0);
    const words = q.split(/\s+/).filter(Boolean);
    if (words.length > 1) {
        // every word somewhere: "secret scope" finds "Trust & security › Secret scopes"
        const each = words.map((w) => fuzzyScore(w, text));
        return each.every((x) => x !== null) ? Math.round((each as number[]).reduce((a, b) => a + b, 0) / words.length) - 100 : null;
    }
    let score = 0;
    let run = 0;
    let from = 0;
    for (const ch of q) {
        const i = t.indexOf(ch, from);
        if (i < 0) return null;
        run = i === from ? run + 1 : 1;
        score += 10 + (isBoundary(t, i) ? 25 : 0) + (run > 1 ? 8 * run : 0) - Math.min(i - from, 10);
        from = i + 1;
    }
    return score;
}

/** Every word of the query appears in the text (as a word's start). */
export function wordsIn(query: string, text: string): boolean {
    const words = norm(query).split(/\s+/).filter((w) => w.length > 1);
    const t = ` ${norm(text).replace(/[^\p{L}\p{N}]+/gu, ' ')}`;
    return words.length > 0 && words.every((w) => t.includes(` ${w}`));
}

const KIND_ORDER: Record<PaletteKind, number> = { page: 0, action: 1, tab: 2, recent: 3, catalog: 4, search: 8, ask: 9 };

/** The items `query` matches, best first (label first, then detail and keywords, weighed less). */
export function rank(items: PaletteItem[], query: string, limit = 50): PaletteItem[] {
    const scored: { item: PaletteItem; score: number }[] = [];
    for (const item of items) {
        if (item.kind === 'ask' || item.kind === 'search') continue;
        const label = fuzzyScore(query, item.label);
        // The detail and keywords count only as whole words typed (letters in order would match almost anything there).
        const rest = wordsIn(query, `${item.label} ${item.detail ?? ''} ${item.keywords ?? ''}`);
        const best = Math.max(label ?? -Infinity, rest ? 400 : -Infinity);
        if (best === -Infinity) continue;
        scored.push({ item, score: best + (item.boost ?? 0) });
    }
    scored.sort((a, b) => b.score - a.score || KIND_ORDER[a.item.kind] - KIND_ORDER[b.item.kind]
        || a.item.label.length - b.item.label.length || a.item.label.localeCompare(b.item.label));
    const seen = new Set<string>();
    const out: PaletteItem[] = [];
    for (const { item } of scored) {
        const key = item.to ? `to:${item.to}` : item.id;
        if (seen.has(key)) continue;
        seen.add(key);
        out.push(item);
        if (out.length >= limit) break;
    }
    return out;
}

/** The index the arrow keys move to (wrapping), Home and End included; null for other keys. */
export function moveActive(key: string, active: number, count: number): number | null {
    if (count === 0) return null;
    switch (key) {
        case 'ArrowDown':
            return (active + 1) % count;
        case 'ArrowUp':
            return (active - 1 + count) % count;
        case 'Home':
            return 0;
        case 'End':
            return count - 1;
        case 'PageDown':
            return Math.min(active + 8, count - 1);
        case 'PageUp':
            return Math.max(active - 8, 0);
        default:
            return null;
    }
}

const RECENT_KEY = 'cairn.palette.recent';
const RECENT_MAX = 8;

/** Pages opened from the palette or the sidebar, newest first (this device). */
export function readRecent(): { to: string; label: string }[] {
    try {
        const raw = JSON.parse(window.localStorage.getItem(RECENT_KEY) || '[]');
        return Array.isArray(raw) ? raw.filter((r) => r && typeof r.to === 'string' && typeof r.label === 'string').slice(0, RECENT_MAX) : [];
    } catch {
        return [];
    }
}

export function pushRecent(to: string, label: string): void {
    try {
        const next = [{ to, label }, ...readRecent().filter((r) => r.to !== to)].slice(0, RECENT_MAX);
        window.localStorage.setItem(RECENT_KEY, JSON.stringify(next));
    } catch {
        // storage blocked: no recent list
    }
}
