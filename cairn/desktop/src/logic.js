'use strict';
/*
 * Cairn Desktop's rules, without Electron (tested with Node): workspace addresses, what a cairn:// link opens, where
 * a window may go, which permissions a page gets, where a notification leads, and which pages may be kept offline.
 */
const crypto = require('crypto');

const PROTOCOL = 'cairn';
const APP_PATH = '/cairn-ui/app.html';
/** Top-level pages of the workspace a deep link may open (the web UI's routes, ConsumerApp.tsx and its hubs). */
const SECTIONS = new Set(['discover', 'search', 'asset', 'marketplace', 'requests', 'assistant', 'context', 'governance',
    'reports', 'pipelines', 'sql', 'flows', 'ontology', 'queries', 'dashboards', 'capabilities', 'profile', 'device',
    'settings', 'extensions', 'semantic', 'ingestion', 'lakehouse', 'compute', 'agents', 'ml', 'trust', 'sharing',
    'usage']);
/** `cairn://open/...` opens in the workspace on screen (Cairn Desktop 0.x links), so no workspace is called that. */
const CURRENT = 'open';
/** The CLI's rule for profile names (cairn_cli.config.PROFILE_NAME): workspaces are the CLI's profiles. */
const PROFILE_NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$/;
const LOOPBACK = new Set(['localhost', '127.0.0.1', '[::1]']);
/** Pages kept for reading offline: the dashboards and reports a person opened. */
const OFFLINE_ROUTES = ['/dashboards', '/reports'];
/** Chromium's network errors that mean "Cairn cannot be reached" (not "Cairn said no"). */
const OFFLINE_ERRORS = new Set([-2, -7, -21, -100, -101, -102, -104, -105, -106, -109, -118, -130, -137, -324]);

/**
 * A workspace address as typed: https only (http only to this computer, for development), no user name or password.
 * Returns the address without a trailing slash, path kept (people paste the API's address or the site's).
 */
function normalizeWorkspaceUrl(input) {
    let u;
    try {
        u = new URL(String(input || '').trim());
    } catch {
        throw new Error('Enter your Cairn’s address, e.g. https://cairn.example.com');
    }
    const loopback = u.protocol === 'http:' && LOOPBACK.has(u.hostname);
    if (u.protocol !== 'https:' && !loopback) throw new Error('Cairn’s address must start with https://');
    if (u.username || u.password) throw new Error('Cairn’s address cannot carry a user name or password');
    u.hash = '';
    u.search = '';
    let path = u.pathname.replace(/\/+$/, '');
    if (path.endsWith('/cairn-ui/app.html')) path = path.slice(0, -'/cairn-ui/app.html'.length);
    if (path.endsWith('/health')) path = path.slice(0, -'/health'.length);
    return `${u.origin}${path}`;
}

/** Where to look for Cairn's API from an address (as `cairn login` does): the address, then its /cairn/api. */
function apiCandidates(url) {
    const base = normalizeWorkspaceUrl(url);
    return base.endsWith('/cairn/api') ? [base] : [base, `${base}/cairn/api`];
}

/** Does a /health answer come from Cairn's API? */
function isCairnHealth(status, body) {
    return status === 200 && !!body && typeof body === 'object' && body.service === 'api';
}

function originOf(url) {
    try {
        return new URL(url).origin;
    } catch {
        return null;
    }
}

/** The web UI page for a route such as "/asset/urn:li:dataset:(...)". */
function appUrl(origin, route = '/discover') {
    const r = String(route || '/discover');
    return `${origin}${APP_PATH}#${r.startsWith('/') ? r : `/${r}`}`;
}

function checkWorkspaceName(name) {
    const n = String(name || '').trim();
    if (!PROFILE_NAME.test(n)) throw new Error('A name of letters, digits, “.”, “_” and “-” (at most 63)');
    if (n.toLowerCase() === CURRENT) throw new Error('“open” is kept for links to the workspace on screen');
    return n;
}

/** A name for a new workspace from its address: the host's first label, made unique among the existing ones. */
function suggestName(url, existing = []) {
    let host = 'cairn';
    try {
        host = new URL(url).hostname.replace(/^\[|\]$/g, '');
    } catch { /* the default */ }
    const label = host === 'localhost' || /^[\d.:]+$/.test(host) ? 'local' : host.split('.')[0];
    let base = label.replace(/[^A-Za-z0-9_.-]/g, '-').replace(/^[^A-Za-z0-9]+/, '').slice(0, 50) || 'cairn';
    if (base.toLowerCase() === CURRENT) base = 'cairn-open';
    const taken = new Set(existing.map((x) => x.toLowerCase()));
    if (!taken.has(base.toLowerCase())) return base;
    for (let i = 2; ; i += 1) if (!taken.has(`${base}-${i}`.toLowerCase())) return `${base}-${i}`;
}

/** Each workspace's own browser storage: its name and address, so a renamed or moved workspace starts afresh. */
function partitionFor(name, origin) {
    const h = crypto.createHash('sha256').update(`${name}\n${origin}`).digest('hex').slice(0, 24);
    return `persist:cairn-ws-${h}`;
}

/**
 * A cairn:// link, checked: cairn://<workspace>/<section>[/<rest>][?query]. Returns {workspace, route} (workspace
 * null for cairn://open/..., the workspace on screen) or null for anything else (unknown section, credentials, a
 * port, dot segments, control characters, over 2048 characters).
 */
