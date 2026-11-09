'use strict';
// Cairn Desktop's rules (logic.js): addresses, deep links, the navigation allowlist, permissions, notifications.
const assert = require('node:assert/strict');
const test = require('node:test');

const logic = require('../src/logic');

const O = 'https://cairn.example.com';

test('workspace addresses: https, loopback http, no credentials, the API found as the CLI finds it', () => {
    assert.equal(logic.normalizeWorkspaceUrl(' https://cairn.example.com/ '), O);
    assert.equal(logic.normalizeWorkspaceUrl('https://cairn.example.com/cairn-ui/app.html#/discover'), O);
    assert.equal(logic.normalizeWorkspaceUrl('https://cairn.example.com/cairn/api/health'), `${O}/cairn/api`);
    assert.equal(logic.normalizeWorkspaceUrl('http://localhost:9002'), 'http://localhost:9002');
    assert.equal(logic.normalizeWorkspaceUrl('http://127.0.0.1:9002/x?y=1'), 'http://127.0.0.1:9002/x');
    assert.throws(() => logic.normalizeWorkspaceUrl('http://cairn.example.com'), /https/);
    assert.throws(() => logic.normalizeWorkspaceUrl('http://localhost.evil.io'), /https/);
    assert.throws(() => logic.normalizeWorkspaceUrl('https://me:pw@cairn.example.com'), /password/);
    assert.throws(() => logic.normalizeWorkspaceUrl('ftp://cairn.example.com'), /https/);
    assert.throws(() => logic.normalizeWorkspaceUrl('cairn'), /address/);
    assert.deepEqual(logic.apiCandidates('https://cairn.example.com'), [O, `${O}/cairn/api`]);
    assert.deepEqual(logic.apiCandidates(`${O}/cairn/api`), [`${O}/cairn/api`]);
    assert.ok(logic.isCairnHealth(200, { status: 'ok', service: 'api' }));
    assert.ok(!logic.isCairnHealth(200, { status: 'ok' }) && !logic.isCairnHealth(503, { service: 'api' }) && !logic.isCairnHealth(200, null));
});

test('workspace names: the CLI profile rule, "open" kept, suggestions unique', () => {
    assert.equal(logic.checkWorkspaceName('prod-eu.1'), 'prod-eu.1');
    for (const bad of ['', '-x', 'a b', 'a/b', 'x'.repeat(64), 'open', 'OPEN']) assert.throws(() => logic.checkWorkspaceName(bad), bad);
    assert.equal(logic.suggestName('https://cairn.acme.com/cairn/api', []), 'cairn');
    assert.equal(logic.suggestName('https://cairn.acme.com', ['cairn', 'Cairn-2']), 'cairn-3');
    assert.equal(logic.suggestName('http://localhost:9002', []), 'local');
    assert.equal(logic.suggestName('https://open.example.com', []), 'cairn-open');
    const a = logic.partitionFor('prod', O);
    assert.match(a, /^persist:cairn-ws-[0-9a-f]{24}$/);
    assert.notEqual(a, logic.partitionFor('prod', 'https://other.example.com'));
    assert.notEqual(a, logic.partitionFor('dev', O));
    assert.equal(a, logic.partitionFor('prod', O));
});

test('deep links: cairn://<workspace>/<page> is checked before anything opens', () => {
    const urn = 'urn:li:dataset:(urn:li:dataPlatform:s3,bucket/path,PROD)';
    assert.deepEqual(logic.parseDeepLink('cairn://prod/dashboards?open=d1'), { workspace: 'prod', route: '/dashboards?open=d1' });
    assert.deepEqual(logic.parseDeepLink(`cairn://prod/asset/${encodeURIComponent(urn)}`),
        { workspace: 'prod', route: `/asset/${encodeURIComponent(urn)}` });
    assert.deepEqual(logic.parseDeepLink('cairn://open/search?q=orders'), { workspace: null, route: '/search?q=orders' });
    assert.deepEqual(logic.parseDeepLink('CAIRN://Prod/profile'), { workspace: 'Prod', route: '/profile' });
    for (const bad of ['cairn://prod/nowhere', 'cairn://prod/', 'cairn://prod', 'cairn://prod/../../etc/passwd',
        'cairn://prod/asset/%2e%2e', 'cairn://prod/asset/..', 'cairn://u:p@prod/discover', 'cairn://prod:80/discover',
        'cairn://pr od/discover', 'cairn://prod/discover#x', 'cairn://-x/discover', 'https://prod/discover',
        'javascript:alert(1)', 'cairn://prod/discover\n', `cairn://prod/search?q=${'x'.repeat(2100)}`, 42, null,
        'cairn://prod/asset/%E0%A4%A', 'cairn://pro%2Fd/discover']) {
        assert.equal(logic.parseDeepLink(bad), null, String(bad).slice(0, 60));
    }
    const names = ['alpha', 'Beta'];
    assert.deepEqual(logic.resolveDeepLink('cairn://alpha/sql', names, 'Beta'), { kind: 'open', workspace: 'alpha', route: '/sql' });
    assert.deepEqual(logic.resolveDeepLink('cairn://beta/sql', names, 'alpha'), { kind: 'open', workspace: 'Beta', route: '/sql' });
    assert.deepEqual(logic.resolveDeepLink('cairn://open/sql', names, 'Beta'), { kind: 'open', workspace: 'Beta', route: '/sql' });
    assert.equal(logic.resolveDeepLink('cairn://open/sql', [], null), null);
    assert.deepEqual(logic.resolveDeepLink('cairn://gamma/reports', names, 'alpha'), { kind: 'unknown', workspace: 'gamma', route: '/reports' });
    assert.equal(logic.findWorkspace('x', ['X', 'x']), 'x');
    assert.equal(logic.findWorkspace('X1', ['x1', 'X1']), 'X1');
    assert.equal(logic.findWorkspace('ab', ['AB', 'Ab']), null);                 // ambiguous ignoring case: nothing
    assert.equal(logic.linkFromArgv(['/app', '--flag', 'cairn://alpha/sql']), 'cairn://alpha/sql');
    assert.equal(logic.linkFromArgv(['/app']), null);
});

