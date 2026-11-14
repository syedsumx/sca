// Test harness: the person's part of the sign-in (Cairn's consent page), and the add-ins' own JavaScript loaded
// into Node with small stand-ins for the host APIs they use (Office's CustomFunctions and OfficeRuntime.storage;
// Apps Script's SpreadsheetApp, UrlFetchApp, PropertiesService and friends).
import { spawnSync } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const here = dirname(fileURLToPath(import.meta.url));
export const live = {
    url: process.env.CAIRN_TEST_URL,
    dist: process.env.CAIRN_TEST_DIST,
    person: process.env.CAIRN_TEST_PERSON || 'urn:li:corpuser:ana',
    scriptId: process.env.CAIRN_TEST_SCRIPT_ID,
    addinOrigin: process.env.CAIRN_TEST_ADDIN_ORIGIN,
};

/** Signs in on Cairn's authorization page as `person` (a signed-in browser, here the dev identity header),
 *  allows every scope offered, and returns where Cairn sends the browser back to. */
export async function consent(authorizeUrl, person) {
    const headers = { 'X-Cairn-Actor': person };
    const page = await fetch(authorizeUrl, { headers, redirect: 'manual' });
    const html = await page.text();
    if (page.status !== 200) throw new Error(`authorize page: ${page.status} ${page.headers.get('location') || html}`);
    const form = new URLSearchParams();
    for (const [, k, v] of html.matchAll(/<input type="hidden" name="(\w+)" value="([^"]*)">/g)) {
        form.append(k, v.replace(/&quot;/g, '"').replace(/&#x27;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&'));
    }
    const scopes = [...html.matchAll(/name="scope" value="(\w+)"/g)].map((m) => m[1]);
    for (const s of scopes) form.append('scope', s);
    form.append('decision', 'allow');
    const base = authorizeUrl.slice(0, authorizeUrl.indexOf('/oauth/authorize'));
    const done = await fetch(`${base}/oauth/authorize`, {
        method: 'POST', headers: { ...headers, 'Content-Type': 'application/x-www-form-urlencoded' },
        body: form.toString(), redirect: 'manual',
    });
    if (done.status !== 302) throw new Error(`consent: ${done.status} ${await done.text()}`);
    return { location: done.headers.get('location'), scopes };
}

// ---- Office ---------------------------------------------------------------------------------------------------
export function loadExcel(dist, { fetchImpl } = {}) {
    const store = new Map();
    const registry = {};
    const calls = [];
    class CFError extends Error {
        constructor(code, message) {
            super(message);
            this.code = code;
        }
    }
    const countingFetch = (url, init) => {
        calls.push({ url: String(url), method: (init && init.method) || 'GET', body: init && init.body });
        return (fetchImpl || fetch)(url, init);
    };
    const ctx = {
        console, crypto: globalThis.crypto, TextEncoder, URL, URLSearchParams, setTimeout, clearTimeout, btoa, Buffer,
        fetch: countingFetch,
        CustomFunctions: {
            associate: (id, fn) => { registry[id] = fn; },
            Error: CFError,
            ErrorCode: { invalidValue: '#VALUE!', notAvailable: '#N/A' },
        },
        OfficeRuntime: {
            storage: {
                getItem: async (k) => (store.has(k) ? store.get(k) : null),
                setItem: async (k, v) => { store.set(k, String(v)); },
                removeItem: async (k) => { store.delete(k); },
            },
        },
    };
    ctx.window = ctx;
    vm.createContext(ctx);
    for (const f of ['config.js', 'cairn.js', 'functions.js']) {
        vm.runInContext(readFileSync(join(dist, 'excel', f), 'utf8'), ctx, { filename: f });
    }
    return { ctx, registry, store, calls, client: ctx.CairnAddin.fromConfig() };
}

// ---- Apps Script ----------------------------------------------------------------------------------------------
function syncFetch(url, params = {}) {
    const headers = { ...(params.headers || {}) };
    if (params.contentType) headers['Content-Type'] = params.contentType;
    const req = { url, method: (params.method || 'get').toUpperCase(), headers, body: params.payload ?? null };
    const r = spawnSync(process.execPath, [join(here, 'fetch-sync.mjs')], { input: JSON.stringify(req), encoding: 'utf8',
                                                                        timeout: 60000 });
    if (r.status !== 0) throw new Error(`UrlFetchApp: ${r.stderr}`);
    const res = JSON.parse(r.stdout);
    if (res.error) throw new Error(`Exception: ${res.error}: ${url}`);
    if (!params.muteHttpExceptions && res.status >= 400) throw new Error(`Request failed for ${url} returned code ${res.status}`);
    return { getResponseCode: () => res.status, getContentText: () => res.body, getHeaders: () => res.headers };
}

function propertyStore(mode, readOnlyInCustomFunctions) {
    const m = new Map();
    const guard = (what) => {
        if (readOnlyInCustomFunctions && mode.customFunction) {
            throw new Error(`You do not have permission to call ${what}`);
        }
    };
    const store = {
        getProperty: (k) => (m.has(k) ? m.get(k) : null),
        setProperty: (k, v) => { guard('setProperty'); m.set(k, String(v)); return store; },
        deleteProperty: (k) => { guard('deleteProperty'); m.delete(k); return store; },
        getProperties: () => Object.fromEntries(m),
    };
    return store;
}

function htmlOutput(content) {
    const out = { content, title: null };
    return {
        getContent: () => out.content,
        setTitle(t) { out.title = t; return this; },
        getTitle: () => out.title,
        setWidth() { return this; },
        setHeight() { return this; },
    };
}

export function loadSheets(dist, { api, scriptId }) {
    const mode = { customFunction: false };
    const script = propertyStore(mode, false);
    const user = propertyStore(mode, true);
    const ui = { menus: [], sidebars: [], dialogs: [] };
    const triggers = [];
    const cells = { formulas: [], values: [] };
    const stateTokens = [];
    const toSigned = (buf) => [...buf].map((b) => (b > 127 ? b - 256 : b));
    const ctx = {
        console, JSON, Date, Math, encodeURIComponent, decodeURIComponent,
        PropertiesService: { getScriptProperties: () => script, getUserProperties: () => user },
        UrlFetchApp: { fetch: syncFetch },
        LockService: {
            getUserLock: () => {
                if (mode.customFunction) throw new Error('You do not have permission to call getUserLock');
                return { waitLock() {}, releaseLock() {} };
            },
        },
        Utilities: {
            getUuid: () => randomUUID(),
            DigestAlgorithm: { SHA_256: 'sha256' },
            Charset: { US_ASCII: 'ascii', UTF_8: 'utf8' },
            computeDigest: (alg, value, charset) => toSigned(createHash(alg).update(Buffer.from(String(value), charset || 'utf8')).digest()),
            base64EncodeWebSafe: (bytes) => Buffer.from(bytes.map((b) => b & 255)).toString('base64').replace(/\+/g, '-').replace(/\//g, '_'),
        },
        ScriptApp: {
            getScriptId: () => scriptId,
            newStateToken() {
                const t = { method: null, timeout: null };
                return {
                    withMethod(m) { t.method = m; return this; },
                    withArgument() { return this; },
                    withTimeout(s) { t.timeout = s; return this; },
                    createToken() { const tok = `ST_${randomUUID()}`; stateTokens.push({ ...t, token: tok }); return tok; },
                };
            },
            getProjectTriggers: () => triggers.slice(),
            deleteTrigger: (tr) => { triggers.splice(triggers.indexOf(tr), 1); },
            newTrigger(handler) {
                const spec = { handler, every: null };
                return {
                    timeBased() { return this; },
                    everyHours(h) { spec.every = `${h}h`; return this; },
                    everyMinutes(m) { spec.every = `${m}m`; return this; },
                    create() { const tr = { ...spec, getHandlerFunction: () => handler }; triggers.push(tr); return tr; },
                };
            },
        },
        HtmlService: {
            createHtmlOutput: htmlOutput,
            createHtmlOutputFromFile: (name) => htmlOutput(readFileSync(join(dist, 'sheets', `${name}.html`), 'utf8')),
            createTemplateFromFile: (name) => ({ evaluate: () => htmlOutput(readFileSync(join(dist, 'sheets', `${name}.html`), 'utf8')) }),
        },
        SpreadsheetApp: {
            getUi: () => ({
                createAddonMenu() {
                    const menu = { items: [] };
                    ui.menus.push(menu);
                    return {
                        addItem(label, fn) { menu.items.push([label, fn]); return this; },
                        addSeparator() { menu.items.push(['-']); return this; },
                        addToUi() { menu.added = true; },
                    };
                },
                showSidebar: (html) => ui.sidebars.push(html),
                showModalDialog: (html, title) => ui.dialogs.push({ html, title }),
            }),
            getActiveRange: () => ({
                getCell: () => ({
                    setFormula: (f) => { cells.formulas.push(f); },
                    getRow: () => 3, getColumn: () => 2,
                    getSheet: () => ({ getRange: (r, c, h, w) => ({ setValues: (v) => { cells.values.push({ r, c, h, w, v }); } }) }),
                }),
            }),
        },
    };
    vm.createContext(ctx);
    vm.runInContext(readFileSync(join(dist, 'sheets', 'Code.gs'), 'utf8'), ctx, { filename: 'Code.gs' });
    script.setProperty('CAIRN_API_URL', api);
    /** Runs `fn` the way Sheets runs a custom function: properties are read-only, no locks. */
    const asCustomFunction = (fn) => {
        mode.customFunction = true;
        try {
            return fn();
        } finally {
            mode.customFunction = false;
        }
    };
    return { gs: ctx, ui, triggers, cells, user, script, stateTokens, asCustomFunction };
}
