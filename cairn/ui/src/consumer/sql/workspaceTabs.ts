/**
 * The SQL editor's open files (L163): each tab is a saved query or an untitled draft, with what the editor holds and
 * what was last saved, so the page knows what is unsaved, what to autosave and what to keep for next time. Pure
 * functions: the page keeps the list in its state and these say what changes.
 */
import type { ChartSpec, SavedQuery } from '../api/types';

export interface EditorTab {
    key: string;
    /** The saved query this tab edits; none for an untitled draft. */
    savedId?: string;
    name: string;
    sql: string;
    connection?: string;
    chart?: ChartSpec;
    description?: string;
    shared?: boolean;
    /** The SQL as last saved (or opened): the tab is unsaved when the editor holds something else. */
    savedSql?: string;
    /** The person's role on the saved query: only an owner or editor saves it. */
    role?: string | null;
    saving?: boolean;
    error?: string;
}

export function isDirty(t: EditorTab): boolean {
    return t.savedId ? t.sql !== t.savedSql : t.sql.trim().length > 0;
}

/** May this person save the tab's query as it is (autosave included)? */
export function maySave(t: EditorTab): boolean {
    return !t.savedId || t.role === 'OWNER' || t.role === 'EDITOR' || t.role == null;
}

/** "Untitled 1", "Untitled 2", …: the first number no open tab uses. */
export function untitledName(tabs: EditorTab[]): string {
    const used = new Set(tabs.map((t) => t.name));
    for (let n = 1; ; n += 1) {
        if (!used.has(`Untitled ${n}`)) return `Untitled ${n}`;
    }
}

let seq = 0;
export function newKey(): string {
    seq += 1;
    return `t${Date.now().toString(36)}${seq}`;
}

/** A new untitled tab (with ``sql``, e.g. a query from the history). */
export function untitled(tabs: EditorTab[], sql = '', connection?: string): EditorTab {
    return { key: newKey(), name: untitledName(tabs), sql, connection };
}

/** The tab for a saved query: the one already open, else a new one. Returns the list and the tab's key. */
export function openSaved(tabs: EditorTab[], q: SavedQuery): { tabs: EditorTab[]; key: string } {
    const open = tabs.find((t) => t.savedId === q.id);
    if (open) return { tabs, key: open.key };
    const tab: EditorTab = { key: newKey(), savedId: q.id, name: q.name, sql: q.sql, savedSql: q.sql,
        connection: q.connection, chart: q.chart, description: q.description, shared: q.shared, role: q.role ?? null };
    return { tabs: [...tabs, tab], key: tab.key };
}

/** The list after closing ``key``, and which tab is shown then (the one to its right, else to its left). */
export function closeTab(tabs: EditorTab[], key: string, active: string | undefined): { tabs: EditorTab[]; active?: string } {
    const i = tabs.findIndex((t) => t.key === key);
    if (i < 0) return { tabs, active };
    const rest = tabs.filter((t) => t.key !== key);
    if (active !== key) return { tabs: rest, active };
    return { tabs: rest, active: (rest[i] ?? rest[i - 1])?.key };
}

/** After a save: the tab now edits ``q`` as saved (an untitled tab becomes the saved query's). The editor may hold
 * newer text than what was saved (typing went on while it saved): that stays, and stays unsaved. */
export function saved(tabs: EditorTab[], key: string, q: SavedQuery, sentSql: string): EditorTab[] {
    return tabs.map((t) => (t.key === key ? { ...t, savedId: q.id, name: q.name, savedSql: sentSql, role: q.role ?? t.role ?? 'OWNER',
        connection: q.connection, description: q.description, shared: q.shared, saving: false, error: undefined } : t));
}

/**
 * The open tabs against the saved queries as Cairn has them now (after a rename, a move or a delete from the file tree,
 * or a change by someone else): a saved query's name, description, sharing and role follow Cairn, so a later save does
 * not put back an old name. A query that was in the ``before`` list and is gone now was deleted: its tab becomes a draft
 * holding its text (one not listed yet, just saved, is left as it is). The same list when nothing changed.
 */
export function refreshed(tabs: EditorTab[], savedQueries: SavedQuery[], before: Set<string> = new Set()): EditorTab[] {
    const byId = new Map(savedQueries.map((q) => [q.id, q]));
    let changed = false;
    const out = tabs.map((t) => {
        if (!t.savedId) return t;
        const q = byId.get(t.savedId);
        if (!q) {
            if (!before.has(t.savedId)) return t;
            changed = true;
            return { key: t.key, name: `${t.name} (deleted)`, sql: t.sql, connection: t.connection, chart: t.chart };
        }
        const role = q.role ?? t.role ?? null;
        if (q.name === t.name && q.description === t.description && !!q.shared === !!t.shared && role === t.role) return t;
        changed = true;
        return { ...t, name: q.name, description: q.description, shared: q.shared, role };
    });
    return changed ? out : tabs;
}

// ---- kept on this device ------------------------------------------------------------------------------------------
const KEY = 'cairn.sql.tabs';

interface Kept {
    tabs: { key: string; savedId?: string; name: string; sql?: string; connection?: string }[];
    active?: string;
}

/** What to keep of the open tabs for next time: which are open, and the text of drafts and unsaved changes (a saved
 * query's text is read again from Cairn when it is opened). */
export function toKeep(tabs: EditorTab[], active?: string): Kept {
    return {
        tabs: tabs.map((t) => ({ key: t.key, savedId: t.savedId, name: t.name, connection: t.connection,
            ...(isDirty(t) ? { sql: t.sql } : {}) })),
        active,
    };
}

export function remember(user: string | undefined, tabs: EditorTab[], active?: string): void {
    try {
        window.localStorage.setItem(`${KEY}:${user ?? ''}`, JSON.stringify(toKeep(tabs, active)));
    } catch {
        // storage blocked or full: the tabs last while the page is open
    }
}

/** The tabs kept on this device, put back against the saved queries the person may open now (a query deleted or no
 * longer shared since is dropped; its unsaved text, if any, comes back as an untitled draft). */
export function restore(user: string | undefined, savedQueries: SavedQuery[]): { tabs: EditorTab[]; active?: string } {
    let kept: Kept | null = null;
    try {
        kept = JSON.parse(window.localStorage.getItem(`${KEY}:${user ?? ''}`) || 'null');
    } catch {
        kept = null;
    }
    if (!kept || !Array.isArray(kept.tabs)) return { tabs: [] };
    const byId = new Map(savedQueries.map((q) => [q.id, q]));
    const tabs: EditorTab[] = [];
    for (const k of kept.tabs) {
        if (!k || typeof k.key !== 'string') continue;
        const q = k.savedId ? byId.get(k.savedId) : undefined;
        if (q) {
            tabs.push({ key: k.key, savedId: q.id, name: q.name, sql: typeof k.sql === 'string' ? k.sql : q.sql,
                savedSql: q.sql, connection: q.connection, chart: q.chart, description: q.description, shared: q.shared,
                role: q.role ?? null });
        } else if (typeof k.sql === 'string' && k.sql.trim()) {
            tabs.push({ key: k.key, name: k.savedId ? `${k.name} (unsaved)` : k.name, sql: k.sql, connection: k.connection });
        }
    }
    return { tabs, active: tabs.some((t) => t.key === kept!.active) ? kept.active : tabs[0]?.key };
}
