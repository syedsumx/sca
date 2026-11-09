'use strict';
/*
 * Cairn Desktop end to end: the real Electron app (Playwright's Electron support) against real Cairns. Started by
 * tests/test_desktop_e2e.py, which runs two Cairns, a D-Bus session with an unlocked GNOME Keyring and a virtual
 * display, and passes the addresses in CAIRN_E2E_*. Skipped without them.
 */
const assert = require('node:assert/strict');
const { spawn, execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const E = process.env;
const DESKTOP = path.join(__dirname, '..', '..');
const skip = !E.CAIRN_E2E_ALPHA ? 'run by tests/test_desktop_e2e.py' : false;

const ALPHA = E.CAIRN_E2E_ALPHA;
const BETA = E.CAIRN_E2E_BETA;
const BOARD = E.CAIRN_E2E_BOARD;
const CLI = E.CAIRN_E2E_CLI;
const CONFIG = path.join(E.XDG_CONFIG_HOME || '', 'cairn', 'config.toml');
const USERDATA = path.join(E.XDG_CONFIG_HOME || '', 'Cairn Desktop');
const ARGS = [DESKTOP, '--no-sandbox', '--password-store=gnome-libsecret'];
const ENV = { ...E, CAIRN_DESKTOP_HEALTH_MS: '1500', CAIRN_DESKTOP_POLL_MS: '1500', CAIRN_DESKTOP_SETTLE_MS: '700',
    CAIRN_DESKTOP_CLI: CLI, CAIRN_DESKTOP_NO_PROTOCOL: '1', ELECTRON_DISABLE_SECURITY_WARNINGS: '1' };

async function until(fn, what, ms = 30000) {
    const end = Date.now() + ms;
    let last;
    for (;;) {
        try {
            last = await fn();
            if (last) return last;
        } catch (e) {
            last = e;
        }
        if (Date.now() > end) throw new Error(`timed out waiting for ${what} (last: ${last && last.message ? last.message : JSON.stringify(last)})`);
        await new Promise((r) => setTimeout(r, 200));
    }
}

const post = (url, body) => fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    .then((r) => r.json());

test('Cairn Desktop against real Cairns', { skip, timeout: 420000 }, async (t) => {
    const { _electron } = require('playwright-core');
    const electronPath = require('electron');
    const app = await _electron.launch({ executablePath: electronPath, args: ARGS, env: ENV, timeout: 60000 });
    const main = (fn, arg) => app.evaluate(fn, arg);
    const pageOf = (pred, what) => until(() => app.windows().find((p) => { try { return pred(p.url()); } catch { return false; } }), what);
    const desktopState = () => main(() => {
        const s = global.cairnDesktop;
        return { active: s.active, panel: s.panel, toast: s.toast, external: s.external,
            notifications: s.notifications.map((n) => ({ workspace: n.workspace, title: n.title, source: n.source, url: n.url })),
            tray: s.trayModel ? { count: s.trayModel.count, tooltip: s.trayModel.tooltip } : null,
            workspaces: [...s.workspaces.values()].map((w) => ({ name: w.name, url: w.url, origin: w.origin, status: w.status,
                signedIn: w.signedIn, partition: w.partition, id: w.id, unread: w.unread, lastSnapshot: w.lastSnapshot || null,
                currentUrl: w.currentUrl, policy: w.policy })) };
    });
    const ws = async (name) => (await desktopState()).workspaces.find((w) => w.name === name);
    try {
        const panel = await pageOf((u) => u.endsWith('/ui/panel.html'), 'the panel');
        await pageOf((u) => u.endsWith('/ui/bar.html'), 'the top bar');

        await t.test('adding a workspace (and the CLI sees it)', async () => {
            await until(async () => (await desktopState()).panel?.mode === 'add', 'the welcome panel');
            await panel.fill('#url', 'http://127.0.0.1:1');                // nothing there: refused with a reason
            await panel.click('#add-submit');
            await until(async () => /Cannot reach/.test(await panel.textContent('#add-error')), 'the error');
            await panel.fill('#url', 'http://cairn.example.com');          // not https
            await panel.click('#add-submit');
            await until(async () => /https/.test(await panel.textContent('#add-error')), 'the https rule');
            await panel.fill('#url', ALPHA);
            await panel.fill('#name', 'alpha');
            await panel.click('#add-submit');
            await until(async () => (await desktopState()).active === 'alpha', 'alpha active');
            const config = fs.readFileSync(CONFIG, 'utf8');
            assert.match(config, /\[profiles\."alpha"\]\nurl = "http:\/\/127\.0\.0\.1:\d+\/cairn\/api"/);
            assert.match(config, /default_profile = "alpha"/);
            const listed = JSON.parse(execFileSync(CLI, ['profiles', 'list', '--output', 'json'], { env: E }).toString());
            assert.deepEqual(listed.profiles.map((p) => p.name), ['alpha']);
        });

        // The web UI asks a person's role on their first visit to its home page (L128): "Not now" answers it, so
        // the dialog doesn't sit over what follows.
        async function answerRoleQuestion(page) {
            const skip = page.getByRole('button', { name: 'Not now' });
            try {
                await skip.waitFor({ state: 'visible', timeout: 5000 });
                await skip.click();
                await skip.waitFor({ state: 'hidden', timeout: 5000 });
            } catch { /* not asked: onboarding off, or already answered */ }
        }

        let alpha;
        await t.test('signing in (the web UI\'s own sign-in, in the app\'s window)', async () => {
            alpha = await pageOf((u) => u.startsWith(`${ALPHA}/login`), 'alpha\'s sign-in page');
            await alpha.waitForSelector('input[name=username]');
            await alpha.fill('input[name=username]', 'ana');
            await alpha.click('button[type=submit]');
            await alpha.waitForURL((u) => u.href.includes('/cairn-ui/app.html'), { timeout: 30000 });
            await alpha.waitForSelector('nav[aria-label]', { timeout: 30000 });
            await answerRoleQuestion(alpha);
            await until(async () => (await ws('alpha')).signedIn === true, 'alpha signed in');
            const me = await alpha.evaluate(() => fetch('/cairn/api/me').then((r) => r.json()));
            assert.equal(me.urn, 'urn:li:corpuser:ana');
            assert.equal(await alpha.evaluate(() => window.cairnDesktop && window.cairnDesktop.desktop), true);
            assert.equal(await alpha.evaluate(() => typeof window.require + typeof window.process), 'undefinedundefined');
            await until(async () => (await ws('alpha')).policy && (await ws('alpha')).policy.offline_cache === true, 'alpha\'s desktop policy');
        });

        let beta;
        await t.test('two workspaces with separate sessions, and switching', async () => {
            await main(() => global.cairnDesktop.act('add'));
            await until(async () => (await desktopState()).panel?.mode === 'add', 'the add panel');
            await panel.fill('#url', `${BETA}/cairn-ui/app.html`);
            await panel.fill('#name', 'beta');
            await panel.click('#add-submit');
            beta = await pageOf((u) => u.startsWith(`${BETA}/login`), 'beta\'s sign-in page');
            await beta.waitForSelector('input[name=username]');
            await beta.fill('input[name=username]', 'bob');
            await beta.click('button[type=submit]');
            await beta.waitForSelector('nav[aria-label]', { timeout: 30000 });
            await answerRoleQuestion(beta);
            assert.equal((await beta.evaluate(() => fetch('/cairn/api/me').then((r) => r.json()))).urn, 'urn:li:corpuser:bob');
            const jars = await main(async ({ session }) => {
                const s = global.cairnDesktop;
                const out = {};
                for (const name of ['alpha', 'beta']) {
                    const cookies = await session.fromPartition(s.workspaces.get(name).partition).cookies.get({});
                    out[name] = cookies.map((c) => `${c.domain}|${c.name}|${c.value}`);
                }
                return out;
            });
            const a = await ws('alpha');
            const b = await ws('beta');
            assert.notEqual(a.partition, b.partition);
            assert.ok(jars.alpha.some((c) => c.startsWith('127.0.0.1|PLAY_SESSION|')), JSON.stringify(jars));
            assert.ok(jars.beta.some((c) => c.startsWith('localhost|PLAY_SESSION|')), JSON.stringify(jars));
            assert.ok(!jars.alpha.some((c) => c.startsWith('localhost|')) && !jars.beta.some((c) => c.startsWith('127.0.0.1|')));
            assert.ok(!jars.alpha.some((c) => jars.beta.includes(c)));
            await main(() => global.cairnDesktop.act('switch', 'alpha'));
            assert.equal((await desktopState()).active, 'alpha');
            assert.equal((await alpha.evaluate(() => fetch('/cairn/api/me').then((r) => r.json()))).urn, 'urn:li:corpuser:ana');
            const listed = JSON.parse(execFileSync(CLI, ['profiles', 'list', '--output', 'json'], { env: E }).toString());
            assert.deepEqual(listed.profiles.map((p) => p.name), ['alpha', 'beta']);
        });

        await t.test('a deep link opens the page; an unknown workspace asks to add it', async () => {
            const second = spawn(electronPath, [...ARGS, 'cairn://beta/profile'], { env: ENV, stdio: 'ignore' });
            await new Promise((r) => second.on('exit', r));            // the running app takes the link
            await until(async () => (await desktopState()).active === 'beta', 'beta active');
            await beta.waitForURL((u) => u.hash === '#/profile', { timeout: 20000 });
            const third = spawn(electronPath, [...ARGS, 'cairn://gamma/dashboards?open=1'], { env: ENV, stdio: 'ignore' });
            await new Promise((r) => third.on('exit', r));
            const s = await until(async () => { const st = await desktopState(); return st.panel?.prompt ? st : null; }, 'the add prompt');
            assert.deepEqual(s.panel, { mode: 'add', prompt: { workspace: 'gamma', route: '/dashboards?open=1' } });
            assert.match(await panel.textContent('#add-title'), /gamma/);
            await panel.click('#add-cancel');
            await until(async () => !(await desktopState()).panel, 'the panel closed');
            // Links that are not Cairn's shape open nothing.
            assert.equal(await main(() => global.cairnDesktop.openLink('cairn://beta/../../etc/passwd')), false);
            assert.equal(await main(() => global.cairnDesktop.openLink('cairn://beta/nowhere')), false);
            await main(() => global.cairnDesktop.act('switch', 'alpha'));
        });

        await t.test('notifications: from the page, and from the inbox, reach the main process', async () => {
            await alpha.evaluate(() => { new Notification('Build finished L131', { body: 'orders refreshed', data: { url: '#/sql' } }); });
            await until(async () => (await desktopState()).notifications.find((n) => n.source === 'page' && n.title === 'Build finished L131'),
                'the page\'s notification');
            await post(`${ALPHA}/__test/notify`, { to: 'urn:li:corpuser:ana', title: 'Ana was mentioned L131', body: 'on Regional sales',
                url: `${ALPHA}/cairn/dashboards?open=${BOARD}` });
            const s = await until(async () => {
                const st = await desktopState();
                return st.notifications.find((n) => n.source === 'inbox' && n.title === 'Ana was mentioned L131') ? st : null;
            }, 'the inbox notification', 20000);
            assert.ok(s.tray.count >= 1 && /alpha: \d+ unread/.test(s.tray.tooltip), JSON.stringify(s.tray));
            // Clicking it opens that dashboard in the workspace (the classic /cairn/... link mapped to the app).
            await main(() => {
                const st = global.cairnDesktop;
                st.openNotification('alpha', st.notifications.find((n) => n.source === 'inbox'));
            });
            await alpha.waitForURL((u) => u.hash === `#/dashboards?open=${BOARD}`, { timeout: 20000 });
        });

        await t.test('a link to another site opens in the browser, not in the app', async () => {
            const stubbed = await main(({ shell }) => {
                global.__opened = [];
                shell.openExternal = async (u) => { global.__opened.push(u); };
                return true;
            });
            assert.ok(stubbed);
            const before = alpha.url();
            await alpha.evaluate(() => { window.location.href = 'https://example.org/elsewhere'; });
            await alpha.evaluate(() => { window.open('https://example.com/popup', '_blank'); });
            const opened = await until(async () => {
                const o = await main(() => global.__opened);
                return o.length >= 2 ? o : null;
            }, 'the external opens');
            assert.deepEqual(opened, ['https://example.org/elsewhere', 'https://example.com/popup']);
            assert.equal(alpha.url(), before);
            assert.equal(app.windows().filter((p) => p.url().includes('example.')).length, 0);
        });

        await t.test('"Sign in the command line too": cairn whoami works as the person', async () => {
            const r = await main(() => global.cairnDesktop.cliSignIn('alpha'));
            assert.equal(r.ok, true, JSON.stringify(r));
            assert.equal(r.user, 'urn:li:corpuser:ana');
            const who = JSON.parse(execFileSync(CLI, ['whoami', '--profile', 'alpha', '--output', 'json'], { env: E }).toString());
            assert.equal(who.user, 'urn:li:corpuser:ana');
            assert.equal(who.via, 'cli');
            assert.equal(who.credential_store, 'keyring');               // the OS keychain (GNOME Keyring here)
            assert.ok(!fs.existsSync(path.join(path.dirname(CONFIG), 'credentials.json')));
        });

        await t.test('offline: a dashboard viewed is kept encrypted and shown read-only; sign-out clears it', async () => {
            assert.equal((await desktopState()).active, 'alpha');
            assert.ok(alpha.url().endsWith(`#/dashboards?open=${BOARD}`));
            // (A navigation the app refused earlier leaves Playwright waiting on it: read the page instead.)
            await until(() => alpha.evaluate(() => document.body.innerText.includes('Regional sales L131')
                && document.body.innerText.includes('US')), 'the dashboard rendered');
            const snap = await until(async () => (await ws('alpha')).lastSnapshot, 'the snapshot', 40000);
            assert.equal(snap.key, `/dashboards?open=${BOARD}`);
            const a = await ws('alpha');
            const dir = path.join(USERDATA, 'offline', a.id);
            const files = fs.readdirSync(dir);
            assert.ok(files.includes('index.bin') && files.length >= 2, files.join(','));
            for (const f of files) {
                const raw = fs.readFileSync(path.join(dir, f));
                assert.ok(!raw.includes('Regional sales') && !raw.includes('Orders by region'), `${f} is plaintext`);
            }
            // The network goes: Cairn stops answering.
            await post(`${ALPHA}/__test/stop`, {});
            await until(async () => (await desktopState()).panel?.mode === 'offline', 'the offline panel', 30000);
            await panel.waitForSelector('#cached li button', { timeout: 20000 });
            assert.match(await panel.textContent('#offline-title'), /alpha can’t be reached/);
            assert.match(await panel.textContent('#cached'), /as of/);
            await panel.click('#cached li button');
            await panel.waitForSelector('#viewer:not([hidden])');
            assert.match(await panel.textContent('#viewer-asof'), /Read-only · as of/);
            assert.equal(await panel.getAttribute('#frame', 'sandbox'), '');
            const frame = panel.frameLocator('#frame');
            await frame.locator('text=Regional sales L131').first().waitFor({ timeout: 10000 });
            const html = await panel.evaluate(() => document.getElementById('frame').srcdoc);
            assert.ok(!/<script/i.test(html) && !/\shref=/i.test(html) && !/\son[a-z]+=/i.test(html));
            assert.match(html, /EU/);                                   // the tile's rows, as rendered
            if (E.CAIRN_E2E_SHOTS) {
                await panel.screenshot({ path: path.join(E.CAIRN_E2E_SHOTS, 'offline-viewer.png') });
                await panel.click('#viewer-close');
                await panel.screenshot({ path: path.join(E.CAIRN_E2E_SHOTS, 'offline-list.png') });
                const bar = app.windows().find((p) => p.url().endsWith('/ui/bar.html'));
                await bar.screenshot({ path: path.join(E.CAIRN_E2E_SHOTS, 'bar.png') });
            } else {
                await panel.click('#viewer-close');
            }
            // Signing out forgets the session and the kept pages.
            await main(() => global.cairnDesktop.signOut(global.cairnDesktop.workspaces.get('alpha'), { ask: false }));
            assert.ok(!fs.existsSync(dir));
            const jar = await main(async ({ session }) => (await session.fromPartition(global.cairnDesktop.workspaces.get('alpha').partition).cookies.get({})).length);
            assert.equal(jar, 0);
        });

        await t.test('a workspace administrator turns the offline cache off', async () => {
            await main(() => global.cairnDesktop.act('switch', 'beta'));
            const put = await beta.evaluate(() => fetch('/cairn/api/admin/desktop/policy', { method: 'PUT',
                headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ offline_cache: false }) }).then((r) => r.status));
            assert.equal(put, 200);
            await until(async () => (await ws('beta')).policy?.offline_cache === false, 'beta\'s policy refreshed', 20000);
            await beta.evaluate((id) => { window.location.hash = `#/dashboards?open=${id}`; }, BOARD);
            await beta.waitForSelector('text=Regional sales L131', { timeout: 30000 });
            await new Promise((r) => setTimeout(r, 3000));
            assert.equal((await ws('beta')).lastSnapshot, null);
            assert.ok(!fs.existsSync(path.join(USERDATA, 'offline', (await ws('beta')).id)));
        });

        await t.test('the window\'s place is remembered', async () => {
            await main(() => global.cairnDesktop.window.setBounds({ x: 40, y: 30, width: 1000, height: 700 }));
            const saved = await until(() => {
                try {
                    const s = JSON.parse(fs.readFileSync(path.join(USERDATA, 'window-state.json'), 'utf8'));
                    return s.width === 1000 && s.height === 700 ? s : null;
                } catch {
                    return null;
                }
            }, 'window-state.json');
            assert.equal(saved.maximized, false);
        });
    } finally {
        await app.close().catch(() => {});
    }
});
