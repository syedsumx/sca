'use strict';
// The offline cache: encrypted at rest, per workspace, limited in count and age, cleared on sign-out, and nothing
// kept without encryption.
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const { OfflineCache, safeStorageEncryptor } = require('../src/offline-cache');

function aes(available = true) {
    const key = crypto.randomBytes(32);
    return {
        available: () => available,
        encrypt(s) {
            const iv = crypto.randomBytes(12);
            const c = crypto.createCipheriv('aes-256-gcm', key, iv);
            const body = Buffer.concat([c.update(s, 'utf8'), c.final()]);
            return Buffer.concat([iv, c.getAuthTag(), body]);
        },
        decrypt(b) {
            const d = crypto.createDecipheriv('aes-256-gcm', key, b.subarray(0, 12));
            d.setAuthTag(b.subarray(12, 28));
            return Buffer.concat([d.update(b.subarray(28)), d.final()]).toString('utf8');
        },
    };
}

const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), 'cairn-offline-'));
const page = (key, title = `Dashboard ${key}`) => ({ key, title, html: `<h1>${title}</h1><p>secret revenue 42</p>` });

test('pages are encrypted at rest and come back as kept', () => {
    const dir = tmp();
    const c = new OfflineCache(dir, aes());
    assert.equal(c.put('ws-a', page('/dashboards?open=1', 'Revenue')), true);
    const listed = c.list('ws-a');
    assert.equal(listed.length, 1);
    assert.equal(listed[0].title, 'Revenue');
    assert.equal(c.get('ws-a', listed[0].id).html, '<h1>Revenue</h1><p>secret revenue 42</p>');
    for (const f of fs.readdirSync(path.join(dir, 'ws-a'))) {
        const raw = fs.readFileSync(path.join(dir, 'ws-a', f));
        assert.ok(!raw.includes('secret') && !raw.includes('Revenue') && !raw.includes('dashboards'), f);
        if (process.platform !== 'win32') assert.equal(fs.statSync(path.join(dir, 'ws-a', f)).mode & 0o777, 0o600);
    }
    // Another key (a new keychain) cannot read them: as good as empty, never an error.
    const other = new OfflineCache(dir, aes());
    assert.deepEqual(other.list('ws-a'), []);
    assert.equal(other.get('ws-a', listed[0].id), null);
});

test('per workspace, newest first, at most N pages, and the same page replaced', () => {
    let now = 1_000_000;
    const c = new OfflineCache(tmp(), aes(), () => now);
    for (let i = 1; i <= 5; i += 1) {
        now += 1000;
        c.put('ws-a', page(`/reports?r=${i}`), { maxPages: 3 });
    }
    c.put('ws-b', page('/dashboards'));
    assert.deepEqual(c.list('ws-a', { maxPages: 3 }).map((e) => e.key), ['/reports?r=5', '/reports?r=4', '/reports?r=3']);
    assert.deepEqual(c.list('ws-b').map((e) => e.key), ['/dashboards']);
    now += 1000;
    c.put('ws-a', page('/reports?r=4', 'Updated'), { maxPages: 3 });
    assert.deepEqual(c.list('ws-a', { maxPages: 3 }).map((e) => [e.key, e.title]),
        [['/reports?r=4', 'Updated'], ['/reports?r=5', 'Dashboard /reports?r=5'], ['/reports?r=3', 'Dashboard /reports?r=3']]);
    assert.equal(fs.readdirSync(c.folder('ws-a')).filter((f) => f !== 'index.bin').length, 3);
});

test('pages expire', () => {
    let now = Date.UTC(2026, 0, 1);
    const c = new OfflineCache(tmp(), aes(), () => now);
    c.put('ws-a', page('/dashboards?open=old'));
    now += 3 * 86400000;
    c.put('ws-a', page('/dashboards?open=new'));
    const oldId = OfflineCache.id('/dashboards?open=old');
    now += 2 * 86400000;                                                     // old: 5 days, new: 2 days
    assert.equal(c.get('ws-a', oldId, { maxAgeDays: 4 }), null);
    assert.deepEqual(c.list('ws-a', { maxAgeDays: 4 }).map((e) => e.key), ['/dashboards?open=new']);
    assert.ok(!fs.existsSync(path.join(c.folder('ws-a'), `${oldId}.bin`)));
    now += 30 * 86400000;
    assert.deepEqual(c.list('ws-a', { maxAgeDays: 14 }), []);
    assert.ok(!fs.existsSync(c.folder('ws-a')));
});

test('sign-out clears a workspace; nothing is kept without encryption or over the size limit', () => {
    const c = new OfflineCache(tmp(), aes());
    c.put('ws-a', page('/dashboards'));
    c.put('ws-b', page('/dashboards'));
    c.clear('ws-a');
    assert.deepEqual(c.list('ws-a'), []);
    assert.equal(c.list('ws-b').length, 1);
    c.clearAll();
    assert.deepEqual(c.list('ws-b'), []);
    const plain = new OfflineCache(tmp(), aes(false));
    assert.equal(plain.put('ws-a', page('/dashboards')), false);
    assert.ok(!fs.existsSync(plain.folder('ws-a')));
    assert.equal(c.put('ws-a', { key: '/reports', html: 'x'.repeat(2000) }, { maxBytes: 1000 }), false);
    assert.throws(() => c.folder('../etc'), /bad workspace/);
    assert.equal(c.get('ws-a', '../../etc/passwd'), null);
});

test('safeStorage counts only with a real keychain (not Linux\'s basic_text)', () => {
    const fake = (available, backend) => ({ isEncryptionAvailable: () => available, getSelectedStorageBackend: () => backend,
        encryptString: (s) => Buffer.from(s), decryptString: (b) => b.toString() });
    assert.equal(safeStorageEncryptor(fake(true, 'gnome_libsecret'), 'linux').available(), true);
    assert.equal(safeStorageEncryptor(fake(true, 'kwallet5'), 'linux').available(), true);
    assert.equal(safeStorageEncryptor(fake(true, 'basic_text'), 'linux').available(), false);
    assert.equal(safeStorageEncryptor(fake(false, 'gnome_libsecret'), 'linux').available(), false);
    assert.equal(safeStorageEncryptor(fake(true, 'basic_text'), 'darwin').available(), true);
    assert.equal(new OfflineCache(tmp(), { available: () => { throw new Error('no'); } }).available(), false);
    const later = safeStorageEncryptor({ ...fake(true, 'gnome_libsecret'), isAsyncEncryptionAvailable: async () => false }, 'linux');
    return later.init().then((ok) => assert.equal(ok, false));
});
