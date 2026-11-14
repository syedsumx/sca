'use strict';
// The tray's state, the menus, the remembered window, and the command line's helpers (no Electron needed).
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { PassThrough } = require('node:stream');
const test = require('node:test');

const { trayModel, badge } = require('../src/tray-model');
const { menuTemplate } = require('../src/menu');
const { restoreBounds } = require('../src/window-state');
const cli = require('../src/cli');

test('tray: the unread count, its tooltip and the quick switcher', () => {
    const m = trayModel([{ name: 'alpha', unread: 3, signedIn: true }, { name: 'beta', unread: 0, offline: true },
        { name: 'gamma', signedIn: false }, { name: 'delta', unread: 120 }], 'beta');
    assert.equal(m.count, 123);
    assert.equal(m.label, '99+');
    assert.equal(m.tooltip, 'Cairn\nalpha: 3 unread\nbeta: offline\ngamma: not signed in\ndelta: 120 unread');
    const ids = m.items.map((i) => i.id || i.type);
    assert.deepEqual(ids, ['show', 'ask', 'separator', 'switch:alpha', 'switch:beta', 'switch:gamma', 'switch:delta', 'add', 'separator', 'quit']);
    assert.equal(m.items.find((i) => i.id === 'switch:beta').checked, true);
    assert.equal(m.items.find((i) => i.id === 'switch:alpha').label, 'alpha (3)');
    assert.equal(m.items.find((i) => i.id === 'switch:delta').label, 'delta (99+)');
    assert.equal(m.items.find((i) => i.id === 'switch:alpha').accelerator, 'CommandOrControl+1');
    const empty = trayModel([], null);
    assert.equal(empty.count, 0);
    assert.equal(empty.label, '');
    assert.equal(empty.items.find((i) => i.id === 'ask').enabled, false);
    assert.equal(trayModel([{ name: 'a', unread: -2 }, { name: 'b', unread: 1.5 }], 'a').count, 0);
    assert.equal(badge(0), '');
    assert.equal(badge(7), '7');
});

test('menus: standard roles and shortcuts, the workspace switcher with Ctrl/Cmd+1…9', () => {
    const acts = [];
    const ws = Array.from({ length: 10 }, (_, i) => ({ name: `w${i}` }));
    const flat = (t) => t.flatMap((m) => [m, ...(Array.isArray(m.submenu) ? flat(m.submenu) : [])]);
    for (const platform of ['darwin', 'win32', 'linux']) {
        const t = menuTemplate({ platform, workspaces: ws, active: 'w1' }, (a, arg) => acts.push([a, arg]));
        const items = flat(t);
        const roles = new Set(items.map((i) => i.role).filter(Boolean));
        for (const r of ['undo', 'redo', 'cut', 'copy', 'paste', 'selectAll', 'togglefullscreen', 'windowMenu', 'help']) assert.ok(roles.has(r), `${platform} ${r}`);
        assert.equal(roles.has('about'), true);
        assert.equal(roles.has('services'), platform === 'darwin');
        const sw = items.filter((i) => i.type === 'radio');
        assert.equal(sw.length, 10);
        assert.deepEqual(sw.slice(0, 9).map((i) => i.accelerator), Array.from({ length: 9 }, (_, i) => `CommandOrControl+${i + 1}`));
        assert.equal(sw[9].accelerator, undefined);
        assert.equal(sw.find((i) => i.checked).label, 'w1');
        sw[3].click();
        const zoomIn = items.find((i) => i.label === 'Zoom In' && i.accelerator === 'CommandOrControl+Plus');
        zoomIn.click();
        items.find((i) => i.label === 'Ask Cairn…').click();
        items.find((i) => i.label === 'Sign In the Command Line Too').click();
        assert.ok(items.find((i) => i.label === 'Install Command Line Tool…'));
        assert.equal(items.filter((i) => i.label === 'Check for Updates…').length, 1);
        assert.ok(!items.some((i) => i.label === 'Developer Tools'));
    }
    assert.deepEqual(acts.slice(0, 4), [['switch', 'w3'], ['zoom', 1], ['ask', undefined], ['cli-sign-in', undefined]]);
    const none = flat(menuTemplate({ platform: 'linux', workspaces: [], active: null }, () => {}));
    assert.equal(none.find((i) => i.label === 'Ask Cairn…').enabled, false);
});