function parseDeepLink(link) {
    if (typeof link !== 'string' || link.length > 2048 || /[\u0000-\u001f\u007f\s\\]/.test(link)) return null;
    if (!link.toLowerCase().startsWith(`${PROTOCOL}://`)) return null;
    let u;
    try {
        u = new URL(link);
    } catch {
        return null;
    }
    if (u.protocol !== `${PROTOCOL}:` || u.username || u.password || u.port || u.hash) return null;
    const host = decodeURIComponent(u.host);
    if (!PROFILE_NAME.test(host)) return null;
    const raw = u.pathname.split('/').filter(Boolean);
    let parts;
    try {
        parts = raw.map((x) => decodeURIComponent(x));
    } catch {
        return null;
    }
    if (!parts.length || !SECTIONS.has(parts[0]) || parts.some((p) => p === '.' || p === '..')) {
        return null;
    }
    const route = `/${parts.map((x) => encodeURIComponent(x)).join('/')}${u.search}`;
    return { workspace: host.toLowerCase() === CURRENT ? null : host, route };
}

/** The workspace a link names among the known ones: the exact name, else the only one equal ignoring case. */
function findWorkspace(name, names) {
    if (names.includes(name)) return name;
    const same = names.filter((n) => n.toLowerCase() === String(name).toLowerCase());
    return same.length === 1 ? same[0] : null;
}

/** What a link does: open a page in a workspace, ask to add an unknown workspace, or nothing. */
function resolveDeepLink(link, names, active) {
    const parsed = parseDeepLink(link);
    if (!parsed) return null;
    if (parsed.workspace === null) {
        return active ? { kind: 'open', workspace: active, route: parsed.route } : null;
    }
    const found = findWorkspace(parsed.workspace, names);
    if (found) return { kind: 'open', workspace: found, route: parsed.route };
    return { kind: 'unknown', workspace: parsed.workspace, route: parsed.route };
}

/** The deep link among a launch's arguments (Windows and Linux pass it there). */
function linkFromArgv(argv) {
    return (argv || []).find((a) => typeof a === 'string' && a.toLowerCase().startsWith(`${PROTOCOL}://`)) || null;
}

/** Links to elsewhere open in the default browser, and only web and mail links (never file:, javascript: …). */
function externalAllowed(target) {
    try {
        return ['https:', 'http:', 'mailto:'].includes(new URL(target).protocol);
    } catch {
        return false;
    }
}

/**
 * Where a workspace's window may go. `allow`: show it; `external`: the default browser; `drop`: a file dropped on a
 * page that does not take files; `deny`: nothing.
 *
 * The workspace's own origin is shown. Sign-in may leave it: a redirect the server sends (single sign-on to the
 * identity provider) starts a sign-in detour, during which https pages are shown until the workspace is back; the
 * administrator's `sign_in_origins` are always shown. Everything else opens in the default browser.
 */
function navigationDecision({ url, workspaceOrigin, signInOrigins = [], inSignIn = false, redirect = false }) {
    let u;
    try {
        u = new URL(url);
    } catch {
        return 'deny';
    }
    if (u.protocol === 'file:') return 'drop';
    if (u.username || u.password) return 'deny';
    if (workspaceOrigin && u.origin === workspaceOrigin) return 'allow';
    if (u.protocol === 'https:' && (redirect || inSignIn || signInOrigins.includes(u.origin))) return 'allow';
    return externalAllowed(url) ? 'external' : 'deny';
}

/**
 * A permission a page asks for. `allow`: notifications, sanitized clipboard writes and full screen for the workspace;
 * `ask`: camera and microphone for the workspace (the person decides); everything else, and every other origin: deny.
 */
function permissionDecision(permission, requestingOrigin, workspaceOrigin) {
    if (!workspaceOrigin || originOf(requestingOrigin || '') !== workspaceOrigin) return 'deny';
    if (['notifications', 'clipboard-sanitized-write', 'fullscreen'].includes(permission)) return 'allow';
    if (permission === 'media') return 'ask';
    return 'deny';
}

/** Where a notification leads: a page of the workspace (`{url}`), a link elsewhere (`{external}`), or the inbox. */
function notificationTarget(actionUrl, origin) {
    const inbox = { url: appUrl(origin, '/settings') };
    if (!actionUrl) return inbox;
    let u;
    try {
        u = new URL(actionUrl, `${origin}${APP_PATH}`);
    } catch {
        return inbox;
    }
    if (u.origin !== origin) return externalAllowed(u.href) ? { external: u.href } : inbox;
    if (u.pathname === APP_PATH) return { url: u.href };
    const parts = u.pathname.split('/').filter(Boolean);
    if (parts[0] === 'cairn' && SECTIONS.has(parts[1])) {          // links to the classic UI's /cairn/<section>/...
        return { url: appUrl(origin, `/${parts.slice(1).join('/')}${u.search}`) };
    }
    return { url: u.href };
}

/** The page of a workspace URL that may be kept offline (dashboards and reports), as its key, or null. */
function offlineKey(url, origin) {
    let u;
    try {
        u = new URL(url);
    } catch {
        return null;
    }
    if (u.origin !== origin || u.pathname !== APP_PATH) return null;
    const route = u.hash.replace(/^#/, '') || '/';
    const path = route.split('?')[0].replace(/\/+$/, '');
    return OFFLINE_ROUTES.includes(path) ? route : null;
}

function isOfflineError(code) {
    return OFFLINE_ERRORS.has(Number(code));
}

module.exports = {
    PROTOCOL, APP_PATH, SECTIONS, CURRENT, PROFILE_NAME, normalizeWorkspaceUrl, apiCandidates, isCairnHealth, originOf,
    appUrl, checkWorkspaceName, suggestName, partitionFor, parseDeepLink, findWorkspace, resolveDeepLink, linkFromArgv,
    externalAllowed, navigationDecision, permissionDecision, notificationTarget, offlineKey, isOfflineError,
};
