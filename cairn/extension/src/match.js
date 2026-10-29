// Which BI page is open, and the part of its URL the catalog knows it by. Shared by the content
// script, the service worker and the tests; no browser APIs here.

/** @typedef {{ tool: 'looker' | 'tableau' | 'powerbi' | 'sigma' | 'superset' | 'metabase' | 'qlik', kind: 'dashboard' | 'look' | 'explore' | 'view' | 'workbook' | 'report' | 'chart' | 'question' | 'app', id: string, key: string }} BiPage */

const LOOKER = /^\/(dashboards(?:-next)?|looks|explore)\/([^/?#]+(?:\/[^/?#]+)?)/;
const TABLEAU = /^\/(?:t\/[^/]+\/|site\/[^/]+\/)?(views|workbooks)\/([^?]+)/;
const POWERBI = /^\/groups\/([^/]+)\/(reports|dashboards)\/([^/?#]+)/;
// Sigma: /<org>/workbook/<Name>-<id>; Superset and Preset: /superset/dashboard/<id or slug>/ and charts
// (explore ?slice_id=); Metabase: /dashboard/<id>-<slug>, /question/<id>-<slug>; Qlik Cloud: /sense/app/<id>/...
const SIGMA = /^\/([^/]+)\/workbook\/([^/?#]+)/;
const SUPERSET = /^\/superset\/dashboard\/([^/?#]+)\/?/;
const METABASE = /^\/(dashboard|question)\/(\d+)(?:-[^/?#]*)?/;
const QLIK = /^\/sense\/app\/([0-9a-f-]{8,})/i;

/**
 * @param {string} href
 * @param {{ looker?: string[], tableau?: string[], powerbi?: string[], sigma?: string[], superset?: string[], metabase?: string[], qlik?: string[] }} [custom] extra hosts per tool
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
    if (is('sigma', (h) => h === 'app.sigmacomputing.com' || h.endsWith('.sigmacomputing.com'))) {
        const m = SIGMA.exec(url.pathname);
        if (!m) return null;
        return { tool: 'sigma', kind: 'workbook', id: m[2], key: `${url.origin}/${m[1]}/workbook/${m[2]}` };
    }
    if (is('superset', (h) => h.endsWith('.preset.io'))) {
        const m = SUPERSET.exec(url.pathname);
        if (m) return { tool: 'superset', kind: 'dashboard', id: m[1], key: `${url.origin}/superset/dashboard/${m[1]}/` };
        const slice = url.pathname.startsWith('/explore') && url.searchParams.get('slice_id');
        if (slice) return { tool: 'superset', kind: 'chart', id: slice, key: `${url.origin}/explore/?slice_id=${slice}` };
        return null;
    }
    if (is('metabase', (h) => h.endsWith('.metabaseapp.com'))) {
        const m = METABASE.exec(url.pathname);
        if (!m) return null;
        // The catalog knows /dashboard/12, not the slug Metabase appends for people.
        return { tool: 'metabase', kind: m[1] === 'dashboard' ? 'dashboard' : 'question', id: m[2], key: `${url.origin}/${m[1]}/${m[2]}` };
    }
    if (is('qlik', (h) => h.endsWith('.qlikcloud.com'))) {
        const m = QLIK.exec(url.pathname);
        if (!m) return null;
        return { tool: 'qlik', kind: 'app', id: m[1], key: `${url.origin}/sense/app/${m[1]}` };
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
