'use strict';
// What the app may do: the computer's policy file, each workspace's settings and the person's choice, the strictest
// winning; and what to do about an update.
const assert = require('node:assert/strict');
const test = require('node:test');

const p = require('../src/policy');

const files = (map) => (f) => {
    if (!(f in map)) {
        const e = new Error('ENOENT');
        e.code = 'ENOENT';
        throw e;
    }
    if (map[f] instanceof Error) throw map[f];
    return map[f];
};

test('the computer\'s policy files: where, merged strictly, and failing closed', () => {
    assert.deepEqual(p.machinePolicyFiles('linux', {}), ['/etc/cairn/desktop-policy.json']);
    assert.deepEqual(p.machinePolicyFiles('darwin', { CAIRN_DESKTOP_POLICY_FILE: '/tmp/extra.json' }),
        ['/Library/Application Support/Cairn/desktop-policy.json', '/tmp/extra.json']);
    assert.match(p.machinePolicyFiles('win32', { ProgramData: 'D:\\PD' })[0], /D:\\PD.Cairn.desktop-policy\.json$/);
    assert.deepEqual(p.readMachinePolicy(['/none'], files({})), { error: null });
    const merged = p.readMachinePolicy(['/a', '/b'], files({
        '/a': JSON.stringify({ updates: 'auto', pinned_version: '2.0.0', offline_cache: true, offline_cache_pages: 50,
            allowed_origins: ['https://a.example', 'https://b.example'], workspaces: [{ name: 'prod', url: 'https://a.example' }] }),
        '/b': JSON.stringify({ updates: 'notify', pinned_version: 'v1.5.0', offline_cache: false, offline_cache_pages: 10,
            allowed_origins: ['https://b.example'], global_shortcut: false }),
    }));
    assert.equal(merged.updates, 'notify');
    assert.equal(merged.pinned_version, '1.5.0');
    assert.equal(merged.offline_cache, false);
    assert.equal(merged.offline_cache_pages, 10);
    assert.deepEqual(merged.allowed_origins, ['https://b.example']);
    assert.equal(merged.global_shortcut, false);
    assert.deepEqual(merged.workspaces, [{ name: 'prod', url: 'https://a.example' }]);
    // A file that is there but unreadable or not JSON: no updates and no offline cache until it is fixed.
    for (const bad of ['{not json', '[]', '"x"', new Error('EACCES')]) {
        const r = p.readMachinePolicy(['/a'], files({ '/a': bad }));
        assert.equal(r.updates, 'off');
        assert.equal(r.offline_cache, false);
        assert.match(r.error, /\/a/);
    }
    assert.equal(p.mergeStrict([{ updates: 'sometimes' }]).updates, 'off');     // an unknown value: off
});

test('offline caching needs the computer, the workspace (known) and the person to allow it', () => {
    const none = { error: null };
    assert.deepEqual(p.offlineFor(none, {}, {}), { enabled: true, maxPages: 20, maxAgeDays: 14 });
    assert.equal(p.offlineFor(none, null, {}).enabled, false);                      // the workspace's settings unknown
    assert.equal(p.offlineFor(none, { offline_cache: false }, {}).enabled, false);
    assert.equal(p.offlineFor({ offline_cache: false }, {}, {}).enabled, false);
    assert.equal(p.offlineFor(none, {}, { offlineCache: false }).enabled, false);
    assert.equal(p.offlineFor({ error: 'x' }, {}, {}).enabled, false);
    assert.deepEqual(p.offlineFor({ offline_cache_pages: 5, offline_cache_max_age_days: 3 },
        { offline_cache_pages: 50, offline_cache_max_age_days: 30 }, {}), { enabled: true, maxPages: 5, maxAgeDays: 3 });
    assert.deepEqual(p.offlineFor(none, { offline_cache_pages: 7, offline_cache_max_age_days: 2 }, {}),
        { enabled: true, maxPages: 7, maxAgeDays: 2 });
});

test('update settings: strictest of the computer, every workspace and the person; the lowest pin', () => {
    const ws = (name, policy) => ({ name, policy });
    assert.deepEqual(p.updatePolicy({}, [], {}), { updates: 'auto', pinned: null, feed: null, source: { updates: null, pinned: null } });
    let u = p.updatePolicy({}, [ws('a', { updates: 'auto', pinned_version: '1.4.0' }), ws('b', { updates: 'notify', pinned_version: '1.2.0' }),
        ws('c', null)], {});
    assert.equal(u.updates, 'notify');
    assert.equal(u.source.updates, 'b');
    assert.equal(u.pinned, '1.2.0');
    assert.equal(u.source.pinned, 'b');
    u = p.updatePolicy({ pinned_version: '1.9.0', update_feed_url: 'https://updates.example/cairn' },
        [ws('a', { pinned_version: '1.1.0', update_feed_url: 'https://other.example' })], {});
    assert.equal(u.pinned, '1.9.0');                                                   // the computer's pin wins
    assert.equal(u.source.pinned, 'computer');
    assert.equal(u.feed, 'https://updates.example/cairn');
    assert.equal(p.updatePolicy({}, [ws('a', { update_feed_url: 'https://w.example' })], {}).feed, 'https://w.example');
    u = p.updatePolicy({}, [ws('a', { updates: 'auto' })], { updates: false });
    assert.deepEqual([u.updates, u.source.updates], ['off', 'you']);
    assert.equal(p.updatePolicy({ updates: 'off' }, [ws('a', { updates: 'auto' })], {}).updates, 'off');
    assert.equal(p.updatePolicy({ error: 'bad file' }, [], {}).updates, 'off');
});

test('update decisions, honest about unsigned builds', () => {
    const pol = (extra = {}) => ({ updates: 'auto', pinned: null, feed: null, source: { updates: null, pinned: null }, ...extra });
    const d = (o) => p.updateDecision({ current: '1.0.0', available: '1.1.0', platform: 'win32', signed: false, ...o, policy: o.policy || pol() });
    assert.equal(d({}).action, 'download');                                            // Windows (NSIS), unsigned: checksum only
    assert.equal(d({ platform: 'linux' }).action, 'download');                         // AppImage
    assert.equal(d({ platform: 'linux', appImage: false }).action, 'notify');           // a .deb: the package manager
    assert.equal(d({ platform: 'darwin' }).action, 'notify');                          // macOS needs a signed app
    assert.match(d({ platform: 'darwin' }).reason, /signed/);
    assert.equal(d({ platform: 'darwin', signed: true }).action, 'download');
    assert.equal(d({ available: '1.0.0' }).action, 'none');
    assert.equal(d({ available: '0.9.9' }).action, 'none');
    assert.equal(d({ available: null }).action, 'none');
    assert.equal(d({ packaged: false }).action, 'notify');
    assert.equal(d({ policy: pol({ updates: 'off', source: { updates: 'computer' } }) }).action, 'blocked');
    assert.equal(d({ policy: pol({ updates: 'notify' }) }).action, 'notify');
    const pinned = d({ available: '1.3.0', policy: pol({ pinned: '1.2.0', source: { pinned: 'prod' } }) });
    assert.deepEqual([pinned.action, pinned.reason], ['blocked', 'pinned to 1.2.0 (prod)']);
    assert.equal(d({ available: '1.2.0', policy: pol({ pinned: '1.2.0' }) }).action, 'download');
    assert.equal(p.compareVersions('1.10.0', '1.9.9'), 1);
    assert.equal(p.compareVersions('v2.0.0', '2.0.0'), 0);
    assert.equal(p.compareVersions('1.2.3-beta.1', '1.2.4'), -1);
});
