'use strict';
// With Electron installed (npm install) and a display (xvfb-run on Linux): the real app opens a workspace from the
// command line's config.toml, and a cairn:// link given to a second launch opens in the first window.
// CAIRN_DESKTOP_APP names a packaged app's executable to smoke instead (the release workflow does). Skipped when
// neither Electron nor a packaged app is there (the main build never installs Electron).
// The full end-to-end test against real Cairns is tests/test_desktop_e2e.py (test/e2e/desktop.e2e.js).
const assert = require('node:assert/strict');
const { spawn, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const packaged = process.env.CAIRN_DESKTOP_APP || '';
let electron = null;
if (!packaged) {
    try {
        electron = require('electron');          // the binary's path
    } catch {
        electron = null;
    }
}
const xvfb = process.platform === 'linux' && !process.env.DISPLAY
    ? spawnSync('which', ['xvfb-run']).stdout.toString().trim() : '';
const skip = !electron && !packaged ? 'Electron is not installed (npm install in desktop/)'
    : process.platform === 'linux' && !process.env.DISPLAY && !xvfb ? 'needs a display or xvfb-run' : false;

test('the real app opens a workspace and follows a deep link from a second launch', { skip, timeout: 120000 }, async () => {
    const hits = [];
    const server = http.createServer((req, res) => {
        const u = new URL(req.url, 'http://x');
        if (process.env.CAIRN_SMOKE_DEBUG) console.error('REQ', req.url);
        const json = (body) => { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(body)); };
        if (u.pathname === '/cairn/api/health') return json({ status: 'ok', service: 'api' });
        if (u.pathname === '/cairn/api/me') return json({ urn: 'urn:li:corpuser:smoke' });
        if (u.pathname === '/cairn/api/v1/desktop/policy') return json({ offline_cache: false });
        if (u.pathname === '/cairn/api/notify/inbox') return json([]);
        if (u.pathname === '/cairn/api/notify/inbox/count') return json({ unread: 0 });
        if (u.pathname === '/cairn-ui/app.html') {
            res.setHeader('Content-Type', 'text/html');
            return res.end('<!doctype html><title>Cairn</title><script>const hit = () => fetch("/hit?h=" + encodeURIComponent(location.hash)' +
                ' + "&d=" + (window.cairnDesktop && window.cairnDesktop.desktop)); hit(); addEventListener("hashchange", hit);</script>');
        }
        if (u.pathname === '/hit') hits.push({ hash: u.searchParams.get('h'), desktop: u.searchParams.get('d') });
        res.statusCode = u.pathname === '/hit' ? 200 : 404;
        return res.end('ok');
    });
    await new Promise((r) => server.listen(0, '127.0.0.1', r));
    const url = `http://127.0.0.1:${server.address().port}`;
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'cairn-desktop-home-'));
    fs.mkdirSync(path.join(home, '.config', 'cairn'), { recursive: true });
    fs.writeFileSync(path.join(home, '.config', 'cairn', 'config.toml'),
        `default_profile = "smoke"\n\n[profiles."smoke"]\nurl = "${url}/cairn/api"\n`);
    const env = { ...process.env, XDG_CONFIG_HOME: path.join(home, '.config'), HOME: home, APPDATA: path.join(home, 'AppData'),
        ELECTRON_DISABLE_SECURITY_WARNINGS: '1', CAIRN_DESKTOP_NO_PROTOCOL: '1' };
    for (const k of Object.keys(env)) if (/^(https?|all|no)_proxy$/i.test(k)) delete env[k];   // a local Cairn
    const run = (args) => {
        const argv = [...(packaged ? [] : [path.join(__dirname, '..')]), '--no-sandbox', '--no-proxy-server', ...args];
        const exe = packaged || electron;
        // Its own process group, so the app, its helpers and the virtual display all stop at the end.
        const opts = { env, detached: process.platform !== 'win32', stdio: process.env.CAIRN_SMOKE_DEBUG ? 'inherit' : 'ignore' };
        return xvfb ? spawn(xvfb, ['-a', exe, ...argv], opts) : spawn(exe, argv, opts);
    };
    const stop = (child) => new Promise((resolve) => {
        if (child.exitCode !== null || child.signalCode !== null) return resolve();
        child.on('exit', resolve);
        try {
            if (process.platform === 'win32') child.kill();
            else process.kill(-child.pid, 'SIGTERM');
        } catch {
            resolve();
        }
        return undefined;
    });
    const until = async (fn, ms = 60000) => {
        const end = Date.now() + ms;
        while (!fn()) {
            if (Date.now() > end) throw new Error(`timed out; hits: ${JSON.stringify(hits)}`);
            await new Promise((r) => setTimeout(r, 200));
        }
    };
    const first = run([]);
    try {
        await until(() => hits.some((h) => h.hash === '#/discover'));
        assert.equal(hits.find((h) => h.hash === '#/discover').desktop, 'true');      // the page sees it runs in the app
        const second = run(['cairn://smoke/profile']);
        const secondExit = new Promise((r) => second.on('exit', r));        // listening before it can exit
        await until(() => hits.some((h) => h.hash === '#/profile'));
        await secondExit;
    } finally {
        await stop(first);
        server.close();
    }
});
