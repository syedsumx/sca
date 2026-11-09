'use strict';
/*
 * Cairn Desktop's main process.
 *
 * One window: a thin top bar (the workspace switcher, status, Ask Cairn) over the active workspace's page, Cairn's
 * own web UI (/cairn-ui/app.html) in a WebContentsView with that workspace's own session partition, so cookies and
 * storage never mix between workspaces. A panel (add a workspace, offline, settings) takes the page's place when
 * needed. Workspaces are the command line's profiles (profiles.js).
 *
 * Security: pages run sandboxed and context-isolated without Node.js; a workspace's window shows its own origin (and
 * its sign-in's redirects), other links open in the default browser; cairn:// links are checked (logic.js);
 * permissions are the workspace's notifications, clipboard writes and full screen, with camera and microphone only
 * when the person allows; IPC is accepted only from the app's own pages, checked by sender.
 *
 * `start(electron, options)` takes Electron as an argument so tests can drive it with a stand-in.
 */
const fs = require('fs');
const os = require('os');
const path = require('path');

const logic = require('./logic');
const { Profiles, configDir } = require('./profiles');
const { OfflineCache, safeStorageEncryptor } = require('./offline-cache');
const policyLib = require('./policy');
const { trayModel } = require('./tray-model');
const windowState = require('./window-state');
const cliLib = require('./cli');
const { snapshotCode } = require('./snapshot');
const { menuTemplate } = require('./menu');
const { createUpdater } = require('./updater');

const BAR_HEIGHT = 44;
const RELEASES_URL = 'https://github.com/syedsumx/sca/releases';
const HELP_URL = 'https://github.com/syedsumx/sca/blob/main/cairn/docs/site/desktop.md';
const ISOLATED_WORLD = 1031;
const DEFAULT_PREFS = {
    globalShortcut: { enabled: false, accelerator: 'CommandOrControl+Shift+Space' },
    updates: true, offlineCache: true, lastActive: null, workspaces: {},
};