test('the navigation allowlist: the workspace, its sign-in, everything else in the browser', () => {
    const nav = (url, extra = {}) => logic.navigationDecision({ url, workspaceOrigin: O, ...extra });
    assert.equal(nav(`${O}/cairn-ui/app.html#/sql`), 'allow');
    assert.equal(nav(`${O}/login?redirect_uri=x`), 'allow');
    assert.equal(nav('https://cairn.example.com.evil.io/'), 'external');
    assert.equal(nav('https://docs.example.org/page'), 'external');
    assert.equal(nav('mailto:data@example.com'), 'external');
    assert.equal(nav('http://cairn.example.com/'), 'external');                       // another origin (http)
    assert.equal(nav('javascript:alert(1)'), 'deny');
    assert.equal(nav('data:text/html,<script>1</script>'), 'deny');
    assert.equal(nav('file:///home/me/report.csv'), 'drop');                          // a file dropped on the page
    assert.equal(nav('https://u:p@cairn.example.com/'), 'deny');
    assert.equal(nav('not a url'), 'deny');
    // Sign-in: a redirect from the workspace to the identity provider is followed, and its pages are shown.
    assert.equal(nav('https://login.idp.example/authorize?x', { redirect: true }), 'allow');
    assert.equal(nav('http://login.idp.example/authorize?x', { redirect: true }), 'external');   // never plain http
    assert.equal(nav('https://login.idp.example/mfa', { inSignIn: true }), 'allow');
    assert.equal(nav('https://login.idp.example/help', { signInOrigins: ['https://login.idp.example'] }), 'allow');
    assert.equal(nav('https://login.idp.example/help'), 'external');
    assert.ok(logic.externalAllowed('https://x.example') && !logic.externalAllowed('file:///etc/passwd')
        && !logic.externalAllowed('smb://host/share'));
});

test('permissions: notifications for the workspace, camera and microphone asked, the rest refused', () => {
    assert.equal(logic.permissionDecision('notifications', `${O}/cairn-ui/app.html`, O), 'allow');
    assert.equal(logic.permissionDecision('clipboard-sanitized-write', O, O), 'allow');
    assert.equal(logic.permissionDecision('media', `${O}/x`, O), 'ask');
    for (const p of ['geolocation', 'midi', 'midiSysex', 'pointerLock', 'openExternal', 'hid', 'serial', 'usb', 'display-capture',
        'clipboard-read', 'idle-detection', 'unknown']) {
        assert.equal(logic.permissionDecision(p, O, O), 'deny', p);
    }
    assert.equal(logic.permissionDecision('notifications', 'https://evil.example', O), 'deny');
    assert.equal(logic.permissionDecision('media', 'https://evil.example', O), 'deny');
    assert.equal(logic.permissionDecision('notifications', O, null), 'deny');
    assert.equal(logic.permissionDecision('notifications', '', O), 'deny');
});

test('notification targets and the pages kept offline', () => {
    assert.deepEqual(logic.notificationTarget(`${O}/cairn/dashboards?open=d1`, O), { url: `${O}/cairn-ui/app.html#/dashboards?open=d1` });
    assert.deepEqual(logic.notificationTarget(`${O}/cairn-ui/app.html#/sql`, O), { url: `${O}/cairn-ui/app.html#/sql` });
    assert.deepEqual(logic.notificationTarget('#/requests', O), { url: `${O}/cairn-ui/app.html#/requests` });
    assert.deepEqual(logic.notificationTarget('https://status.example.org/x', O), { external: 'https://status.example.org/x' });
    assert.deepEqual(logic.notificationTarget('javascript:alert(1)', O), { url: `${O}/cairn-ui/app.html#/settings` });
    assert.deepEqual(logic.notificationTarget(null, O), { url: `${O}/cairn-ui/app.html#/settings` });
    assert.equal(logic.offlineKey(`${O}/cairn-ui/app.html#/dashboards?open=d1`, O), '/dashboards?open=d1');
    assert.equal(logic.offlineKey(`${O}/cairn-ui/app.html#/reports`, O), '/reports');
    assert.equal(logic.offlineKey(`${O}/cairn-ui/app.html#/sql`, O), null);
    assert.equal(logic.offlineKey(`${O}/cairn-ui/app.html#/dashboardsx`, O), null);
    assert.equal(logic.offlineKey('https://other.example/cairn-ui/app.html#/dashboards', O), null);
    assert.equal(logic.offlineKey(`${O}/login#/dashboards`, O), null);
    assert.ok(logic.isOfflineError(-106) && logic.isOfflineError(-102) && !logic.isOfflineError(-3) && !logic.isOfflineError(-501));
});
