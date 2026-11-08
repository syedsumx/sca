'use strict';
// Cairn Desktop's main process with a stand-in for Electron (no Electron download needed): what it opens, where its
// windows may go, deep links, permissions and the setup page's one channel.
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const logic = require('../src/logic');
const { start } = require('../src/app');

const CAIRN = 'https://cairn.example.com';

function fakeElectron({ locked = true } = {}) {
    const userData = fs.mkdtempSync(path.join(os.tmpdir(), 'cairn-desktop-'));
    const app = new EventEmitter();
    let ready;
    const readyP = new Promise((r) => { ready = r; });
    Object.assign(app, {
        quitCalled: false, protocols: [], userModelId: null,
        getPath: () => userData,
        requestSingleInstanceLock: () => locked,
        quit() { this.quitCalled = true; },
        setAsDefaultProtocolClient(p) { this.protocols.push(p); return true; },
        setAppUserModelId(id) { this.userModelId = id; },
        whenReady: () => readyP,
    });
    const windows = [];
    class BrowserWindow extends EventEmitter {
        constructor(opts) {
            super();
            this.opts = opts;
            this.loaded = [];
            this.minimized = false;
            this.focused = 0;
            const contents = new EventEmitter();
            contents.setWindowOpenHandler = (h) => { contents.openHandler = h; };
            contents.getURL = () => this.loaded.at(-1);
            this.webContents = contents;
            windows.push(this);
        }
        loadURL(u) { this.loaded.push(u); }
        loadFile(f) { this.loaded.push(`file://${f}`); }
        isMinimized() { return this.minimized; }
        restore() { this.minimized = false; }
        focus() { this.focused += 1; }
    }
    const opened = [];
    const handlers = {};
    const perms = {};
    const electron = {
        app, BrowserWindow, shell: { openExternal: (u) => opened.push(u) },
        ipcMain: { handle: (ch, fn) => { handlers[ch] = fn; } },
        session: { defaultSession: { setPermissionRequestHandler: (h) => { perms.request = h; },
                                     setPermissionCheckHandler: (h) => { perms.check = h; } } },
    };
    return { electron, app, ready, windows, opened, handlers, perms, userData };
}

const tick = () => new Promise((r) => setImmediate(r));

test('logic: addresses, deep links and navigation rules', () => {
    assert.equal(logic.normalizeCairnUrl(' https://cairn.example.com/some/page '), CAIRN);
    assert.equal(logic.normalizeCairnUrl('http://localhost:9002'), 'http://localhost:9002');
    assert.throws(() => logic.normalizeCairnUrl('http://cairn.example.com'), /https/);
    assert.throws(() => logic.normalizeCairnUrl('https://me:pw@cairn.example.com'), /password/);
    assert.throws(() => logic.normalizeCairnUrl('cairn'), /address/);
    assert.equal(logic.appUrl(CAIRN), `${CAIRN}/cairn-ui/app.html#/discover`);
    const urn = 'urn:li:dataset:(urn:li:dataPlatform:sqlite,main.orders,PROD)';
    assert.equal(logic.deepLinkTarget(`cairn://open/asset/${encodeURIComponent(urn)}`, CAIRN),
        `${CAIRN}/cairn-ui/app.html#/asset/${encodeURIComponent(urn)}`);
    assert.equal(logic.deepLinkTarget('cairn://open/search?q=orders', CAIRN), `${CAIRN}/cairn-ui/app.html#/search?q=orders`);
    for (const bad of ['cairn://evil/asset/x', 'cairn://open/../../etc', 'cairn://open/nowhere', 'https://open/asset/x',
                       'cairn://open/', 'javascript:alert(1)']) {
        assert.equal(logic.deepLinkTarget(bad, CAIRN), null, bad);
    }
    assert.equal(logic.deepLinkTarget('cairn://open/discover', null), null);
    assert.ok(logic.sameOrigin(`${CAIRN}/login`, CAIRN) && !logic.sameOrigin('https://cairn.example.com.evil.io/', CAIRN));
    assert.ok(logic.externalAllowed('https://docs.example.com') && !logic.externalAllowed('file:///etc/passwd'));
    assert.equal(logic.configuredUrl(['x', '--cairn-url=https://a.example'], { CAIRN_URL: 'https://b.example' }, {}), 'https://a.example');
    assert.equal(logic.configuredUrl([], { CAIRN_URL: 'https://b.example' }, { cairnUrl: 'https://c.example' }), 'https://b.example');
    assert.equal(logic.configuredUrl([], {}, { cairnUrl: 'https://c.example' }), 'https://c.example');
    assert.equal(logic.configuredUrl([], {}, {}), null);
});

