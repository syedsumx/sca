import type { Hub, ResourceSpec } from '../resources/types';
import TOPICS from './help-topics.json';

/** A section of the in-product docs, bundled at build time (scripts/build-help.mjs). */
export interface HelpSnippet {
    title: string;
    body: string;
    /** The section goes on past the excerpt. */
    more: boolean;
}

const PAGE_TOPICS = TOPICS as Record<string, string>;

const TAB_KEY = (route: string) => `cairn.hub.tab:${route}`;

/** The tab a hub opens on: the one in the URL, else the one used last on this device, else the first. */
export function hubTab(hub: Hub, search: string, tabs: string[] = hub.tabs): string | undefined {
    const asked = new URLSearchParams(search).get('tab');
    if (asked && tabs.includes(asked)) return asked;
    try {
        const last = window.localStorage.getItem(TAB_KEY(hub.route));
        if (last && tabs.includes(last)) return last;
    } catch {
        // storage blocked
    }
    return tabs[0];
}

export function rememberHubTab(route: string, tab: string): void {
    try {
        window.localStorage.setItem(TAB_KEY(route), tab);
    } catch {
        // storage blocked: the hub opens on its first tab
    }
}

/**
 * The docs section (`page.html#anchor`) about what is on screen: a hub's open tab (its area's `docs`), else the
 * page's own topic (the longest route prefix that has one).
 */
export function helpAnchorFor(pathname: string, search: string, hubs: Hub[], specs: Record<string, ResourceSpec>): string | null {
    const hub = hubs.find((h) => h.route === pathname);
    if (hub) {
        const tabs = hub.tabs.filter((k) => specs[k]);
        const tab = hubTab(hub, search, tabs);
        const docs = (tab && specs[tab]?.docs) || tabs.map((k) => specs[k].docs).find(Boolean);
        return docs ?? null;
    }
    if (PAGE_TOPICS[pathname]) return PAGE_TOPICS[pathname];
    const prefix = Object.keys(PAGE_TOPICS).filter((p) => pathname.startsWith(`${p}/`)).sort((a, b) => b.length - a.length)[0];
    return prefix ? PAGE_TOPICS[prefix] : null;
}

/** A purpose-built page's docs section (`help-topics.json`), for its header. */
export function pageHelp(route: string): string | undefined {
    return PAGE_TOPICS[route];
}

/** What the page is called in the help's question to the assistant. */
export function pageTitleFor(pathname: string, search: string, hubs: Hub[], specs: Record<string, ResourceSpec>): string {
    const hub = hubs.find((h) => h.route === pathname);
    if (hub) {
        const tab = hubTab(hub, search, hub.tabs.filter((k) => specs[k]));
        return tab && specs[tab] ? `${hub.title} › ${specs[tab].title}` : hub.title;
    }
    return pathname;
}

let loaded: Promise<Record<string, HelpSnippet>> | null = null;

/** The bundled snippets (their own chunk, loaded the first time help opens). */
export function loadSnippets(): Promise<Record<string, HelpSnippet>> {
    if (!loaded) {
        loaded = import('./help.generated.json').then((m) => ((m as { default?: unknown }).default ?? m) as Record<string, HelpSnippet>);
        loaded.catch(() => {
            loaded = null;
        });
    }
    return loaded;
}

export async function helpFor(anchor: string | null): Promise<HelpSnippet | null> {
    if (!anchor) return null;
    return (await loadSnippets())[anchor] ?? null;
}

/** The full docs' address for an anchor. */
export const docsHref = (anchor: string | null) => `/docs/${anchor ?? 'index.html'}`;
