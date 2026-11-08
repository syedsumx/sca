'use strict';
/*
 * Cairn Desktop's main process: one window on the configured Cairn's workspace (/cairn-ui/app.html), cairn:// deep
 * links, and the OS's notifications (the workspace's web notifications and push, shown by the OS).
 *
 * `start(electron, options)` takes Electron as an argument so the tests can drive it with a stand-in.
 * Security: the page gets no Node.js (context isolation, sandbox); windows only show Cairn's origin, other links
 * open in the default browser; only Cairn's origin may ask for notifications; nothing else is permitted.
 */
const fs = require('fs');
const path = require('path');

const logic = require('./logic');

function start(electron, options = {}) {
    const { app, BrowserWindow, shell, ipcMain, session } = electron;
    const argv = options.argv || process.argv;
    const env = options.env || process.env;
    const configFile = () => path.join(app.getPath('userData'), 'config.json');
    const readSaved = () => {
        try {
            return JSON.parse(fs.readFileSync(configFile(), 'utf8'));
        } catch {
            return {};
        }
    };
    const state = { cairnUrl: null, window: null, pending: null };
    try {
        state.cairnUrl = logic.configuredUrl(argv, env, readSaved());
    } catch (e) {
        state.cairnUrl = null;            // a bad saved or given address: ask again
    }

    if (!app.requestSingleInstanceLock()) {
        app.quit();
        return state;
    }
    if (!options.skipProtocol) {
        // Development (electron .) registers with the script path; a packaged app with its own executable.
        if (process.defaultApp && process.argv.length >= 2) {
            app.setAsDefaultProtocolClient(logic.PROTOCOL, process.execPath, [path.resolve(process.argv[1])]);
        } else {
            app.setAsDefaultProtocolClient(logic.PROTOCOL);
        }
    }

    function open(link) {
        const target = logic.deepLinkTarget(link, state.cairnUrl);
        if (!target) return false;
        if (!state.window) {
            state.pending = target;
            return true;
        }
        if (state.window.isMinimized()) state.window.restore();
        state.window.loadURL(target);
        state.window.focus();
        return true;
    }

    function harden(contents) {
        contents.setWindowOpenHandler(({ url }) => {
            if (state.cairnUrl && logic.sameOrigin(url, state.cairnUrl)) {
                state.window.loadURL(url);
            } else if (logic.externalAllowed(url)) {
                shell.openExternal(url);
            }
            return { action: 'deny' };
        });
        contents.on('will-navigate', (event, url) => {
            if (state.cairnUrl && logic.sameOrigin(url, state.cairnUrl)) return;
            if (url.startsWith('file:') && !state.cairnUrl) return;         // the setup page
            event.preventDefault();
            if (logic.externalAllowed(url)) shell.openExternal(url);
        });
    }

    function createWindow() {
        state.window = new BrowserWindow({
            width: 1280, height: 860, minWidth: 360, minHeight: 480, title: 'Cairn', show: true,
            webPreferences: { contextIsolation: true, sandbox: true, nodeIntegration: false,
                              preload: path.join(__dirname, 'preload.js') },
        });
        harden(state.window.webContents);
        state.window.on('closed', () => { state.window = null; });
        if (!state.cairnUrl) {
            state.window.loadFile(path.join(__dirname, 'setup.html'));
        } else {
            state.window.loadURL(state.pending || logic.appUrl(state.cairnUrl));
            state.pending = null;
        }
    }

    // Only Cairn may ask for notifications; every other permission is refused.
    function permissions() {
        const s = session.defaultSession;
        s.setPermissionRequestHandler((contents, permission, callback, details) => {
            const origin = (details && details.requestingUrl) || contents.getURL();
            callback(permission === 'notifications' && !!state.cairnUrl && logic.sameOrigin(origin, state.cairnUrl));
        });
        s.setPermissionCheckHandler((contents, permission, requestingOrigin) =>
            permission === 'notifications' && !!state.cairnUrl && logic.sameOrigin(requestingOrigin, state.cairnUrl));
    }

    // The setup page saves the address (and nothing else).
    ipcMain.handle('cairn:set-url', (event, raw) => {
        const url = logic.normalizeCairnUrl(raw);
        fs.mkdirSync(path.dirname(configFile()), { recursive: true });
        fs.writeFileSync(configFile(), JSON.stringify({ cairnUrl: url }, null, 2));
        state.cairnUrl = url;
        if (state.window) state.window.loadURL(logic.appUrl(url));
        return url;
    });

    app.on('second-instance', (_event, args) => {
        const link = logic.linkFromArgv(args);
        if (!(link && open(link)) && state.window) {
            if (state.window.isMinimized()) state.window.restore();
            state.window.focus();
        }
    });
    app.on('open-url', (event, link) => {              // macOS
        event.preventDefault();
        open(link);
    });
    app.on('web-contents-created', (_e, contents) => harden(contents));
    app.on('window-all-closed', () => {
        if (process.platform !== 'darwin') app.quit();
    });
    app.on('activate', () => {
        if (!state.window) createWindow();
    });
    app.whenReady().then(() => {
        if (options.appUserModelId !== false && app.setAppUserModelId) app.setAppUserModelId('com.cairn.desktop');
        permissions();
        const link = logic.linkFromArgv(argv);
        if (link) open(link);
        createWindow();
    });
    state.open = open;
    return state;
}

module.exports = { start };