function start(electron, options = {}) {
    const { app, BaseWindow, WebContentsView, Menu, Tray, Notification, nativeImage, shell, ipcMain, session, dialog,
        globalShortcut, screen, safeStorage, powerMonitor } = electron;
    const env = options.env || process.env;
    const argv = options.argv || process.argv;
    const platform = options.platform || process.platform;
    const num = (name, fallback) => (Number(env[name]) > 0 ? Number(env[name]) : fallback);
    const HEALTH_MS = num('CAIRN_DESKTOP_HEALTH_MS', 30000);
    const POLL_MS = num('CAIRN_DESKTOP_POLL_MS', 60000);
    const SETTLE_MS = num('CAIRN_DESKTOP_SETTLE_MS', 1200);
    const pkg = require('../package.json');

    // Its own folder (Windows folds case: "Cairn" would be the command line's %APPDATA%\cairn).
    const oldUserData = app.getPath('userData');
    app.setPath('userData', path.join(app.getPath('appData'), 'Cairn Desktop'));
    const userData = app.getPath('userData');
    const file = (name) => path.join(userData, name);

    const state = {
        window: null, bar: null, panelView: null, tray: null, workspaces: new Map(), active: null,
        panel: null, toast: null, notifications: [], external: [], cliResults: [], ready: false,
        profilesError: null, pendingLink: null,
    };
    global.cairnDesktop = state;              // the main process only (pages never reach it); tests read it

    if (!app.requestSingleInstanceLock()) {
        app.quit();
        return state;
    }

    const readJson = (f, fallback) => {
        try {
            return JSON.parse(fs.readFileSync(f, 'utf8'));
        } catch {
            return fallback;
        }
    };
    const prefs = { ...DEFAULT_PREFS, ...readJson(file('prefs.json'), {}) };
    const savePrefs = () => {
        try {
            fs.mkdirSync(userData, { recursive: true });
            fs.writeFileSync(file('prefs.json'), JSON.stringify(prefs, null, 2), { mode: 0o600 });
        } catch { /* not worth failing for */ }
    };
    const machine = policyLib.readMachinePolicy(policyLib.machinePolicyFiles(platform, env));
    const profiles = new Profiles(configDir(env, platform));
    const encryptor = safeStorageEncryptor(safeStorage, platform);
    const cache = new OfflineCache(file('offline'), encryptor);
    state.cache = cache;
    state.machinePolicy = machine;
    state.prefs = prefs;

    // ---- the app's own pages and their requests ---------------------------------------------------------------
    const localPreload = path.join(__dirname, 'preload-local.js');
    const localPrefs = { contextIsolation: true, sandbox: true, nodeIntegration: false, preload: localPreload,
        spellcheck: false, webviewTag: false, navigateOnDragDrop: false };
    const isLocal = (event) => [state.bar, state.panelView].some((v) => v && event.sender === v.webContents)
        && String(event.senderFrame && event.senderFrame.url).startsWith('file:');
    function handle(channel, fn) {
        ipcMain.handle(channel, async (event, ...args) => {
            if (!isLocal(event)) throw new Error('not allowed');
            return fn(...args);
        });
    }

    function workspaceList() {
        return [...state.workspaces.values()].sort((a, b) => a.name.localeCompare(b.name));
    }

    function publicState() {
        const ws = state.active ? state.workspaces.get(state.active) : null;
        const limits = ws ? offlineLimits(ws) : null;
        return {
            appVersion: app.getVersion(), platform, active: state.active, panel: state.panel, toast: state.toast,
            profilesError: state.profilesError,
            workspaces: workspaceList().map((w) => ({
                name: w.name, url: w.url, origin: w.origin, status: w.status, signedIn: w.signedIn,
                unread: w.unread, inSignIn: w.inSignIn ? (logic.originOf(w.currentUrl) || '') : '',
                offlineCache: offlineLimits(w).enabled, offlineSince: w.offlineSince,
            })),
            cached: ws && (ws.status === 'offline' || state.panel && state.panel.mode === 'offline')
                ? cache.list(ws.id, limits) : [],
            cacheAvailable: cache.available(),
            prefs: { globalShortcut: prefs.globalShortcut, updates: prefs.updates, offlineCache: prefs.offlineCache },
            machine: { error: machine.error, updates: machine.updates || null, offline_cache: machine.offline_cache,
                global_shortcut: machine.global_shortcut, cli_install: machine.cli_install },
            updates: updater ? { ...updater.state, policy: updatePolicyNow() } : null,
            cli: fs.existsSync(cliPathNow()),
        };
    }

    let pushTimer = null;
    function push() {
        if (pushTimer) return;
        pushTimer = setImmediate(() => {
            pushTimer = null;
            const s = publicState();
            for (const v of [state.bar, state.panelView]) {
                if (v && !v.webContents.isDestroyed()) v.webContents.send('state', s);
            }
            updateTray();
            buildMenu();
        });
    }

    let toastTimer = null;
    function toast(text, kind = 'info') {
        state.toast = { text, kind, at: Date.now() };
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => { state.toast = null; push(); }, 8000);
        push();
    }

    // ---- workspaces ----------------------------------------------------------------------------------------------
    function makeWorkspace(name, url) {
        const origin = logic.originOf(url);
        const partition = logic.partitionFor(name, origin);
        return {
            name, url, origin, partition, id: partition.replace(/^persist:/, ''), view: null,
            ses: session.fromPartition(partition), status: 'unknown', signedIn: null, policy: null, unread: 0,
            seen: new Set(), primed: false, inSignIn: false, currentUrl: null, pendingRoute: null, offlineSince: null,
            allowedMedia: new Set(), wired: false,
        };
    }

    function offlineLimits(ws) {
        return policyLib.offlineFor(machine, ws.policy, prefs);
    }

    function allowedByComputer(origin) {
        return !Array.isArray(machine.allowed_origins) || machine.allowed_origins.includes(origin);
    }

    function syncProfiles() {
        let list;
        try {
            list = profiles.list();
            state.profilesError = null;
        } catch (e) {
            state.profilesError = e.message;
            push();
            return;
        }
        const names = new Set();
        for (const p of list) {
            let url;
            try {
                url = logic.normalizeWorkspaceUrl(p.url);
            } catch {
                continue;                                     // a profile the desktop cannot open (not https)
            }
            if (p.name.toLowerCase() === logic.CURRENT || !allowedByComputer(logic.originOf(url))) continue;
            names.add(p.name);
            const have = state.workspaces.get(p.name);
            if (have && have.url === url) continue;
            if (have) dropWorkspace(have, false);
            const ws = makeWorkspace(p.name, url);
            state.workspaces.set(p.name, ws);
            wireSession(ws);
            connect(ws, { load: false });
        }
        for (const ws of [...state.workspaces.values()]) if (!names.has(ws.name)) dropWorkspace(ws, false);
        if (state.active && !state.workspaces.has(state.active)) state.active = null;
        if (!state.active && state.workspaces.size) {
            const data = (() => { try { return profiles.load(); } catch { return {}; } })();
            const pick = [prefs.lastActive, data.default_profile].find((n) => n && state.workspaces.has(n))
                || workspaceList()[0].name;
            activate(pick);
        }
        if (!state.workspaces.size && state.window) showPanel({ mode: 'add' });
        push();
    }

    function dropWorkspace(ws, clearData) {
        if (ws.view) {
            try {
                state.window.contentView.removeChildView(ws.view);
            } catch { /* closed */ }
            ws.view.webContents.close();
            ws.view = null;
        }
        if (clearData) {
            cache.clear(ws.id);
            ws.ses.clearStorageData().catch(() => {});
        }
        state.workspaces.delete(ws.name);
    }

    function wsFetch(ws, p, init = {}) {
        const ctl = new AbortController();
        const timer = setTimeout(() => ctl.abort(), init.timeout || 10000);
        return ws.ses.fetch(`${ws.url}${p}`, { credentials: 'include', cache: 'no-store', ...init, signal: ctl.signal })
            .finally(() => clearTimeout(timer));
    }

    /** Is Cairn there, is the person signed in, and what may the app do: then show the page (or the panel). */
    async function connect(ws, { load = true, route = null } = {}) {
        const wasOffline = ws.status === 'offline';
        let health;
        try {
            health = await wsFetch(ws, '/health', { credentials: 'omit', timeout: 8000 });
        } catch {
            if (route) ws.pendingRoute = route;
            setOffline(ws, true);
            return;
        }
        let body = null;
        try {
            body = await health.json();
        } catch { /* not JSON */ }
        if (!logic.isCairnHealth(health.status, body)) {
            setOffline(ws, true, `${ws.url} does not answer as Cairn (HTTP ${health.status})`);
            return;
        }
        setOffline(ws, false);
        await refreshSignIn(ws);
        if (ws.signedIn) await refreshPolicy(ws);
        if ((load || wasOffline) && ws.view) {
            const target = route || ws.pendingRoute;
            ws.pendingRoute = null;
            const here = ws.currentUrl && logic.originOf(ws.currentUrl) === ws.origin ? ws.currentUrl : null;
            if (ws.signedIn === false) loadSignIn(ws, target);
            else if (target) ws.view.webContents.loadURL(logic.appUrl(ws.origin, target));
            else if (wasOffline || !here) ws.view.webContents.loadURL(here || logic.appUrl(ws.origin, savedRoute(ws)));
        }
        if (ws.signedIn) pollInbox(ws);
        push();
    }

    async function refreshSignIn(ws) {
        let r;
        try {
            r = await wsFetch(ws, '/me');
        } catch {
            return;
        }
        const was = ws.signedIn;
        if (r.status === 200) {
            ws.signedIn = true;
        } else if (r.status === 401 || r.status === 403) {
            ws.signedIn = false;
            ws.primed = false;
            ws.seen.clear();
            ws.unread = 0;
            if (was !== false) cache.clear(ws.id);             // signed out (here or elsewhere): nothing stays
        }
        if (was === false && ws.signedIn) refreshPolicy(ws).then(push);
    }

    async function refreshPolicy(ws) {
        try {
            const r = await wsFetch(ws, '/v1/desktop/policy');
            if (r.status === 200) ws.policy = await r.json();
            else if (r.status === 404) ws.policy = {};                 // a Cairn without these settings: defaults
        } catch { /* unknown stays unknown: no offline cache */ }
        if (ws.policy && !offlineLimits(ws).enabled) cache.clear(ws.id);
    }

    function setOffline(ws, offline, reason = null) {
        const before = ws.status;
        ws.status = offline ? 'offline' : 'online';
        ws.offlineReason = reason;
        if (offline && before !== 'offline') ws.offlineSince = Date.now();
        if (!offline) ws.offlineSince = null;
        if (ws.name === state.active) {
            if (offline) showPanel({ mode: 'offline', workspace: ws.name, reason });
            else if (state.panel && state.panel.mode === 'offline') hidePanel();
        }
        push();
    }

    function savedRoute(ws) {
        const saved = prefs.workspaces[ws.name] || {};
        return typeof saved.route === 'string' && saved.route.startsWith('/') ? saved.route : '/discover';
    }

    function loadSignIn(ws, route) {
        const back = `${logic.APP_PATH}#${route || savedRoute(ws)}`;
        ws.view.webContents.loadURL(`${ws.origin}/login?redirect_uri=${encodeURIComponent(back)}`);
    }

    function createView(ws) {
        const view = new WebContentsView({ webPreferences: {
            partition: ws.partition, contextIsolation: true, sandbox: true, nodeIntegration: false,
            preload: path.join(__dirname, 'preload-workspace.js'), spellcheck: true, webviewTag: false,
            navigateOnDragDrop: true, safeDialogs: true, additionalArguments: [`--cairn-desktop-version=${app.getVersion()}`],
        } });
        ws.view = view;
        const wc = view.webContents;
        const zoom = (prefs.workspaces[ws.name] || {}).zoom;
        if (Number.isFinite(zoom)) wc.setZoomLevel(zoom);
        wc.setWindowOpenHandler(({ url }) => {
            const d = logic.navigationDecision({ url, workspaceOrigin: ws.origin, signInOrigins: signInOrigins(ws) });
            if (d === 'allow' && logic.originOf(url) === ws.origin) wc.loadURL(url);
            else if (d === 'allow' || d === 'external') openExternal(url);
            return { action: 'deny' };
        });
        wc.on('will-navigate', (event) => guard(ws, event, false));
        wc.on('will-redirect', (event) => guard(ws, event, true));
        wc.on('did-navigate', (_e, url) => afterNavigate(ws, url));
        wc.on('did-navigate-in-page', (_e, url, isMainFrame) => { if (isMainFrame) afterNavigate(ws, url); });
        wc.on('did-fail-load', (_e, code, _desc, url, isMainFrame) => {
            if (isMainFrame && logic.isOfflineError(code)) {
                ws.pendingRoute = routeOf(ws, url) || ws.pendingRoute;
                setOffline(ws, true);
            }
        });
        wc.on('context-menu', (_e, params) => contextMenu(ws, params));
        wc.on('zoom-changed', (_e, dir) => zoomBy(ws, dir === 'in' ? 1 : -1));
        state.window.contentView.addChildView(view);
        layout();
        return view;
    }

    function routeOf(ws, url) {
        try {
            const u = new URL(url);
            if (u.origin === ws.origin && u.pathname === logic.APP_PATH && u.hash.startsWith('#/')) return u.hash.slice(1);
        } catch { /* none */ }
        return null;
    }

    function signInOrigins(ws) {
        return Array.isArray(ws.policy && ws.policy.sign_in_origins) ? ws.policy.sign_in_origins : [];
    }

    function guard(ws, event, redirect) {
        const url = event.url;
        if (event.isMainFrame === false) return;                    // a frame inside the page: the page's business
        const d = logic.navigationDecision({ url, workspaceOrigin: ws.origin, signInOrigins: signInOrigins(ws),
            inSignIn: ws.inSignIn, redirect });
        if (d === 'allow') {
            if (logic.originOf(url) !== ws.origin) ws.inSignIn = true;
            return;
        }
        event.preventDefault();
        if (d === 'external') openExternal(url);
        else if (d === 'drop') toast('Drop not supported here: open a page that takes files (such as a volume) and drop them there.', 'warning');
    }

    function afterNavigate(ws, url) {
        ws.currentUrl = url;
        if (logic.originOf(url) === ws.origin) {
            const wasSigningIn = ws.inSignIn;
            ws.inSignIn = false;
            const u = new URL(url);
            if (/^\/(logout|sign-?out)\b/i.test(u.pathname)) {
                cache.clear(ws.id);
                ws.signedIn = false;
            }
            const route = routeOf(ws, url);
            if (route) {
                prefs.workspaces[ws.name] = { ...(prefs.workspaces[ws.name] || {}), route };
                savePrefs();
                if (wasSigningIn || ws.signedIn !== true) refreshSignIn(ws).then(() => { if (ws.signedIn) { refreshPolicy(ws); pollInbox(ws); } push(); });
            }
            scheduleSnapshot(ws, url);
        }
        push();
    }

    // ---- offline: keeping what the person looked at --------------------------------------------------------------
    function scheduleSnapshot(ws, url) {
        const key = logic.offlineKey(url, ws.origin);
        clearTimeout(ws.snapshotTimer);
        if (!key) return;
        ws.snapshotTimer = setTimeout(() => snapshot(ws, key), 300);
    }

    async function snapshot(ws, key) {
        const lim = offlineLimits(ws);
        if (!lim.enabled || !ws.signedIn || ws.status !== 'online' || !cache.available()) return;
        if (ws.name !== state.active || !ws.view || state.panel) return;          // only what is on screen
        const wc = ws.view.webContents;
        let page;
        try {
            page = await wc.executeJavaScriptInIsolatedWorld(ISOLATED_WORLD, [{ code: snapshotCode(SETTLE_MS) }]);
        } catch {
            return;
        }
        // Still the same page, still on screen, still allowed: then it is kept.
        if (!page || logic.offlineKey(wc.getURL(), ws.origin) !== key || ws.name !== state.active || state.panel) return;
        if (!offlineLimits(ws).enabled || !ws.signedIn) return;
        if (cache.put(ws.id, { key, title: page.title, html: page.html }, lim)) {
            ws.lastSnapshot = { key, at: Date.now() };
        }
    }

    // ---- notifications and the unread count ----------------------------------------------------------------------
    async function pollInbox(ws) {
        if (!ws.signedIn || ws.status !== 'online') return;
        try {
            const r = await wsFetch(ws, '/notify/inbox?unread=true&limit=20');
            if (r.status === 401) {
                await refreshSignIn(ws);
                return;
            }
            if (r.status !== 200) return;
            const items = await r.json();
            if (!Array.isArray(items)) return;
            const c = await wsFetch(ws, '/notify/inbox/count');
            ws.unread = c.status === 200 ? Number((await c.json()).unread) || 0 : items.length;
            for (const n of items) {
                if (!n || typeof n.id !== 'string' || ws.seen.has(n.id)) continue;
                ws.seen.add(n.id);
                if (ws.primed) notify(ws, { title: n.title, body: n.body, url: n.action_url, source: 'inbox', id: n.id });
            }
            ws.primed = true;
        } catch { /* the next poll */ }
        push();
    }

    function notify(ws, { title, body, url, source, id }) {
        const entry = { workspace: ws.name, title: String(title || 'Cairn').slice(0, 200),
            body: String(body || '').slice(0, 1000), url: url || '', source, id: id || null, at: Date.now() };
        state.notifications.push(entry);
        if (state.notifications.length > 100) state.notifications.shift();
        if (!Notification || !Notification.isSupported || !Notification.isSupported()) return entry;
        const n = new Notification({ title: entry.title, body: entry.body, silent: false,
            icon: iconPath('icon-192.png') });
        n.on('click', () => openNotification(ws.name, entry));
        n.show();
        entry.shown = true;
        return entry;
    }

    function openNotification(name, entry) {
        const ws = state.workspaces.get(name);
        if (!ws) return;
        const target = logic.notificationTarget(entry.url, ws.origin);
        if (target.external) {
            openExternal(target.external);
            return;
        }
        showWindow();
        activate(ws.name);
        if (ws.view && ws.status === 'online') ws.view.webContents.loadURL(target.url);
        if (entry.id) wsFetch(ws, '/notify/inbox/read', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ids: [entry.id] }) }).then(() => pollInbox(ws)).catch(() => {});
    }
    state.openNotification = openNotification;

    ipcMain.on('ws:notify', (event, n) => {
        const ws = workspaceList().find((w) => w.view && w.view.webContents === event.sender);
        if (!ws || !event.senderFrame || event.senderFrame.parent !== null) return;
        if (logic.originOf(event.senderFrame.url) !== ws.origin || !n || typeof n !== 'object') return;
        notify(ws, { title: n.title, body: n.body, url: n.url, source: 'page' });
    });

    // ---- the window ----------------------------------------------------------------------------------------------
    function iconPath(name) {
        return path.join(__dirname, 'assets', name);
    }

    function createWindow() {
        const displays = screen.getAllDisplays().map((d) => d.workArea);
        const primary = screen.getPrimaryDisplay().workArea;
        const ordered = [primary, ...displays.filter((d) => d.x !== primary.x || d.y !== primary.y)];
        const bounds = windowState.restoreBounds(windowState.load(file('window-state.json')), ordered);
        const win = new BaseWindow({ ...bounds, minWidth: 480, minHeight: 400, title: 'Cairn', show: false,
            icon: iconPath('icon-512.png'), backgroundColor: '#ffffff' });
        state.window = win;
        state.bar = new WebContentsView({ webPreferences: localPrefs });
        state.bar.webContents.loadFile(path.join(__dirname, 'ui', 'bar.html'));
        hardenLocal(state.bar.webContents);
        win.contentView.addChildView(state.bar);
        state.panelView = new WebContentsView({ webPreferences: localPrefs });
        state.panelView.webContents.loadFile(path.join(__dirname, 'ui', 'panel.html'));
        hardenLocal(state.panelView.webContents);
        win.contentView.addChildView(state.panelView);
        state.panelView.setVisible(false);
        if (bounds.maximized) win.maximize();
        if (bounds.fullScreen) win.setFullScreen(true);
        win.on('resize', layout);
        let saveTimer = null;
        const remember = () => {
            clearTimeout(saveTimer);
            saveTimer = setTimeout(() => {
                if (win.isDestroyed()) return;
                const b = win.isMaximized() || win.isFullScreen() ? (state.normalBounds || win.getBounds()) : win.getBounds();
                if (!win.isMaximized() && !win.isFullScreen()) state.normalBounds = b;
                windowState.save(file('window-state.json'), { ...b, maximized: win.isMaximized(), fullScreen: win.isFullScreen() });
            }, 400);
        };
        for (const ev of ['resize', 'move', 'maximize', 'unmaximize', 'enter-full-screen', 'leave-full-screen']) win.on(ev, remember);
        win.on('close', () => {
            const b = win.getBounds();
            windowState.save(file('window-state.json'), { ...(win.isMaximized() || win.isFullScreen() ? (state.normalBounds || b) : b),
                maximized: win.isMaximized(), fullScreen: win.isFullScreen() });
        });
        win.on('closed', () => {
            state.window = null;
            state.bar = null;
            state.panelView = null;
            for (const ws of state.workspaces.values()) ws.view = null;
        });
        win.on('focus', () => {
            const ws = state.workspaces.get(state.active);
            if (ws && ws.view && !state.panel) ws.view.webContents.focus();
        });
        layout();
        win.show();
        return win;
    }

    function layout() {
        const win = state.window;
        if (!win || win.isDestroyed()) return;
        const [w, h] = win.getContentSize();
        if (state.bar) state.bar.setBounds({ x: 0, y: 0, width: w, height: BAR_HEIGHT });
        const area = { x: 0, y: BAR_HEIGHT, width: w, height: Math.max(0, h - BAR_HEIGHT) };
        for (const ws of state.workspaces.values()) {
            if (!ws.view) continue;
            ws.view.setBounds(area);
            ws.view.setVisible(ws.name === state.active && !state.panel);
        }
        if (state.panelView) {
            state.panelView.setBounds(area);
            state.panelView.setVisible(!!state.panel);
        }
    }

    function showPanel(panel) {
        state.panel = panel;
        layout();
        if (state.panelView) state.panelView.webContents.focus();
        push();
    }

    function hidePanel() {
        state.panel = null;
        layout();
        const ws = state.workspaces.get(state.active);
        if (ws && ws.view) ws.view.webContents.focus();
        push();
    }

    function activate(name, route = null) {
        const ws = state.workspaces.get(name);
        if (!ws) return false;
        state.active = name;
        prefs.lastActive = name;
        savePrefs();
        if (state.panel && state.panel.mode !== 'add') state.panel = null;
        if (state.panel && state.panel.mode === 'add' && !state.panel.prompt) state.panel = null;
        if (state.window && !ws.view) {
            createView(ws);
            ws.pendingRoute = route || ws.pendingRoute;
            connect(ws);
        } else if (route && ws.view) {
            if (ws.status === 'offline') {
                ws.pendingRoute = route;
                connect(ws);
            } else {
                ws.view.webContents.loadURL(logic.appUrl(ws.origin, route));
            }
        }
        if (ws.status === 'offline') showPanel({ mode: 'offline', workspace: name, reason: ws.offlineReason });
        layout();
        if (ws.view && !state.panel) ws.view.webContents.focus();
        push();
        return true;
    }
    state.activate = activate;

    function showWindow() {
        if (!state.window) {
            createWindow();
            if (state.active) {
                const name = state.active;
                state.active = null;
                activate(name);
            } else if (!state.workspaces.size) {
                showPanel({ mode: 'add' });
            }
            return;
        }
        if (state.window.isMinimized()) state.window.restore();
        state.window.show();
        state.window.focus();
    }

    function hardenLocal(wc) {
        wc.setWindowOpenHandler(({ url }) => {
            if (logic.externalAllowed(url)) openExternal(url);
            return { action: 'deny' };
        });
        wc.on('will-navigate', (event) => {
            event.preventDefault();
            if (logic.externalAllowed(event.url)) openExternal(event.url);
        });
    }

    function openExternal(url) {
        if (!logic.externalAllowed(url)) return;
        state.external.push(url);
        shell.openExternal(url);
    }
    state.openExternal = openExternal;

    // ---- deep links ----------------------------------------------------------------------------------------------
    function openLink(link) {
        const r = logic.resolveDeepLink(link, workspaceList().map((w) => w.name), state.active);
        if (!r) {
            if (logic.linkFromArgv([link])) toast('That cairn:// link is not one Cairn Desktop opens.', 'warning');
            return false;
        }
        if (!state.window || !state.ready) {
            state.pendingLink = link;
            return true;
        }
        showWindow();
        if (r.kind === 'open') {
            activate(r.workspace, r.route);
        } else {
            showPanel({ mode: 'add', prompt: { workspace: r.workspace, route: r.route } });
        }
        return true;
    }
    state.openLink = openLink;

    // ---- adding, signing out, removing ---------------------------------------------------------------------------
    async function addWorkspace({ url, name, route } = {}) {
        if (machine.error) throw new Error('The computer’s Cairn Desktop policy cannot be read; ask your IT team.');
        const typed = logic.normalizeWorkspaceUrl(url);
        if (!allowedByComputer(logic.originOf(typed))) throw new Error('Your organisation’s policy does not allow this Cairn here.');
        const probe = session.fromPartition('cairn-probe');            // in memory: no cookies kept
        let base = null;
        let lastError = null;
        for (const candidate of logic.apiCandidates(typed)) {
            try {
                const ctl = new AbortController();
                const t = setTimeout(() => ctl.abort(), 10000);
                const r = await probe.fetch(`${candidate}/health`, { credentials: 'omit', cache: 'no-store', signal: ctl.signal });
                clearTimeout(t);
                let body = null;
                try {
                    body = await r.json();
                } catch { /* not JSON */ }
                if (logic.isCairnHealth(r.status, body)) {
                    base = candidate;
                    break;
                }
            } catch (e) {
                lastError = e;
            }
        }
        if (!base) {
            throw new Error(lastError ? `Cannot reach ${typed}: is the address right, and are you online?`
                : `${typed} does not look like Cairn (no answer at /health or /cairn/api/health).`);
        }
        const existing = workspaceList().find((w) => w.url === base);
        if (existing && !name) {
            hidePanel();
            activate(existing.name, route || null);
            return { name: existing.name, existing: true };
        }
        const names = workspaceList().map((w) => w.name);
        const chosen = logic.checkWorkspaceName(name || suggestNameFor(base, names));
        if (names.includes(chosen) && (!existing || existing.name !== chosen)) {
            throw new Error(`There is already a workspace called ${chosen}; choose another name.`);
        }
        profiles.put(chosen, base);
        syncProfiles();
        state.panel = null;
        activate(chosen, route || null);
        return { name: chosen, url: base };
    }
    state.addWorkspace = addWorkspace;

    function suggestNameFor(base, names) {
        return logic.suggestName(base, names);
    }

    async function signOut(ws, { ask = true } = {}) {
        if (ask) {
            const r = await dialog.showMessageBox(state.window, { type: 'question', buttons: ['Sign Out', 'Cancel'],
                defaultId: 0, cancelId: 1, message: `Sign out of ${ws.name}?`,
                detail: 'The app forgets this workspace’s sign-in and the pages it kept for offline reading. The command line stays signed in (cairn logout signs it out).' });
            if (r.response !== 0) return false;
        }
        cache.clear(ws.id);
        try {
            // The site's own sign-out (Cairn Core's /logout, beside the API), with this workspace's session.
            await ws.ses.fetch(`${ws.origin}/logout`, { credentials: 'include', redirect: 'manual', cache: 'no-store',
                signal: AbortSignal.timeout(8000) });
        } catch { /* signed out here anyway */ }
        await ws.ses.clearStorageData();
        await ws.ses.clearCache();
        ws.signedIn = false;
        ws.unread = 0;
        ws.seen.clear();
        ws.primed = false;
        if (ws.view) loadSignIn(ws);
        push();
        return true;
    }
    state.signOut = signOut;

    async function removeWorkspace(ws) {
        const r = await dialog.showMessageBox(state.window, { type: 'warning', buttons: ['Remove', 'Cancel'],
            defaultId: 1, cancelId: 1, message: `Remove ${ws.name}?`,
            detail: 'It goes from Cairn Desktop and from the command line’s profiles (which signs the command line out of it). Nothing changes in Cairn itself.' });
        if (r.response !== 0) return;
        const cli = cliPathNow();
        let viaCli = false;
        if (fs.existsSync(cli)) {
            viaCli = await new Promise((resolve) => {
                const { execFile } = require('child_process');
                execFile(cli, ['profiles', 'remove', ws.name, '--quiet'], { env: cliLib.cliEnv(env), windowsHide: true,
                    timeout: 30000 }, (e) => resolve(!e));
            });
        }
        if (!viaCli) profiles.remove(ws.name);
        dropWorkspace(ws, true);
        state.active = null;
        syncProfiles();
    }

    // ---- the command line ----------------------------------------------------------------------------------------
    function cliPathNow() {
        return cliLib.cliPath({ packaged: app.isPackaged, resourcesPath: process.resourcesPath, platform, env,
            appDir: path.join(__dirname, '..') });
    }

    async function approveDevice(ws, code) {
        let r = await wsFetch(ws, `/oauth/device?code=${encodeURIComponent(code)}`);
        if (r.status !== 200) return { ok: false, error: `Cairn did not show the request (HTTP ${r.status})` };
        const described = await r.json();
        if (!cliLib.isOurDevice(described, ws.name, os.hostname())) {
            return { ok: false, error: 'the waiting sign-in is not the one this app started' };
        }
        r = await wsFetch(ws, '/oauth/device', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_code: code, decision: 'approve' }) });
        if (r.status === 200) return { ok: true };
        let detail = `HTTP ${r.status}`;
        try {
            detail = (await r.json()).detail || detail;
        } catch { /* the status */ }
        return { ok: false, error: String(detail) };
    }

    async function cliSignIn(ws) {
        if (!ws.signedIn) {
            toast(`Sign in to ${ws.name} here first; then the command line can be signed in with it.`, 'warning');
            return { ok: false, error: 'not signed in' };
        }
        toast(`Signing in the command line to ${ws.name}…`);
        const result = await cliLib.signInCommandLine({
            cli: cliPathNow(), profile: ws.name, url: ws.url, env,
            approve: (code) => approveDevice(ws, code),
            fallback: (code, why) => {
                activate(ws.name, `/device?code=${encodeURIComponent(code)}`);
                toast(`Approve the command line’s sign-in on this page (${why}).`, 'warning');
            },
        });
        state.cliResults.push({ workspace: ws.name, ...result });
        if (result.ok) {
            toast(`The command line is signed in to ${ws.name}${result.user ? ` as ${result.user.replace(/^urn:li:corpuser:/, '')}` : ''}: try \`cairn whoami --profile ${ws.name}\`.`, 'success');
        } else {
            toast(`The command line was not signed in: ${result.error}`, 'error');
        }
        return result;
    }
    state.cliSignIn = (name) => cliSignIn(state.workspaces.get(name));

    async function installCli() {
        if (machine.cli_install === false) throw new Error('Your organisation’s policy does not allow installing the command line from here.');
        const cli = cliPathNow();
        if (!fs.existsSync(cli)) throw new Error('This build has no command line inside.');
        let writable = false;
        try {
            fs.accessSync('/usr/local/bin', fs.constants.W_OK);
            writable = true;
        } catch { /* ~/.local/bin */ }
        const plan = cliLib.planInstall({ platform, cli, usrLocalWritable: writable, appImage: !!env.APPIMAGE,
            pathEnv: env.PATH || '' });
        const what = plan.method === 'user-path'
            ? `Add ${plan.dir} to your PATH (for you only), so terminals you open from now on find cairn.`
            : `${plan.method === 'copy' ? 'Copy' : 'Link'} ${plan.target} to the cairn inside Cairn Desktop.`;
        const r = await dialog.showMessageBox(state.window, { type: 'question', buttons: ['Install', 'Cancel'], defaultId: 0,
            cancelId: 1, message: 'Install the cairn command?', detail: what });
        if (r.response !== 0) return { done: false };
        const done = await cliLib.install(plan, cli);
        const note = done.onPath ? '' : ` ${plan.dir} is not on your PATH yet: add it to your shell's profile.`;
        toast(`Installed: open a new terminal and run cairn --version.${note}`, 'success');
        return done;
    }

    // ---- menus, tray, shortcut, context menu ---------------------------------------------------------------------
    function act(action, arg) {
        const ws = state.workspaces.get(state.active);
        switch (action) {
        case 'add': showWindow(); showPanel({ mode: 'add' }); break;
        case 'prefs': showWindow(); showPanel({ mode: 'prefs' }); break;
        case 'switch': showWindow(); activate(arg); break;
        case 'reload':
            if (ws) {
                if (ws.status === 'offline' || !ws.view) connect(ws);
                else ws.view.webContents.reload();
            }
            break;
        case 'zoom': if (ws) zoomBy(ws, arg); break;
        case 'back': if (ws && ws.view && ws.view.webContents.navigationHistory.canGoBack()) ws.view.webContents.navigationHistory.goBack(); break;
        case 'forward': if (ws && ws.view && ws.view.webContents.navigationHistory.canGoForward()) ws.view.webContents.navigationHistory.goForward(); break;
        case 'ask': if (ws) { showWindow(); activate(ws.name, '/assistant'); } break;
        case 'home': if (ws) activate(ws.name, '/discover'); break;
        case 'cli-sign-in': if (ws) cliSignIn(ws); break;
        case 'sign-out': if (ws) signOut(ws); break;
        case 'remove': if (ws) removeWorkspace(ws); break;
        case 'install-cli': installCli().catch((e) => toast(e.message, 'error')); break;
        case 'check-updates': updater.check({ manual: true }); break;
        case 'help': openExternal(HELP_URL); break;
        case 'devtools': if (ws && ws.view) ws.view.webContents.openDevTools({ mode: 'detach' }); break;
        case 'show': showWindow(); break;
        case 'quit': app.quit(); break;
        default: break;
        }
    }
    state.act = act;

    function zoomBy(ws, step) {
        if (!ws.view) return;
        const wc = ws.view.webContents;
        const level = step === 0 ? 0 : Math.max(-3, Math.min(4.5, wc.getZoomLevel() + step * 0.5));
        wc.setZoomLevel(level);
        prefs.workspaces[ws.name] = { ...(prefs.workspaces[ws.name] || {}), zoom: level };
        savePrefs();
    }

    function menuModel() {
        return { platform, active: state.active, appName: 'Cairn',
            workspaces: workspaceList().map((w) => ({ name: w.name, offline: w.status === 'offline' })) };
    }

    let lastMenu = '';
    function buildMenu() {
        const model = menuModel();
        const key = JSON.stringify(model);
        if (key === lastMenu) return;
        lastMenu = key;
        Menu.setApplicationMenu(Menu.buildFromTemplate(menuTemplate(model, act)));
    }

    function updateTray() {
        const model = trayModel(workspaceList().map((w) => ({ name: w.name, unread: w.unread,
            offline: w.status === 'offline', signedIn: w.signedIn })), state.active);
        state.trayModel = model;
        try {
            if (app.setBadgeCount) app.setBadgeCount(model.count);
        } catch { /* not on this desktop */ }
        if (!state.tray) return;
        state.tray.setToolTip(model.tooltip);
        if (platform === 'darwin') state.tray.setTitle(model.label);
        state.tray.setContextMenu(Menu.buildFromTemplate(model.items.map((i) => (i.type === 'separator' ? { type: 'separator' } : {
            label: i.label, type: i.type || 'normal', checked: i.checked, enabled: i.enabled !== false,
            click: () => {
                if (i.id === 'show') act('show');
                else if (i.id === 'ask') act('ask');
                else if (i.id === 'add') act('add');
                else if (i.id === 'quit') act('quit');
                else if (i.id.startsWith('switch:')) act('switch', i.id.slice('switch:'.length));
            },
        }))));
    }

    function createTray() {
        try {
            const img = nativeImage.createFromPath(iconPath(platform === 'darwin' ? 'trayTemplate.png' : 'tray.png'));
            if (platform === 'darwin') img.setTemplateImage(true);
            state.tray = new Tray(img);
            state.tray.on('click', () => showWindow());
        } catch {
            state.tray = null;                      // a desktop without a tray area
        }
        updateTray();
    }

    function applyShortcut() {
        globalShortcut.unregisterAll();
        state.shortcut = null;
        const g = prefs.globalShortcut || {};
        if (!g.enabled || machine.global_shortcut === false || !g.accelerator) return true;
        let ok = false;
        try {
            ok = globalShortcut.register(g.accelerator, () => showWindow());
        } catch {
            ok = false;
        }
        state.shortcut = ok ? g.accelerator : null;
        return ok;
    }

    function contextMenu(ws, params) {
        const wc = ws.view.webContents;
        const items = [];
        for (const s of (params.dictionarySuggestions || []).slice(0, 6)) items.push({ label: s, click: () => wc.replaceMisspelling(s) });
        if (params.misspelledWord) {
            items.push({ label: 'Add to Dictionary', click: () => ws.ses.addWordToSpellCheckerDictionary(params.misspelledWord) });
            items.push({ type: 'separator' });
        }
        if (params.linkURL && logic.externalAllowed(params.linkURL)) {
            items.push({ label: 'Open Link in Browser', click: () => openExternal(params.linkURL) });
            items.push({ label: 'Copy Link', click: () => electron.clipboard.writeText(params.linkURL) });
            items.push({ type: 'separator' });
        }
        if (params.isEditable) items.push({ role: 'cut' }, { role: 'copy' }, { role: 'paste' }, { role: 'selectAll' });
        else if (params.selectionText) items.push({ role: 'copy' });
        if (items.length) Menu.buildFromTemplate(items).popup({ window: state.window });
    }

    // ---- sessions: permissions, devices --------------------------------------------------------------------------
    function wireSession(ws) {
        if (ws.wired) return;
        ws.wired = true;
        const s = ws.ses;
        s.setPermissionRequestHandler((wc, permission, callback, details) => {
            const origin = (details && (details.requestingUrl || details.securityOrigin)) || wc.getURL();
            const d = logic.permissionDecision(permission, origin, ws.origin);
            if (d === 'allow') return callback(true);
            if (d === 'deny') return callback(false);
            const kinds = (details && details.mediaTypes) || [];
            const key = kinds.slice().sort().join('+') || 'media';
            if (ws.allowedMedia.has(key)) return callback(true);
            const what = kinds.includes('video') && kinds.includes('audio') ? 'camera and microphone'
                : kinds.includes('video') ? 'camera' : 'microphone';
            dialog.showMessageBox(state.window, { type: 'question', buttons: ['Allow', 'Don’t Allow'], defaultId: 1,
                cancelId: 1, message: `Let ${ws.name} use your ${what}?`, detail: `${ws.origin} asks for it.` })
                .then((r) => {
                    if (r.response === 0) ws.allowedMedia.add(key);
                    callback(r.response === 0);
                }, () => callback(false));
            return undefined;
        });
        s.setPermissionCheckHandler((_wc, permission, requestingOrigin) => {
            const d = logic.permissionDecision(permission, requestingOrigin, ws.origin);
            return d === 'allow' || (d === 'ask' && ws.allowedMedia.size > 0);
        });
        if (s.setDevicePermissionHandler) s.setDevicePermissionHandler(() => false);
        if (s.setDisplayMediaRequestHandler) s.setDisplayMediaRequestHandler((_req, callback) => callback({}));
        s.setSpellCheckerEnabled(true);
    }

    // ---- updates -------------------------------------------------------------------------------------------------
    function updatePolicyNow() {
        return policyLib.updatePolicy(machine, workspaceList().map((w) => ({ name: w.name, policy: w.policy })), prefs);
    }
    const updater = createUpdater({ app, shell, getPolicy: updatePolicyNow, signed: pkg.cairnSigned === true, platform,
        env, releasesUrl: RELEASES_URL,
        report: (_s, event) => {
            if (event && event.kind === 'notify') {
                const n = Notification && Notification.isSupported && Notification.isSupported()
                    ? new Notification({ title: `Cairn Desktop ${event.version} is out`, body: `${event.reason}. Click to download it.` }) : null;
                if (n) {
                    n.on('click', () => updater.openDownloads());
                    n.show();
                }
                toast(`Cairn Desktop ${event.version} is out (${event.reason}).`, 'info');
            } else if (event && event.kind === 'downloaded') {
                toast(`Cairn Desktop ${event.version} is ready: it installs when you quit, or restart now from Settings.`, 'success');
            } else if (event && event.kind === 'blocked') {
                toast(`No update check: ${event.reason}.`, 'info');
            } else if (event && event.kind === 'error') {
                toast(`The update check failed: ${event.reason}`, 'error');
            } else if (event && event.kind === 'current') {
                toast(`Cairn Desktop ${app.getVersion()} is up to date.`, 'success');
            }
            push();
        } });
    state.updater = updater;

    // ---- requests from the app's own pages -----------------------------------------------------------------------
    const str = (v, max = 2048) => (typeof v === 'string' && v.length <= max ? v : (() => { throw new Error('bad request'); })());
    handle('ws:add', (req) => addWorkspace({ url: str(req && req.url), name: req && req.name ? str(req.name, 63) : undefined,
        route: req && req.route ? logic.parseDeepLink(`cairn://open${str(req.route)}`)?.route : undefined }));
    handle('ws:switch', (name) => activate(str(name, 63)));
    handle('ws:retry', (name) => {
        const ws = state.workspaces.get(str(name, 63));
        return ws ? connect(ws).then(() => ws.status) : null;
    });
    handle('ws:ask', () => act('ask'));
    handle('bar:menu', () => {
        const items = [
            ...workspaceList().map((w) => ({ label: `${w.name}${w.unread ? ` (${w.unread})` : ''}${w.status === 'offline' ? ' — offline' : ''}`,
                type: 'radio', checked: w.name === state.active, click: () => act('switch', w.name) })),
            { type: 'separator' },
            { label: 'Add Workspace…', click: () => act('add') },
            { label: 'Sign In the Command Line Too', enabled: !!state.active, click: () => act('cli-sign-in') },
            { label: 'Sign Out of This Workspace…', enabled: !!state.active, click: () => act('sign-out') },
            { type: 'separator' },
            { label: 'Settings…', click: () => act('prefs') },
        ];
        Menu.buildFromTemplate(items).popup({ window: state.window, x: 8, y: BAR_HEIGHT - 4 });
    });
    handle('cache:page', (name, id) => {
        const ws = state.workspaces.get(str(name, 63));
        if (!ws) return null;
        const page = cache.get(ws.id, str(id, 64), offlineLimits(ws));
        return page ? { title: page.title, at: page.at, html: page.html, key: page.key } : null;
    });
    handle('panel:close', () => {
        const ws = state.workspaces.get(state.active);
        if (state.panel && state.panel.mode === 'offline') return false;
        if (!ws) return false;
        hidePanel();
        return true;
    });
    handle('panel:open', (mode) => {
        if (!['add', 'prefs'].includes(mode)) throw new Error('bad request');
        showPanel({ mode });
    });
    handle('prefs:set', (p) => {
        if (!p || typeof p !== 'object') throw new Error('bad request');
        if (p.updates !== undefined) prefs.updates = p.updates === true;
        if (p.offlineCache !== undefined) {
            prefs.offlineCache = p.offlineCache === true;
            if (!prefs.offlineCache) for (const ws of state.workspaces.values()) cache.clear(ws.id);
        }
        let shortcutOk = true;
        if (p.globalShortcut !== undefined) {
            const g = p.globalShortcut || {};
            prefs.globalShortcut = { enabled: g.enabled === true,
                accelerator: typeof g.accelerator === 'string' && /^[\w+]{1,60}$/.test(g.accelerator) ? g.accelerator
                    : prefs.globalShortcut.accelerator };
            shortcutOk = applyShortcut();
        }
        savePrefs();
        push();
        return { shortcutOk };
    });
    handle('cli:install', () => installCli());
    handle('cli:sign-in', () => {
        const ws = state.workspaces.get(state.active);
        return ws ? cliSignIn(ws) : { ok: false, error: 'no workspace' };
    });
    handle('updates:check', () => updater.check({ manual: true }).then(() => updater.state));
    handle('updates:install', () => updater.installNow());
    ipcMain.on('local:ready', (event) => {
        if (isLocal(event)) event.sender.send('state', publicState());
    });

    // ---- the app's life ------------------------------------------------------------------------------------------
    if (!options.skipProtocol && env.CAIRN_DESKTOP_NO_PROTOCOL !== '1') {
        if (process.defaultApp && process.argv.length >= 2) {
            app.setAsDefaultProtocolClient(logic.PROTOCOL, process.execPath, [path.resolve(process.argv[1])]);
        } else {
            app.setAsDefaultProtocolClient(logic.PROTOCOL);
        }
    }
    app.on('second-instance', (_event, args) => {
        const link = logic.linkFromArgv(args);
        if (!(link && openLink(link))) showWindow();
    });
    app.on('open-url', (event, link) => {               // macOS
        event.preventDefault();
        openLink(link);
    });
    app.on('web-contents-created', (_e, contents) => {
        contents.on('will-attach-webview', (ev) => ev.preventDefault());
    });
    app.on('window-all-closed', () => {
        if (platform === 'linux' || !state.tray) app.quit();         // macOS and Windows keep the tray
    });
    app.on('activate', () => showWindow());
    app.on('will-quit', () => globalShortcut.unregisterAll());

    /** Cairn Desktop 0.x kept one address in config.json; --cairn-url and CAIRN_URL still name one. */
    async function legacyAddress() {
        const flag = argv.find((a) => typeof a === 'string' && a.startsWith('--cairn-url='));
        const legacy = readJson(path.join(oldUserData, 'config.json'), {}).cairnUrl;
        const raw = flag ? flag.slice('--cairn-url='.length) : env.CAIRN_URL || legacy || null;
        if (!raw) return;
        let url;
        try {
            url = logic.normalizeWorkspaceUrl(raw);
        } catch {
            return;
        }
        const origin = logic.originOf(url);
        const have = workspaceList().find((w) => w.origin === origin);
        if (have) {
            if (flag || env.CAIRN_URL) activate(have.name);
            return;
        }
        try {
            await addWorkspace({ url });
        } catch (e) {
            toast(e.message, 'error');
        }
    }

    function seedFromPolicy() {
        if (!Array.isArray(machine.workspaces)) return;
        let data;
        try {
            data = profiles.load();
        } catch {
            return;
        }
        for (const w of machine.workspaces) {
            try {
                const name = logic.checkWorkspaceName(w.name);
                const url = logic.normalizeWorkspaceUrl(w.url);
                if (!data.profiles[name]) profiles.put(name, url);
            } catch { /* a bad entry is skipped */ }
        }
    }

    app.whenReady().then(async () => {
        if (options.appUserModelId !== false && app.setAppUserModelId) app.setAppUserModelId('com.cairn.desktop');
        await encryptor.init();
        seedFromPolicy();
        createWindow();
        createTray();
        applyShortcut();
        syncProfiles();
        fs.watchFile(profiles.file, { interval: 2000 }, () => syncProfiles());
        await legacyAddress();
        state.ready = true;
        const link = state.pendingLink || logic.linkFromArgv(argv);
        state.pendingLink = null;
        if (link) openLink(link);
        if (!state.workspaces.size) showPanel({ mode: 'add' });
        setInterval(() => {
            const ws = state.workspaces.get(state.active);
            if (ws && ws.view) connect(ws, { load: false });
        }, HEALTH_MS).unref();
        setInterval(() => { for (const ws of state.workspaces.values()) pollInbox(ws); }, POLL_MS).unref();
        if (powerMonitor) powerMonitor.on('resume', () => { for (const ws of state.workspaces.values()) connect(ws, { load: false }); });
        updater.start();
        push();
    });
    return state;
}

module.exports = { start, BAR_HEIGHT };
