// Which BI page is open, and the part of its URL the catalog knows it by. Shared by the content
// script, the service worker and the tests; no browser APIs here.

/** @typedef {{ tool: 'looker' | 'tableau' | 'powerbi', kind: 'dashboard' | 'look' | 'explore' | 'view' | 'workbook' | 'report', id: string, key: string }} BiPage */

const LOOKER = /^\/(dashboards(?:-next)?|looks|explore)\/([^/?#]+(?:\/[^/?#]+)?)/;
const TABLEAU = /^\/(?:t\/[^/]+\/|site\/[^/]+\/)?(views|workbooks)\/([^?]+)/;
const POWERBI = /^\/groups\/([^/]+)\/(reports|dashboards)\/([^/?#]+)/;

/**
 * @param {string} href
 * @param {{ looker?: string[], tableau?: string[], powerbi?: string[] }} [custom] extra hosts per tool
 * @returns {BiPage | null}
 */
export function recognise(href, custom = {}) {
    let url;
    try {
        url = new URL(href);
    } catch {
        return null;
    }
    const host = url.hostname;
    const is = (tool, test) => test(host) || (custom[tool] || []).includes(host);
    if (is('looker', (h) => h.endsWith('.looker.com'))) {
        const m = LOOKER.exec(url.pathname);
        if (!m) return null;
        const kind = m[1].startsWith('dashboards') ? 'dashboard' : m[1] === 'looks' ? 'look' : 'explore';
        return { tool: 'looker', kind, id: m[2], key: `${url.origin}${url.pathname}` };
    }
    if (is('tableau', (h) => h.endsWith('.tableau.com'))) {
        // Tableau Cloud and Server keep the route in the hash: /#/site/acme/views/Sales/Overview
        const route = url.hash.startsWith('#/') ? url.hash.slice(1).split('?')[0] : url.pathname;
        const m = TABLEAU.exec(route);
        if (!m) return null;
        const key = url.hash.startsWith('#/') ? `${url.origin}/#${route}` : `${url.origin}${url.pathname}`;
        return { tool: 'tableau', kind: m[1] === 'views' ? 'view' : 'workbook', id: m[2].replace(/\/$/, ''), key };
    }
    if (is('powerbi', (h) => h === 'app.powerbi.com')) {
        const m = POWERBI.exec(url.pathname);
        if (!m) return null;
        return {
            tool: 'powerbi',
            kind: m[2] === 'reports' ? 'report' : 'dashboard',
            id: m[3],
            key: `${url.origin}/groups/${m[1]}/${m[2]}/${m[3]}`,
        };
    }
    return null;
}

/** Content-script match patterns for custom hosts, e.g. a self-hosted Looker. */
export function patternsFor(custom) {
    return Object.values(custom || {})
        .flat()
        .filter(Boolean)
        .map((h) => `https://${h}/*`);
}
