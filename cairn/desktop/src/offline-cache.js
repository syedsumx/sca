'use strict';
/*
 * The offline cache: the last dashboards and reports a person viewed in a workspace, as rendered pages, kept to
 * read while that Cairn cannot be reached.
 *
 * - Per workspace (its own folder), each page and the index encrypted at rest with the given encryptor (Electron's
 *   safeStorage: the OS keychain's key). Without encryption nothing is kept (fail closed).
 * - Only what the app hands it: the app saves a page when the person is looking at it, never by fetching.
 * - At most `maxPages` pages (the oldest go first) and `maxAgeDays` days; an entry over `maxBytes` is not kept.
 * - `clear(workspace)` on sign-out, when the workspace is removed, or when its administrator turns caching off.
 */
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const DEFAULTS = { maxPages: 20, maxAgeDays: 14, maxBytes: 8 * 1024 * 1024 };

class OfflineCache {
    /** `encryptor`: {available() → bool, encrypt(string) → Buffer, decrypt(Buffer) → string}. */
    constructor(dir, encryptor, now = () => Date.now()) {
        this.dir = dir;
        this.encryptor = encryptor;
        this.now = now;
    }

    available() {
        try {
            return !!this.encryptor && this.encryptor.available();
        } catch {
            return false;
        }
    }

    folder(workspaceId) {
        if (!/^[A-Za-z0-9_-]{1,64}$/.test(workspaceId)) throw new Error('bad workspace id');
        return path.join(this.dir, workspaceId);
    }

    read(file) {
        return JSON.parse(this.encryptor.decrypt(fs.readFileSync(file)));
    }

    write(file, value) {
        const tmp = `${file}.${process.pid}.tmp`;
        fs.writeFileSync(tmp, this.encryptor.encrypt(JSON.stringify(value)), { mode: 0o600 });
        fs.renameSync(tmp, file);
    }

    index(workspaceId) {
        const file = path.join(this.folder(workspaceId), 'index.bin');
        if (!fs.existsSync(file)) return [];
        try {
            const entries = this.read(file);
            return Array.isArray(entries) ? entries : [];
        } catch {
            return [];          // unreadable (a new keychain key, a damaged file): as good as empty
        }
    }

    static id(key) {
        return crypto.createHash('sha256').update(String(key)).digest('hex').slice(0, 32);
    }

    /**
     * Keeps a page: {key, title, html}. Returns false when nothing is kept (no encryption, too big). `limits` are the
     * workspace's (maxPages, maxAgeDays).
     */
    put(workspaceId, page, limits = {}) {
        if (!this.available()) return false;
        const lim = { ...DEFAULTS, ...limits };
        const html = String(page.html || '');
        if (!page.key || Buffer.byteLength(html) > lim.maxBytes) return false;
        const dir = this.folder(workspaceId);
        fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
        const id = OfflineCache.id(page.key);
        const at = this.now();
        this.write(path.join(dir, `${id}.bin`), { key: page.key, title: String(page.title || page.key), at, html });
        const entries = [{ id, key: page.key, title: String(page.title || page.key), at },
            ...this.index(workspaceId).filter((e) => e.id !== id)];
        this.write(path.join(dir, 'index.bin'), entries);
        this.prune(workspaceId, lim);
        return true;
    }

    /** The pages kept, newest first: [{id, key, title, at}] (expired ones dropped). */
    list(workspaceId, limits = {}) {
        if (!this.available()) return [];
        this.prune(workspaceId, { ...DEFAULTS, ...limits });
        return this.index(workspaceId);
    }

    /** One page: {key, title, at, html}, or null. */
    get(workspaceId, id, limits = {}) {
        if (!this.available() || !/^[0-9a-f]{32}$/.test(id)) return null;
        const lim = { ...DEFAULTS, ...limits };
        const file = path.join(this.folder(workspaceId), `${id}.bin`);
        if (!fs.existsSync(file)) return null;
        try {
            const page = this.read(file);
            if (this.now() - page.at > lim.maxAgeDays * 86400000) {
                this.prune(workspaceId, lim);
                return null;
            }
            return page;
        } catch {
            return null;
        }
    }

    prune(workspaceId, lim) {
        const dir = this.folder(workspaceId);
        if (!fs.existsSync(dir)) return;
        const entries = this.index(workspaceId);
        const cutoff = this.now() - lim.maxAgeDays * 86400000;
        const keep = entries.filter((e) => e.at >= cutoff).slice(0, lim.maxPages);
        const ids = new Set(keep.map((e) => e.id));
        for (const f of fs.readdirSync(dir)) {
            const m = /^([0-9a-f]{32})\.bin$/.exec(f);
            if (m && !ids.has(m[1])) fs.rmSync(path.join(dir, f), { force: true });
        }
        if (keep.length !== entries.length) {
            if (keep.length) this.write(path.join(dir, 'index.bin'), keep);
            else fs.rmSync(dir, { recursive: true, force: true });
        }
    }

    /** Everything kept for a workspace, gone. */
    clear(workspaceId) {
        fs.rmSync(this.folder(workspaceId), { recursive: true, force: true });
    }

    /** Everything, for every workspace. */
    clearAll() {
        fs.rmSync(this.dir, { recursive: true, force: true });
    }
}

/**
 * Electron's safeStorage as the encryptor. On Linux it counts only with a real keychain (the Secret Service or
 * KWallet): the `basic_text` fallback is a fixed key, so nothing is kept there.
 */
function safeStorageEncryptor(safeStorage, platform = process.platform) {
    let ready = null;                   // set by init() (Electron's asynchronous check), else asked synchronously
    const backendOk = () => platform !== 'linux' || typeof safeStorage.getSelectedStorageBackend !== 'function'
        || !['basic_text', 'unknown'].includes(safeStorage.getSelectedStorageBackend());
    return {
        async init() {
            if (typeof safeStorage.isAsyncEncryptionAvailable === 'function') {
                try {
                    ready = !!(await safeStorage.isAsyncEncryptionAvailable());
                } catch {
                    ready = false;
                }
            }
            return this.available();
        },
        available() {
            const ok = ready !== null ? ready : safeStorage.isEncryptionAvailable();
            return !!ok && backendOk();
        },
        encrypt: (s) => safeStorage.encryptString(s),
        decrypt: (b) => safeStorage.decryptString(b),
    };
}

module.exports = { OfflineCache, safeStorageEncryptor, DEFAULTS };