test('the window comes back where it was, or on a screen that is still there', () => {
    const screens = [{ x: 0, y: 0, width: 1920, height: 1080 }, { x: 1920, y: 0, width: 1280, height: 1024 }];
    assert.deepEqual(restoreBounds(null, screens), { width: 1280, height: 860, maximized: false, fullScreen: false });
    assert.deepEqual(restoreBounds({ x: 2000, y: 50, width: 1000, height: 700, maximized: true }, screens),
        { x: 2000, y: 50, width: 1000, height: 700, maximized: true, fullScreen: false });
    // A monitor unplugged since: centred on the main one.
    assert.deepEqual(restoreBounds({ x: 4000, y: 100, width: 1000, height: 700 }, screens.slice(0, 1)),
        { x: 460, y: 190, width: 1000, height: 700, maximized: false, fullScreen: false });
    // Too big for any screen now: shrunk to fit; too small: the minimum.
    const big = restoreBounds({ x: 0, y: 0, width: 5000, height: 3000 }, screens.slice(0, 1));
    assert.deepEqual([big.width, big.height], [1920, 1080]);
    const small = restoreBounds({ x: 10, y: 10, width: 50, height: 50 }, screens);
    assert.deepEqual([small.width, small.height], [480, 400]);
    assert.deepEqual(restoreBounds({ x: 'a', y: 0, width: 1, height: 1 }, screens).width, 1280);
});

test('the command line: where it is, its code, its environment', () => {
    assert.equal(cli.cliPath({ packaged: true, resourcesPath: '/opt/Cairn/resources', platform: 'linux', env: { CAIRN_DESKTOP_CLI: '/tmp/evil' } }),
        path.join('/opt/Cairn/resources', 'cli', 'cairn'));
    assert.equal(cli.cliPath({ packaged: true, resourcesPath: 'C:\\Cairn\\resources', platform: 'win32', env: {} }),
        path.join('C:\\Cairn\\resources', 'cli', 'cairn.exe'));
    assert.equal(cli.cliPath({ packaged: false, platform: 'linux', env: { CAIRN_DESKTOP_CLI: '/tmp/cairn' }, appDir: '/src' }), '/tmp/cairn');
    assert.equal(cli.cliPath({ packaged: false, platform: 'linux', env: {}, appDir: '/src' }), path.join('/src', 'resources', 'cli', 'cairn'));
    const text = 'To sign in, open \u001b[1mhttps://cairn.example.com/cairn/device\u001b[0m and enter the code \u001b[1mBCDF-GHJK\u001b[0m\n';
    assert.deepEqual(cli.parseDeviceCode(text), { uri: 'https://cairn.example.com/cairn/device', code: 'BCDF-GHJK' });
    assert.equal(cli.parseDeviceCode('Waiting for you to approve it…'), null);
    assert.equal(cli.parseDeviceCode('open x and enter the code bcdf-ghjk'), null);
    assert.ok(cli.isOurDevice({ name: 'cairn CLI on host1 (Linux, profile prod)' }, 'prod', 'host1'));
    assert.ok(!cli.isOurDevice({ name: 'cairn CLI on host1 (Linux, profile prod2)' }, 'prod', 'host1'));
    assert.ok(!cli.isOurDevice({ name: 'cairn CLI on other (Linux, profile prod)' }, 'prod', 'host1'));
    assert.ok(!cli.isOurDevice({ name: 'evil' }, 'prod', 'host1') && !cli.isOurDevice(null, 'prod', 'host1'));
    const env = cli.cliEnv({ PATH: '/bin', CAIRN_URL: 'x', CAIRN_TOKEN: 'secret', CAIRN_TOKEN_FILE: 'f', CAIRN_PROFILE: 'p',
        ELECTRON_RUN_AS_NODE: '1', CAIRN_CONFIG_DIR: '/c' });
    assert.deepEqual(env, { PATH: '/bin', CAIRN_CONFIG_DIR: '/c', NO_COLOR: '1' });
});

function fakeChild({ stderr = [], stdout = '', code = 0, delay = 5 }) {
    const child = new EventEmitter();
    child.stdout = new PassThrough();
    child.stderr = new PassThrough();
    child.killed = false;
    child.kill = () => { child.killed = true; child.emit('close', null); };
    setTimeout(async () => {
        for (const line of stderr) {
            child.stderr.write(line);
            await new Promise((r) => setTimeout(r, delay));
        }
        child.stdout.end(stdout);
        await new Promise((r) => setTimeout(r, delay));
        if (!child.killed) child.emit('close', code);
    }, delay);
    return child;
}

