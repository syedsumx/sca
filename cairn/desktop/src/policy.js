'use strict';
/*
 * What Cairn Desktop may do, from three places, the strictest winning:
 *
 * 1. The computer's policy file, for IT and MDM: /etc/cairn/desktop-policy.json (Linux),
 *    /Library/Application Support/Cairn/desktop-policy.json (macOS), %ProgramData%\Cairn\desktop-policy.json
 *    (Windows). CAIRN_DESKTOP_POLICY_FILE names one more file; it can only add restrictions (both are read).
 * 2. Each workspace's settings (GET /v1/desktop/policy, set by its administrators).
 * 3. The person's own preferences.
 *
 * A file that exists but cannot be read or parsed fails closed: no updates and no offline cache until it is fixed.
 */
const fs = require('fs');
const path = require('path');

const UPDATE_LEVELS = ['off', 'notify', 'auto'];
const LIMITS = { pages: [1, 100, 20], days: [1, 90, 14] };

function machinePolicyFiles(platform = process.platform, env = process.env) {
    const files = [];
    if (platform === 'win32') files.push(path.join(env.ProgramData || 'C:\\ProgramData', 'Cairn', 'desktop-policy.json'));
    else if (platform === 'darwin') files.push('/Library/Application Support/Cairn/desktop-policy.json');
    else files.push('/etc/cairn/desktop-policy.json');
    if ((env.CAIRN_DESKTOP_POLICY_FILE || '').trim()) files.push(env.CAIRN_DESKTOP_POLICY_FILE.trim());
    return files;
}

const LOCKED = { updates: 'off', offline_cache: false, cli_install: false, global_shortcut: false, error: null };

/** The computer's policy: the files merged strictly. `read(file)` → text, or throws ENOENT. */
function readMachinePolicy(files, read = (f) => fs.readFileSync(f, 'utf8')) {
    const found = [];
    for (const file of files) {
        let text;
        try {
            text = read(file);
        } catch (e) {
            if (e && e.code === 'ENOENT') continue;
            return { ...LOCKED, error: `${file}: ${e.message}` };
        }
        try {
            const doc = JSON.parse(text);
            if (!doc || typeof doc !== 'object' || Array.isArray(doc)) throw new Error('not a JSON object');
            found.push(doc);
        } catch (e) {
            return { ...LOCKED, error: `${file}: ${e.message}` };
        }
    }
    return mergeStrict(found);
}

function stricterUpdates(a, b) {
    const ia = UPDATE_LEVELS.indexOf(a);
    const ib = UPDATE_LEVELS.indexOf(b);
    if (ia < 0) return ib < 0 ? undefined : b;
    if (ib < 0) return a;
    return UPDATE_LEVELS[Math.min(ia, ib)];
}

function compareVersions(a, b) {
    const pa = String(a).replace(/^v/, '').split(/[.+-]/).slice(0, 3).map((x) => parseInt(x, 10) || 0);
    const pb = String(b).replace(/^v/, '').split(/[.+-]/).slice(0, 3).map((x) => parseInt(x, 10) || 0);
    for (let i = 0; i < 3; i += 1) if (pa[i] !== pb[i]) return pa[i] < pb[i] ? -1 : 1;
    return 0;
}

function clampInt(v, [low, high]) {
    return Number.isInteger(v) ? Math.min(high, Math.max(low, v)) : undefined;
}

/** Several policy documents as one: the strictest value of each setting. */
function mergeStrict(docs) {
    const out = { error: null };
    for (const d of docs) {
        if (d.updates !== undefined) out.updates = stricterUpdates(out.updates, d.updates) || 'off';
        if (d.pinned_version) {
            out.pinned_version = out.pinned_version && compareVersions(out.pinned_version, d.pinned_version) <= 0
                ? out.pinned_version : String(d.pinned_version).replace(/^v/, '');
        }
        if (d.update_feed_url && !out.update_feed_url) out.update_feed_url = String(d.update_feed_url);
        for (const k of ['offline_cache', 'cli_install', 'global_shortcut']) {
            if (d[k] !== undefined) out[k] = out[k] === false ? false : d[k] === true;
        }
        const pages = clampInt(d.offline_cache_pages, LIMITS.pages);
        if (pages !== undefined) out.offline_cache_pages = Math.min(out.offline_cache_pages || 100, pages);
        const days = clampInt(d.offline_cache_max_age_days, LIMITS.days);
        if (days !== undefined) out.offline_cache_max_age_days = Math.min(out.offline_cache_max_age_days || 90, days);
        if (Array.isArray(d.allowed_origins)) {
            const list = d.allowed_origins.map(String);
            out.allowed_origins = out.allowed_origins ? out.allowed_origins.filter((o) => list.includes(o)) : list;
        }
        if (Array.isArray(d.workspaces)) {
            out.workspaces = [...(out.workspaces || []), ...d.workspaces.filter((w) => w && w.name && w.url)];
        }
    }
    return out;
}

