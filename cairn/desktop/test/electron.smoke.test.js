'use strict';
// With Electron installed (npm install) and a display (xvfb-run on Linux): the real app opens a workspace from the
// command line's config.toml, and a cairn:// link given to a second launch opens in the first window.
// CAIRN_DESKTOP_APP names a packaged app's executable to smoke instead (the release workflow does). Skipped when
// neither Electron nor a packaged app is there (the main build never installs Electron).
// The full end-to-end test against real Cairns is tests/test_desktop_e2e.py (test/e2e/desktop.e2e.js).
//
// The page sends a notification, which must reach the main process; the app's self-test report
// (CAIRN_DESKTOP_SELF_TEST, src/app.js) says so, since a packaged app's fuses keep debuggers out. CAIRN_SMOKE_EXPECT
// adds checks the release workflow asks for where the platform has them (comma-separated):
//   tray      the tray icon exists (and its bounds are logged);
//   keychain  safeStorage is on the OS keychain: available, a round trip, and the OS's own record of the key
//             (macOS: the "Cairn Safe Storage" Keychain item; Windows: the DPAPI-wrapped key in Local State;
//             Linux: the gnome-libsecret or kwallet backend, so run it in a D-Bus session with an unlocked keyring).
// CAIRN_SMOKE_ARGS adds arguments to the app's command line (e.g. --password-store=gnome-libsecret).
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
const expect = new Set((process.env.CAIRN_SMOKE_EXPECT || '').split(',').map((x) => x.trim()).filter(Boolean));
const extraArgs = (process.env.CAIRN_SMOKE_ARGS || '').split(' ').filter(Boolean);
const skip = !electron && !packaged ? 'Electron is not installed (npm install in desktop/)'
    : process.platform === 'linux' && !process.env.DISPLAY && !xvfb ? 'needs a display or xvfb-run' : false;

test('the real app opens a workspace and follows a deep link from a second launch', { skip, timeout: 180000 }, async () => {
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
                ' + "&d=" + (window.cairnDesktop && window.cairnDesktop.desktop)); hit(); addEventListener("hashchange", hit);' +
                ' setTimeout(() => new Notification("Smoke notification", { body: "from the page" }), 500);</script>');
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
    const report = path.join(home, 'self-test.json');
    const env = { ...process.env, XDG_CONFIG_HOME: path.join(home, '.config'), HOME: home, APPDATA: path.join(home, 'AppData'),
        ELECTRON_DISABLE_SECURITY_WARNINGS: '1', CAIRN_DESKTOP_NO_PROTOCOL: '1', CAIRN_DESKTOP_SELF_TEST: report };
    // macOS finds the login keychain under the real home: keep it when the keychain is what is being checked.
    if (process.platform === 'darwin' && expect.has('keychain')) env.HOME = os.homedir();
    for (const k of Object.keys(env)) if (/^(https?|all|no)_proxy$/i.test(k)) delete env[k];   // a local Cairn
    const run = (args) => {
        // Its own profile folder on every OS: Windows takes the app-data folder from the OS, not %APPDATA%, so without
        // it a run would read (and write) the runner's real Cairn Desktop preferences.
        const argv = [...(packaged ? [] : [path.join(__dirname, '..')]), '--no-sandbox', '--no-proxy-server',
            `--user-data-dir=${path.join(home, 'user-data')}`, ...extraArgs, ...args];
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
        // The page's notification reached the main process (the app's self-test report says what it holds).
        const readReport = () => {
            try {
                return JSON.parse(fs.readFileSync(report, 'utf8'));
            } catch {
                return null;
            }
        };
        await until(() => (readReport()?.notifications.received || []).some((n) => n.source === 'page' && n.title === 'Smoke notification'));
        const r = readReport();
        console.log(`self-test: ${JSON.stringify(r)}`);
        assert.equal(r.notifications.received.find((n) => n.title === 'Smoke notification').workspace, 'smoke');
        if (expect.has('tray')) {
            assert.equal(r.tray.exists, true, JSON.stringify(r.tray));
            assert.equal(r.notifications.supported, true);
        }
        if (expect.has('keychain')) {
            assert.equal(r.safeStorage.available, true, JSON.stringify(r.safeStorage));
            assert.equal(r.safeStorage.roundTrip, true, JSON.stringify(r.safeStorage));
            if (process.platform === 'linux') {
                assert.ok(['gnome_libsecret', 'kwallet', 'kwallet5', 'kwallet6'].includes(r.safeStorage.backend), r.safeStorage.backend);
            } else if (process.platform === 'darwin') {
                const found = spawnSync('security', ['find-generic-password', '-s', 'Cairn Safe Storage'], { stdio: 'ignore' });
                assert.equal(found.status, 0, 'no "Cairn Safe Storage" item in the login keychain');
            } else if (process.platform === 'win32') {
                // Chromium keeps safeStorage's key in Local State, wrapped by DPAPI (the user's Windows credentials),
                // written a few seconds after it is made, in the profile folder the run gives it (--user-data-dir).
                const files = [path.join(home, 'user-data', 'Local State')];
                const dpapi = (f) => {
                    try {
                        const st = JSON.parse(fs.readFileSync(f, 'utf8'));
                        return Buffer.from(String(st.os_crypt && st.os_crypt.encrypted_key), 'base64').subarray(0, 5).toString() === 'DPAPI';
                    } catch {
                        return false;
                    }
                };
                await until(() => files.some(dpapi), 40000).catch(() => {
                    throw new Error(`no DPAPI-wrapped key in ${files.join(' or ')}`);
                });
            }
        }
    } finally {
        await stop(first);
        server.close();
    }
});
