/*
 * Cairn for Excel: the client the task pane and the custom functions share (one shared runtime).
 *
 * Sign-in is OAuth 2.0 authorization code with PKCE (S256) against Cairn, as the person: the add-in is the public
 * client `agt_excel_addin` (no secret), its redirect URI `<add-in origin>/callback.html`. Tokens are the person's,
 * narrowed to the scopes read, query_data and model; the access token lives an hour and the refresh token rotates
 * on every use (one refresh at a time: a spent refresh token used twice ends the whole grant).
 *
 * No dependencies; runs in the Office runtime (a browser) and in Node (the tests).
 */
(function (root) {
    'use strict';

    var SCOPES = 'read query_data model';
    var KEY_TOKENS = 'cairn.tokens';
    var KEY_PENDING = 'cairn.pending';
    var SKEW_MS = 60 * 1000;

    function CairnError(message, status, signIn) {
        var e = new Error(message);
        e.name = 'CairnError';
        e.status = status || 0;
        e.signIn = !!signIn;
        return e;
    }

    function b64url(bytes) {
        var s = '';
        for (var i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
        var b64 = typeof btoa === 'function' ? btoa(s) : Buffer.from(s, 'binary').toString('base64');
        return b64.replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
    }

    function randomString(n) {
        var a = new Uint8Array(n);
        root.crypto.getRandomValues(a);
        return b64url(a);
    }

    function challengeOf(verifier) {
        return root.crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier)).then(function (d) {
            return b64url(new Uint8Array(d));
        });
    }

    /** Storage over OfficeRuntime.storage (or anything with getItem/setItem/removeItem, sync or async). */
    function storageOf(s) {
        return {
            get: function (k) { return Promise.resolve(s.getItem(k)).then(function (v) { return v ? JSON.parse(v) : null; }); },
            set: function (k, v) { return Promise.resolve(s.setItem(k, JSON.stringify(v))); },
            remove: function (k) { return Promise.resolve(s.removeItem(k)); },
        };
    }

    /**
     * @param {{apiBase: string, clientId: string, redirectUri: string, storage: object, fetch?: Function}} cfg
     *   apiBase: Cairn's API (https://cairn.example.com/cairn/api); storage: getItem/setItem/removeItem.
     */
    function Client(cfg) {
        if (!cfg || !cfg.apiBase || !cfg.clientId || !cfg.redirectUri) {
            throw CairnError('Cairn for Excel is not configured (apiBase, clientId, redirectUri)', 0);
        }
        this.api = String(cfg.apiBase).replace(/\/+$/, '');
        this.clientId = cfg.clientId;
        this.redirectUri = cfg.redirectUri;
        this.store = storageOf(cfg.storage);
        this.fetch = cfg.fetch || root.fetch.bind(root);
        this.refreshing = null;
    }

    /** The authorization URL to open in the sign-in dialog; the verifier and state wait in storage. */
    Client.prototype.beginSignIn = function () {
        var self = this;
        var verifier = randomString(48), state = randomString(24);
        return challengeOf(verifier).then(function (challenge) {
            return self.store.set(KEY_PENDING, { verifier: verifier, state: state, at: Date.now() }).then(function () {
                var q = new URLSearchParams({
                    response_type: 'code', client_id: self.clientId, redirect_uri: self.redirectUri, scope: SCOPES,
                    state: state, code_challenge: challenge, code_challenge_method: 'S256',
                });
                return { url: self.api + '/oauth/authorize?' + q.toString(), state: state };
            });
        });
    };

    /** Finishes sign-in with the URL the dialog came back to (`callback.html?code=…&state=…`). */
    Client.prototype.completeSignIn = function (callbackUrl) {
        var self = this;
        var u = new URL(callbackUrl);
        var p = u.searchParams;
        return self.store.get(KEY_PENDING).then(function (pending) {
            return self.store.remove(KEY_PENDING).then(function () {
                if (u.origin + u.pathname !== new URL(self.redirectUri).origin + new URL(self.redirectUri).pathname) {
                    throw CairnError('sign-in came back to an unexpected address', 400, true);
                }
                if (p.get('error')) throw CairnError('sign-in was not completed: ' + (p.get('error_description') || p.get('error')), 400, true);
                if (!pending || !p.get('code') || p.get('state') !== pending.state || Date.now() - pending.at > 600000) {
                    throw CairnError('this sign-in expired or does not match; sign in again', 400, true);
                }
                return self._token({ grant_type: 'authorization_code', code: p.get('code'), client_id: self.clientId,
                                     redirect_uri: self.redirectUri, code_verifier: pending.verifier });
            });
        });
    };

    Client.prototype._token = function (form) {
        var self = this;
        return self.fetch(self.api + '/oauth/token', {
            method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            body: new URLSearchParams(form).toString(),
        }).then(function (r) {
            return r.json().catch(function () { return {}; }).then(function (body) {
                if (!r.ok || !body.access_token) {
                    return self.store.remove(KEY_TOKENS).then(function () {
                        throw CairnError('sign in to Cairn again (' + (body.error_description || body.error || r.status) + ')', 401, true);
                    });
                }
                var t = { access_token: body.access_token, refresh_token: body.refresh_token, scope: body.scope,
                          expires_at: Date.now() + (Number(body.expires_in) || 3600) * 1000 };
                return self.store.set(KEY_TOKENS, t).then(function () { return t; });
            });
        });
    };

    Client.prototype.signedIn = function () {
        return this.store.get(KEY_TOKENS).then(function (t) { return !!(t && t.refresh_token); });
    };

    /** A valid access token: the stored one, or a refreshed one (one refresh at a time). */
    Client.prototype.accessToken = function (force) {
        var self = this;
        return self.store.get(KEY_TOKENS).then(function (t) {
            if (!t) throw CairnError('sign in to Cairn first (Home ▸ Cairn ▸ Sign in)', 401, true);
            if (!force && t.expires_at - SKEW_MS > Date.now()) return t.access_token;
            if (!self.refreshing) {
                self.refreshing = self._token({ grant_type: 'refresh_token', refresh_token: t.refresh_token, client_id: self.clientId })
                    .then(function (n) { self.refreshing = null; return n; },
                          function (e) { self.refreshing = null; throw e; });
            }
            return self.refreshing.then(function (n) { return n.access_token; });
        });
    };

    Client.prototype.request = function (method, path, body, retried) {
        var self = this;
        return self.accessToken(!!retried).then(function (token) {
            var h = { Authorization: 'Bearer ' + token, Accept: 'application/json' };
            if (body !== undefined) h['Content-Type'] = 'application/json';
            return self.fetch(self.api + path, { method: method, headers: h,
                                                 body: body === undefined ? undefined : JSON.stringify(body) });
        }).then(function (r) {
            if (r.status === 401 && !retried) return self.request(method, path, body, true);
            return r.json().catch(function () { return {}; }).then(function (out) {
                if (!r.ok) {
                    var detail = typeof out.detail === 'string' ? out.detail : (out.detail ? JSON.stringify(out.detail) : r.statusText);
                    throw CairnError(detail || ('HTTP ' + r.status), r.status, r.status === 401);
                }
                return out;
            });
        });
    };

    Client.prototype.signOut = function () {
        var self = this;
        return self.store.get(KEY_TOKENS).then(function (t) {
            var revoke = [];
            if (t) {
                [t.refresh_token, t.access_token].forEach(function (tok) {
                    if (!tok) return;
                    revoke.push(self.fetch(self.api + '/oauth/revoke', {
                        method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                        body: new URLSearchParams({ token: tok, client_id: self.clientId }).toString(),
                    }).catch(function () { return null; }));
                });
            }
            return Promise.all(revoke).then(function () { return self.store.remove(KEY_TOKENS); });
        });
    };

    // ---- what the functions and the task pane ask for ------------------------------------------------
    Client.prototype.me = function () { return this.request('GET', '/me'); };
    Client.prototype.metrics = function (q) {
        return this.request('GET', '/data/sheets/metrics' + (q ? '?q=' + encodeURIComponent(q) : ''));
    };
    Client.prototype.query = function (sql, connection) {
        return this.request('POST', '/data/sheets/query', { sql: String(sql), connection: connection || null })
            .then(function (o) { return o.values; });
    };
    Client.prototype.metric = function (name, dimensions, filters) {
        return this.request('POST', '/data/sheets/metric', { name: String(name), dimensions: dimensions || null,
                                                             filters: filters || null })
            .then(function (o) { return o.values; });
    };
    Client.prototype.ask = function (question) {
        return this.request('POST', '/data/sheets/ask', { question: String(question) }).then(function (o) { return o.values; });
    };

    /** A formula that calls a function with literal arguments (quotes doubled, as Excel and Sheets write them). */
    function formula(fn, args) {
        while (args.length && (args[args.length - 1] === null || args[args.length - 1] === undefined || args[args.length - 1] === '')) args.pop();
        return '=' + fn + '(' + args.map(function (a) {
            return '"' + String(a === null || a === undefined ? '' : a).replace(/"/g, '""') + '"';
        }).join(',') + ')';
    }

    /** The client from the deployed configuration (config.js) over OfficeRuntime.storage: one per runtime, so the
     *  task pane and the custom functions share one refresh at a time. */
    var shared = null;
    function fromConfig() {
        if (shared) return shared;
        var c = root.CAIRN_ADDIN_CONFIG || {};
        var storage = (root.OfficeRuntime && root.OfficeRuntime.storage) || root.localStorage;
        shared = new Client({ apiBase: c.apiBase, clientId: c.clientId || 'agt_excel_addin',
                              redirectUri: c.redirectUri || (root.location ? root.location.origin + '/callback.html' : ''),
                              storage: storage });
        return shared;
    }

    var api = { Client: Client, CairnError: CairnError, formula: formula, fromConfig: fromConfig, challengeOf: challengeOf };
    root.CairnAddin = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