/**
 * Offline caching for one workspace: allowed only when the computer, the workspace (whose settings must be known:
 * unknown is no) and the person all allow it. {enabled, maxPages, maxAgeDays}.
 */
function offlineFor(machine, workspacePolicy, prefs = {}) {
    const known = !!workspacePolicy && typeof workspacePolicy === 'object';
    const enabled = known && !machine.error && machine.offline_cache !== false && workspacePolicy.offline_cache !== false
        && prefs.offlineCache !== false;
    const pages = Math.min(machine.offline_cache_pages || 100, clampInt(workspacePolicy && workspacePolicy.offline_cache_pages, LIMITS.pages) || LIMITS.pages[2]);
    const days = Math.min(machine.offline_cache_max_age_days || 90,
        clampInt(workspacePolicy && workspacePolicy.offline_cache_max_age_days, LIMITS.days) || LIMITS.days[2]);
    return { enabled, maxPages: pages, maxAgeDays: days };
}

/** The app's update settings: the computer's file, then every workspace's, then the person's choice. */
function updatePolicy(machine, workspacePolicies = [], prefs = {}) {
    let updates = machine.error ? 'off' : (machine.updates || 'auto');
    let pinned = machine.pinned_version || null;
    let feed = machine.update_feed_url || null;
    const source = { updates: machine.updates ? 'computer' : null, pinned: pinned ? 'computer' : null };
    for (const w of workspacePolicies) {
        if (!w || !w.policy) continue;
        const next = stricterUpdates(updates, w.policy.updates);
        if (next !== updates) source.updates = w.name;
        updates = next || updates;
        if (!machine.pinned_version && w.policy.pinned_version
                && (!pinned || compareVersions(w.policy.pinned_version, pinned) < 0)) {
            pinned = String(w.policy.pinned_version).replace(/^v/, '');
            source.pinned = w.name;
        }
        if (!feed && w.policy.update_feed_url) feed = w.policy.update_feed_url;
    }
    if (prefs.updates === false && updates !== 'off') {
        updates = 'off';
        source.updates = 'you';
    }
    return { updates, pinned, feed, source };
}

/**
 * What to do about an update: `none`, `download` (then install on quit), `notify` (say it is out and link to the
 * download) or `blocked` (pinned or turned off; a reason is given).
 *
 * macOS installs updates only into a signed app, so an unsigned build there notifies instead of downloading.
 * Windows (NSIS) and Linux (AppImage) update unsigned, checked against the feed's SHA-512.
 */
function updateDecision({ policy, current, available, platform, signed, packaged = true, appImage = true }) {
    if (!available || compareVersions(available, current) <= 0) return { action: 'none', reason: 'up to date' };
    if (policy.updates === 'off') return { action: 'blocked', reason: `updates are turned off (${policy.source.updates || 'policy'})` };
    if (policy.pinned && compareVersions(available, policy.pinned) > 0) {
        return { action: 'blocked', reason: `pinned to ${policy.pinned} (${policy.source.pinned || 'policy'})` };
    }
    if (policy.updates === 'notify') return { action: 'notify', reason: 'your administrator asks to be told only' };
    if (!packaged) return { action: 'notify', reason: 'a development build does not update itself' };
    if (platform === 'darwin' && !signed) return { action: 'notify', reason: 'macOS installs updates only into a signed app' };
    if (platform === 'linux' && !appImage) return { action: 'notify', reason: 'installed from a package: update it with your package manager' };
    return { action: 'download', reason: 'an update is out' };
}

module.exports = {
    machinePolicyFiles, readMachinePolicy, mergeStrict, offlineFor, updatePolicy, updateDecision, compareVersions,
    UPDATE_LEVELS,
};