test('"Sign in the command line too": the CLI\'s code approved, or handed to the page', async () => {
    const bin = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'cairn-cli-')), 'cairn');
    fs.writeFileSync(bin, '');
    const prompt = 'To sign in, open https://c.example/cairn/device and enter the code WXYZ-BCDF\n';
    let seen = null;
    let args = null;
    const ok = await cli.signInCommandLine({ cli: bin, profile: 'prod', url: 'https://c.example/cairn/api',
        approve: async (code) => { seen = code; return { ok: true }; },
        run: (cmd, a, opts) => {
            args = a;
            assert.equal(opts.env.CAIRN_TOKEN, undefined);
            return fakeChild({ stderr: [prompt, 'Waiting…\n'], stdout: JSON.stringify({ profile: 'prod', user: 'urn:li:corpuser:ana', credential_store: 'keyring' }, null, 2) });
        }, env: { CAIRN_TOKEN: 'x' } });
    assert.deepEqual(ok, { ok: true, user: 'urn:li:corpuser:ana', credential_store: 'keyring', profile: 'prod' });
    assert.equal(seen, 'WXYZ-BCDF');
    assert.deepEqual(args, ['login', '--url', 'https://c.example/cairn/api', '--profile', 'prod', '--no-browser', '--no-color', '--output', 'json']);
    let fell = null;
    const viaPage = await cli.signInCommandLine({ cli: bin, profile: 'prod', url: 'u', approve: async () => ({ ok: false, error: 'a passkey is needed' }),
        fallback: (code, why) => { fell = [code, why]; }, run: () => fakeChild({ stderr: [prompt], stdout: '{"user": "urn:li:corpuser:ana"}', delay: 30 }) });
    assert.deepEqual(fell, ['WXYZ-BCDF', 'a passkey is needed']);
    assert.equal(viaPage.ok, true);
    const failed = await cli.signInCommandLine({ cli: bin, profile: 'prod', url: 'u', approve: async () => ({ ok: true }),
        run: () => fakeChild({ stderr: [prompt, 'cairn: error: the sign-in was refused on the verification page\n'], code: 3 }) });
    assert.deepEqual(failed, { ok: false, error: 'cairn: error: the sign-in was refused on the verification page' });
    const slow = await cli.signInCommandLine({ cli: bin, profile: 'prod', url: 'u', approve: async () => ({ ok: true }), timeoutMs: 50,
        run: () => fakeChild({ stderr: [prompt], delay: 500 }) });
    assert.deepEqual(slow, { ok: false, error: 'the sign-in was not approved in time' });
    const missing = await cli.signInCommandLine({ cli: '/nonexistent/cairn', profile: 'p', url: 'u', approve: async () => ({ ok: true }) });
    assert.match(missing.error, /not in this build/);
});

test('"Install command line tool": where it goes, and never over someone else\'s cairn', { skip: process.platform === 'win32' }, async () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'cairn-home-'));
    const res = path.join(home, 'Cairn', 'resources', 'cli');
    fs.mkdirSync(res, { recursive: true });
    const bin = path.join(res, 'cairn');
    fs.writeFileSync(bin, '#!/bin/sh\necho cairn 1.0\n', { mode: 0o755 });
    assert.deepEqual(cli.planInstall({ platform: 'darwin', home, cli: bin, usrLocalWritable: true, pathEnv: '/usr/bin:/usr/local/bin' }),
        { method: 'symlink', dir: '/usr/local/bin', target: '/usr/local/bin/cairn', onPath: true });
    assert.deepEqual(cli.planInstall({ platform: 'win32', home, cli: 'C:\\Cairn\\resources\\cli\\cairn.exe', pathEnv: 'C:\\Windows' }),
        { method: 'user-path', dir: path.dirname('C:\\Cairn\\resources\\cli\\cairn.exe'), onPath: false });
    const plan = cli.planInstall({ platform: 'linux', home, cli: bin, usrLocalWritable: false, pathEnv: '/usr/bin' });
    assert.deepEqual(plan, { method: 'symlink', dir: path.join(home, '.local', 'bin'), target: path.join(home, '.local', 'bin', 'cairn'), onPath: false });
    await cli.install(plan, bin);
    assert.equal(fs.readlinkSync(plan.target), bin);
    await cli.install(plan, bin);                                                       // again: replaces its own link
    assert.equal(fs.readlinkSync(plan.target), bin);
    const copy = cli.planInstall({ platform: 'linux', home, cli: bin, usrLocalWritable: true, appImage: true });
    assert.equal(copy.method, 'copy');
    assert.equal(copy.dir, path.join(home, '.local', 'bin'));                            // an AppImage moves: a copy, in ~
    fs.rmSync(plan.target);
    await cli.install(copy, bin);
    assert.equal(fs.readFileSync(copy.target, 'utf8'), '#!/bin/sh\necho cairn 1.0\n');
    await cli.install(copy, bin);
    fs.rmSync(copy.target);
    fs.rmSync(`${copy.target}.cairn-desktop`);
    fs.writeFileSync(plan.target, 'pip-installed cairn');                               // someone else's: left alone
    await assert.rejects(cli.install(plan, bin), /not installed by Cairn Desktop/);
    assert.equal(fs.readFileSync(plan.target, 'utf8'), 'pip-installed cairn');
    let ps = null;
    await cli.install({ method: 'user-path', dir: 'C:\\Cairn\\cli' }, 'x', { exec: (cmd, a, opts, cb) => { ps = [cmd, a, opts.env.CAIRN_CLI_DIR]; cb(null); } });
    assert.equal(ps[0], 'powershell.exe');
    assert.equal(ps[2], 'C:\\Cairn\\cli');
    assert.ok(!ps[1].join(' ').includes('C:\\Cairn\\cli'));                            // the folder never in the command
});
