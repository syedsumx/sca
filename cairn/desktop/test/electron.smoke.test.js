'use strict';
// With Electron installed (npm install) and a display (xvfb-run on Linux): the real app opens the configured
// Cairn's workspace, and a cairn:// link given to a second launch opens in the first window.
// Skipped when Electron is not installed (the main build never installs it).
const assert = require('node:assert/strict');
const { spawn, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

let electron = null;
try {
    electron = require('electron');          // the binary's path
} catch {
    electron = null;
}
const xvfb = process.platform === 'linux' && !process.env.DISPLAY
    ? spawnSync('which', ['xvfb-run']).stdout.toString().trim() : '';
const skip = !electron ? 'Electron is not installed (npm install in desktop/)'
    : process.platform === 'linux' && !process.env.DISPLAY && !xvfb ? 'needs a display or xvfb-run' : false;

test('the real app opens Cairn and follows a deep link from a second launch', { skip, timeout: 90000 }, async () => {
    const hits = [];
    const server = http.createServer((req, res) => {
        const u = new URL(req.url, 'http://x');
        if (u.pathname === '/cairn-ui/app.html') {
            res.setHeader('Content-Type', 'text/html');
            res.end('<!doctype html><title>Cairn</title><script>fetch("/hit?h=" + encodeURIComponent(location.hash))' +
                    '.then(() => {}); addEventListener("hashchange", () => fetch("/hit?h=" + encodeURIComponent(location.hash)));</script>');
            return;
        }
        if (u.pathname === '/hit') hits.push(u.searchParams.get('h'));
        res.end('ok');
    });
    await new Promise((r) => server.listen(0, '127.0.0.1', r));
    const url = `http://127.0.0.1:${server.address().port}`;
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'cairn-desktop-home-'));
    const env = { ...process.env, XDG_CONFIG_HOME: home, HOME: home, ELECTRON_DISABLE_SECURITY_WARNINGS: '1' };
    for (const k of Object.keys(env)) if (/^(https?|all|no)_proxy$/i.test(k)) delete env[k];   // a local Cairn
    const run = (args) => {
        const argv = [path.join(__dirname, '..'), '--no-sandbox', '--no-proxy-server', `--cairn-url=${url}`, ...args];
        // Its own process group, so the app, its helpers and the virtual display all stop at the end.
        const opts = { env, detached: true, stdio: 'ignore' };
        return xvfb ? spawn(xvfb, ['-a', electron, ...argv], opts) : spawn(electron, argv, opts);
    };
    const stop = (child) => new Promise((resolve) => {
        if (child.exitCode !== null || child.signalCode !== null) return resolve();
        child.on('exit', resolve);
        try {
            process.kill(-child.pid, 'SIGTERM');
        } catch {
            resolve();
        }
    });
    const until = async (fn, ms = 40000) => {
        const end = Date.now() + ms;
        while (!fn()) {
            if (Date.now() > end) throw new Error(`timed out; hits: ${JSON.stringify(hits)}`);
            await new Promise((r) => setTimeout(r, 200));
        }
    };
    const first = run([]);
    try {
        await until(() => hits.includes('#/discover'));
        const second = run(['cairn://open/profile']);
        await until(() => hits.includes('#/profile'));
        await new Promise((r) => second.on('exit', r));
    } finally {
        await stop(first);
        server.close();
    }
});
