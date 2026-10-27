/**
 * Navigation shared with the catalog shell.
 *
 * Each workspace section is its own entry in the shell's left navigation (deploy/mfe/mfe.config.yaml),
 * all mounting this same bundle: `/mfe/cairn-marketplace` opens the workspace on `#/marketplace`.
 * Moving between sections inside the workspace moves the shell too, so its navigation always shows
 * where you are, and every section has a URL of its own.
 */

/** Workspace route → shell path (without the `/mfe` prefix). Keep in sync with mfe.config.yaml. */
export const HOST_SECTIONS: { route: string; hostPath: string }[] = [
    { route: '/discover', hostPath: '/cairn' },
    { route: '/marketplace', hostPath: '/cairn-marketplace' },
    { route: '/requests', hostPath: '/cairn-requests' },
    { route: '/governance', hostPath: '/cairn-governance' },
    { route: '/context', hostPath: '/cairn-context' },
    { route: '/assistant', hostPath: '/cairn-assistant' },
    { route: '/settings', hostPath: '/cairn-settings' },
];

const MFE_PREFIX = '/mfe';

/** True when the shell hosts the workspace (the page path is one of the sections' shell paths). */
export function isHosted(pathname: string): boolean {
    return hostPathOf(pathname) !== null;
}

function hostPathOf(pathname: string): string | null {
    if (!pathname.startsWith(`${MFE_PREFIX}/`)) return null;
    const path = pathname.slice(MFE_PREFIX.length).replace(/\/+$/, '');
    return HOST_SECTIONS.some((s) => s.hostPath === path) ? path : null;
}

/** The workspace route a shell path opens on: `/mfe/cairn-marketplace` → `/marketplace`. */
export function sectionForHostPath(pathname: string): string | null {
    const path = hostPathOf(pathname);
    return HOST_SECTIONS.find((s) => s.hostPath === path)?.route ?? null;
}

/** The shell path that owns a workspace route: `/marketplace/urn:li:…` → `/cairn-marketplace`; `/search` → null. */
export function hostPathForRoute(route: string): string | null {
    const first = `/${route.replace(/^\/+/, '').split(/[/?#]/)[0]}`;
    return HOST_SECTIONS.find((s) => s.route === first)?.hostPath ?? null;
}

/**
 * Open `route` in the shell entry that owns it. The shell's router listens to `popstate`, so pushing the
 * new URL and announcing it switches the selected navigation item (and remounts the workspace there).
 * Returns false when the route belongs to the current entry (navigate inside the workspace instead).
 */
export function navigateHost(route: string, loc: Location = window.location, hist: History = window.history): boolean {
    const target = hostPathForRoute(route);
    if (!target || target === hostPathOf(loc.pathname)) return false;
    hist.pushState(null, '', `${MFE_PREFIX}${target}#${route}`);
    window.dispatchEvent(new PopStateEvent('popstate', { state: null }));
    return true;
}

/** Before the first render: a bare shell path (`/mfe/cairn-requests`) opens its own section. */
export function applyInitialSection(loc: Location = window.location, hist: History = window.history): void {
    const section = sectionForHostPath(loc.pathname);
    if (section && (!loc.hash || loc.hash === '#' || loc.hash === '#/')) {
        hist.replaceState(hist.state, '', `${loc.pathname}${loc.search}#${section}`);
    }
}