test('opens the workspace, keeps windows on Cairn and sends other links to the browser', async () => {
    const f = fakeElectron();
    start(f.electron, { argv: ['electron', '.', `--cairn-url=${CAIRN}`], env: {}, skipProtocol: true });
    f.ready();
    await tick();
    const [win] = f.windows;
    assert.deepEqual(win.loaded, [`${CAIRN}/cairn-ui/app.html#/discover`]);
    const prefs = win.opts.webPreferences;
    assert.ok(prefs.contextIsolation && prefs.sandbox && !prefs.nodeIntegration);
    assert.equal(f.app.userModelId, 'com.cairn.desktop');           // Windows shows the app's notifications

    let prevented = false;
    win.webContents.emit('will-navigate', { preventDefault: () => { prevented = true; } }, `${CAIRN}/login?x=1`);
    assert.equal(prevented, false);
    win.webContents.emit('will-navigate', { preventDefault: () => { prevented = true; } }, 'https://elsewhere.example/');
    assert.equal(prevented, true);
    assert.deepEqual(f.opened, ['https://elsewhere.example/']);
    assert.deepEqual(win.webContents.openHandler({ url: 'file:///etc/passwd' }), { action: 'deny' });
    assert.deepEqual(f.opened, ['https://elsewhere.example/']);
    win.webContents.openHandler({ url: `${CAIRN}/cairn-ui/app.html#/sql` });
    assert.equal(win.loaded.at(-1), `${CAIRN}/cairn-ui/app.html#/sql`);

    // Notifications only for Cairn; nothing else.
    const ask = (permission, requestingUrl) => new Promise((r) => f.perms.request(win.webContents, permission, r, { requestingUrl }));
    assert.equal(await ask('notifications', `${CAIRN}/cairn-ui/app.html`), true);
    assert.equal(await ask('notifications', 'https://evil.example/'), false);
    assert.equal(await ask('media', `${CAIRN}/cairn-ui/app.html`), false);
    assert.equal(f.perms.check(win.webContents, 'notifications', CAIRN), true);
    assert.equal(f.perms.check(win.webContents, 'geolocation', CAIRN), false);

    // A deep link from a second launch (Windows, Linux) or macOS's open-url opens in the window.
    win.minimized = true;
    f.app.emit('second-instance', {}, ['cairn.exe', 'cairn://open/profile']);
    assert.equal(win.loaded.at(-1), `${CAIRN}/cairn-ui/app.html#/profile`);
    assert.ok(!win.minimized && win.focused >= 1);
    f.app.emit('open-url', { preventDefault() {} }, 'cairn://open/dashboards');
    assert.equal(win.loaded.at(-1), `${CAIRN}/cairn-ui/app.html#/dashboards`);
    f.app.emit('open-url', { preventDefault() {} }, 'cairn://open/nowhere');
    assert.equal(win.loaded.at(-1), `${CAIRN}/cairn-ui/app.html#/dashboards`);
});

test('a deep link at launch opens there; a second instance hands over and quits', async () => {
    const f = fakeElectron();
    start(f.electron, { argv: ['cairn', 'cairn://open/search?q=revenue'], env: { CAIRN_URL: CAIRN }, skipProtocol: true });
    f.ready();
    await tick();
    assert.deepEqual(f.windows[0].loaded, [`${CAIRN}/cairn-ui/app.html#/search?q=revenue`]);
    const second = fakeElectron({ locked: false });
    start(second.electron, { argv: ['cairn'], env: { CAIRN_URL: CAIRN }, skipProtocol: true });
    assert.equal(second.app.quitCalled, true);
});

test('without an address, the setup page saves one (https only) and opens Cairn', async () => {
    const f = fakeElectron();
    start(f.electron, { argv: ['cairn'], env: {}, skipProtocol: true });
    f.ready();
    await tick();
    const [win] = f.windows;
    assert.match(win.loaded[0], /^file:\/\/.*setup\.html$/);
    assert.equal(await f.perms.check(win.webContents, 'notifications', 'file://'), false);
    assert.throws(() => f.handlers['cairn:set-url']({}, 'http://cairn.example.com'), /https/);
    assert.equal(f.handlers['cairn:set-url']({}, `${CAIRN}/`), CAIRN);
    assert.equal(win.loaded.at(-1), `${CAIRN}/cairn-ui/app.html#/discover`);
    assert.deepEqual(JSON.parse(fs.readFileSync(path.join(f.userData, 'config.json'), 'utf8')), { cairnUrl: CAIRN });
    // Next launch: the saved address.
    const g = fakeElectron();
    g.electron.app.getPath = () => f.userData;
    start(g.electron, { argv: ['cairn'], env: {}, skipProtocol: true });
    g.ready();
    await tick();
    assert.deepEqual(g.windows[0].loaded, [`${CAIRN}/cairn-ui/app.html#/discover`]);
});
