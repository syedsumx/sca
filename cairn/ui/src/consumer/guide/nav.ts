import type { Role } from './onboarding';

/** The sidebar's entries, as the guide arranges them (a subset of Layout's NavItem). */
export interface NavEntry {
    to: string;
}
export interface NavGroupOf<T extends NavEntry> {
    label: string;
    items: T[];
}

/** The pages each role reaches for first: the "For you" group. */
export const ROLE_PINS: Record<Role, string[]> = {
    analyst: ['/catalog', '/sql', '/dashboards', '/semantics'],
    engineer: ['/ingestion', '/flows', '/runs', '/lakehouse'],
    steward: ['/glossary', '/quality', '/requests', '/semantics'],
    admin: ['/settings', '/security', '/governance', '/usage'],
    explorer: ['/discover', '/catalog', '/products'],
};

/**
 * The sidebar groups a role needs open (L150: the others are collapsed, one click away, until "Show everything").
 * Every role keeps the Catalog (Cairn Core, the centre of the workspace) open.
 */
export const ROLE_GROUPS: Record<Role, string[]> = {
    analyst: ['nav.group.catalog', 'nav.group.analyze', 'nav.group.ai'],
    engineer: ['nav.group.catalog', 'nav.group.quality', 'nav.group.build'],
    steward: ['nav.group.catalog', 'nav.group.govern', 'nav.group.quality'],
    admin: ['nav.group.catalog', 'nav.group.govern', 'nav.group.platform'],
    explorer: ['nav.group.catalog', 'nav.group.analyze'],
};
/** Open when the person has no role yet. */
export const DEFAULT_GROUPS = ['nav.group.catalog'];

export interface SidebarPrefs {
    pinned: string[];
    hidden: string[];
    show_all: boolean;
}

export type ArrangedGroup<T extends NavEntry> = NavGroupOf<T> & {
    /** Its entries are listed (a collapsed group shows only its heading). */
    open: boolean;
    /** It holds the page being shown. */
    current: boolean;
};

export interface Arranged<T extends NavEntry> {
    /** "For you": the role's pages and the person's pins, in that order, without repeats. */
    forYou: T[];
    groups: ArrangedGroup<T>[];
    /** How many entries "Show everything" would add (in collapsed groups, or hidden). */
    folded: number;
}

/** Does `path` (a location's pathname) belong to the entry `to`? */
export function routeMatches(to: string, path: string): boolean {
    const base = to.split('?')[0];
    return path === base || path.startsWith(`${base}/`);
}

/**
 * The sidebar for a person: `allowed` (what their capabilities let them use; never overridden), then their role and
 * their own choices. Every group the person may use is listed; the role's groups and the group of the current page
 * are open, the rest collapsed, and the person's own open/closed choice (`opened`) wins. Hidden entries are left out
 * and pins added to "For you". "Show everything" (`show_all`) opens every group and shows the hidden entries, still
 * only those allowed.
 */
export function arrangeNav<T extends NavEntry>(groups: NavGroupOf<T>[], allowed: (item: T) => boolean, role: Role | null,
    prefs: SidebarPrefs, where: { path?: string; opened?: Record<string, boolean>; suggested?: Role | null } = {}): Arranged<T> {
    const usable = groups.map((g) => ({ ...g, items: g.items.filter(allowed) }));
    const byPath = new Map(usable.flatMap((g) => g.items).map((i) => [i.to, i]));
    // The groups open by default: the chosen role's, else the one the profile suggests (no "For you" for a guess).
    const groupsRole = role ?? where.suggested ?? null;
    const keep = groupsRole ? ROLE_GROUPS[groupsRole] : DEFAULT_GROUPS;
    const hidden = new Set(prefs.show_all ? [] : prefs.hidden);
    const path = where.path ?? '';
    // The most specific entry the page belongs to (/sql/workbooks is Workbooks, not SQL).
    const best = [...byPath.keys()].filter((to) => path && routeMatches(to, path)).sort((a, b) => b.length - a.length)[0];
    let folded = 0;
    const shown = usable.map((g) => {
        const items = g.items.filter((i) => !hidden.has(i.to));
        const current = !!best && g.items.some((i) => i.to === best);
        const chosen = where.opened?.[g.label];
        const open = prefs.show_all || (chosen ?? (keep.includes(g.label) || current));
        folded += g.items.length - items.length + (open ? 0 : items.length);
        return { ...g, items, open, current };
    }).filter((g) => g.items.length > 0);
    const forYouPaths = [...(role ? ROLE_PINS[role] : []), ...prefs.pinned];
    const forYou = [...new Set(forYouPaths)].map((p) => byPath.get(p)).filter((x): x is T => !!x && !hidden.has(x.to));
    return { forYou, groups: shown, folded };
}

const OPEN_KEY = 'cairn.sidebar.groups';

/** The person's open and closed groups on this device (empty when storage is blocked). */
export function readOpenGroups(user: string | undefined): Record<string, boolean> {
    try {
        const all = JSON.parse(window.localStorage.getItem(OPEN_KEY) || '{}');
        const mine = all && typeof all === 'object' ? all[user ?? ''] : null;
        return mine && typeof mine === 'object' ? mine : {};
    } catch {
        return {};
    }
}

/** Remembers a group opened or closed by the person (this device; storage can be blocked, then it lasts this page). */
export function writeOpenGroups(user: string | undefined, opened: Record<string, boolean>): void {
    try {
        const all = JSON.parse(window.localStorage.getItem(OPEN_KEY) || '{}');
        window.localStorage.setItem(OPEN_KEY, JSON.stringify({ ...(all && typeof all === 'object' ? all : {}), [user ?? '']: opened }));
    } catch {
        // storage blocked: the choice lasts while the page is open
    }
}

/** Pins or unpins `to` (a new prefs object). */
export function togglePin(prefs: SidebarPrefs, to: string): SidebarPrefs {
    const pinned = prefs.pinned.includes(to) ? prefs.pinned.filter((p) => p !== to) : [...prefs.pinned, to];
    return { ...prefs, pinned };
}

/** Hides or shows `to` (a new prefs object). */
export function toggleHidden(prefs: SidebarPrefs, to: string): SidebarPrefs {
    const hidden = prefs.hidden.includes(to) ? prefs.hidden.filter((p) => p !== to) : [...prefs.hidden, to];
    return { ...prefs, hidden };
}
