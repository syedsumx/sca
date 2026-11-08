'use strict';
/*
 * Cairn Desktop's rules, without Electron (tested with Node): which Cairn it opens, where a window may go, and
 * what a cairn:// link opens.
 */

const PROTOCOL = 'cairn';
const APP_PATH = '/cairn-ui/app.html';
/** Workspace sections a deep link may open (cairn://open/<section>/...). */
const SECTIONS = new Set(['discover', 'search', 'asset', 'marketplace', 'requests', 'assistant', 'context', 'governance',
    'reports', 'pipelines', 'sql', 'queries', 'dashboards', 'profile', 'settings', 'capabilities', 'agents', 'ml',
    'lakehouse', 'ingestion', 'compute', 'trust', 'sharing', 'usage', 'semantic']);

/** The Cairn origin from what a person or an operator typed: https only (http only to this computer). */
function normalizeCairnUrl(input) {
    let u;
    try {
        u = new URL(String(input || '').trim());
    } catch {
        throw new Error('Enter Cairn’s address, e.g. https://cairn.example.com');
    }
    const loopback = u.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(u.hostname);
    if (u.protocol !== 'https:' && !loopback) throw new Error('Cairn’s address must start with https://');
    if (u.username || u.password) throw new Error('Cairn’s address cannot carry a user name or password');
    return u.origin;
}

/** The workspace page for a section path such as "/asset/urn:li:dataset:(...)". */
function appUrl(cairnUrl, path = '/discover') {
    const p = String(path || '/discover');
    return `${cairnUrl}${APP_PATH}#${p.startsWith('/') ? p : `/${p}`}`;
}

/**
 * The page a deep link opens, or null: cairn://open/<section>[/<rest>][?query]. Anything else (another host, an
 * unknown section, a scheme trick) opens nothing.
 */
function deepLinkTarget(link, cairnUrl) {
    if (!cairnUrl || typeof link !== 'string' || !link.toLowerCase().startsWith(`${PROTOCOL}://`)) return null;
    let u;
    try {
        u = new URL(link);
    } catch {
        return null;
    }
    if (u.protocol !== `${PROTOCOL}:` || u.hostname !== 'open') return null;
    const parts = u.pathname.split('/').filter(Boolean);
    if (!parts.length || !SECTIONS.has(parts[0])) return null;
    const rest = parts.map((x) => encodeURIComponent(decodeURIComponent(x))).join('/');
    return appUrl(cairnUrl, `/${rest}${u.search}`);
}

/** May a window of the app show this address? Only Cairn's own origin (sign-in pages included). */
function sameOrigin(target, cairnUrl) {
    try {
        return new URL(target).origin === cairnUrl;
    } catch {
        return false;
    }
}

/** Links to elsewhere open in the default browser, and only web links (never file:, javascript: …). */
function externalAllowed(target) {
    try {
        return ['https:', 'http:', 'mailto:'].includes(new URL(target).protocol);
    } catch {
        return false;
    }
}

/** The deep link among a second instance's command line (Windows and Linux pass it there). */
function linkFromArgv(argv) {
    return (argv || []).find((a) => typeof a === 'string' && a.toLowerCase().startsWith(`${PROTOCOL}://`)) || null;
}

/** Where the Cairn address comes from: --cairn-url, CAIRN_URL, then the saved choice. */
function configuredUrl(argv, env, saved) {
    const flag = (argv || []).find((a) => typeof a === 'string' && a.startsWith('--cairn-url='));
    const raw = flag ? flag.slice('--cairn-url='.length) : (env && env.CAIRN_URL) || (saved && saved.cairnUrl) || null;
    return raw ? normalizeCairnUrl(raw) : null;
}

module.exports = { PROTOCOL, APP_PATH, normalizeCairnUrl, appUrl, deepLinkTarget, sameOrigin, externalAllowed,
    linkFromArgv, configuredUrl };
