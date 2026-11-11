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
 * The sidebar groups a role needs (the rest fold away until "Show everything"). Every role keeps the Catalog (Cairn
 * Core, the centre of the workspace) and Platform.
 */
export const ROLE_GROUPS: Record<Role, string[] | null> = {
    analyst: ['nav.group.catalog', 'nav.group.analyze', 'nav.group.ai', 'nav.group.platform'],
    engineer: ['nav.group.catalog', 'nav.group.quality', 'nav.group.analyze', 'nav.group.build', 'nav.group.ai',
        'nav.group.platform'],
    steward: ['nav.group.catalog', 'nav.group.govern', 'nav.group.quality', 'nav.group.ai', 'nav.group.platform'],
    admin: null,
    explorer: ['nav.group.catalog', 'nav.group.analyze', 'nav.group.platform'],
};

export interface SidebarPrefs {
    pinned: string[];
    hidden: string[];
    show_all: boolean;
}

export interface Arranged<T extends NavEntry> {
    /** "For you": the role's pages and the person's pins, in that order, without repeats. */
    forYou: T[];
    groups: NavGroupOf<T>[];
    /** How many entries "Show everything" would add. */
    folded: number;
}

/**
 * The sidebar for a person: `allowed` (what their capabilities let them use; never overridden), then their role
 * (groups it rarely needs are folded) and their own choices (hidden entries left out, pins added to "For you").
 * "Show everything" (`show_all`) unfolds the groups and shows the hidden entries, still only those allowed.
 */
export function arrangeNav<T extends NavEntry>(groups: NavGroupOf<T>[], allowed: (item: T) => boolean, role: Role | null,
    prefs: SidebarPrefs): Arranged<T> {
    const usable = groups.map((g) => ({ ...g, items: g.items.filter(allowed) }));
    const byPath = new Map(usable.flatMap((g) => g.items).map((i) => [i.to, i]));
    const keep = role ? ROLE_GROUPS[role] : null;
    const hidden = new Set(prefs.show_all ? [] : prefs.hidden);
    let folded = 0;
    const shown = usable.map((g) => {
        const groupOn = prefs.show_all || !keep || keep.includes(g.label);
        const items = g.items.filter((i) => groupOn && !hidden.has(i.to));
        folded += g.items.length - items.length;
        return { ...g, items };
    }).filter((g) => g.items.length > 0);
    const forYouPaths = [...(role ? ROLE_PINS[role] : []), ...prefs.pinned];
    const forYou = [...new Set(forYouPaths)].map((p) => byPath.get(p)).filter((x): x is T => !!x && !hidden.has(x.to));
    return { forYou, groups: shown, folded };
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
