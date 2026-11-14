/*
 * Cairn's service worker (scope: the consumer UI, /cairn-ui/).
 *
 * It keeps the app shell, never data: only same-origin GETs of the UI's own static files are cached (the shell at
 * install; content-hashed chunks as they load). Cairn's API (/cairn/api), the catalog, documents and everything
 * else go straight to the network and are never stored, so nothing governed stays on the device. A page opened
 * without a network gets the offline page.
 *
 * Push: Cairn sends an empty push message for each inbox notification; this worker then asks Cairn, with the
 * person's own session, for their newest unread notification and shows it. Nothing travels through the push
 * service but the wake-up.
 */
/* eslint-disable no-restricted-globals */
'use strict';

var BUILD = '__CAIRN_BUILD__';
var CACHE = 'cairn-shell-' + BUILD;
var SHELL = ['app.html', 'app.js', 'app.css', 'antd.min.css', 'offline.html', 'offline.js', 'manifest.webmanifest', 'remoteEntry.js',
             'icons/icon-192.png', 'icons/icon-512.png', 'icons/maskable-512.png', 'icons/apple-touch-icon.png'];
var API = new URL(self.location.href).searchParams.get('api') || '/cairn/api';
var HASHED = /\.[0-9a-f]{8}\.js$/;

function scopePath() {
    return new URL(self.registration.scope).pathname;
}

self.addEventListener('install', function (event) {
    event.waitUntil(caches.open(CACHE).then(function (cache) {
        return cache.addAll(SHELL.map(function (p) { return new Request(p, { cache: 'reload' }); }));
    }).then(function () { return self.skipWaiting(); }));
});

self.addEventListener('activate', function (event) {
    event.waitUntil(caches.keys().then(function (keys) {
        return Promise.all(keys.filter(function (k) { return k.indexOf('cairn-shell-') === 0 && k !== CACHE; })
            .map(function (k) { return caches.delete(k); }));
    }).then(function () { return self.clients.claim(); }));
});

function cacheable(response) {
    return response && response.ok && response.type === 'basic';
}

self.addEventListener('fetch', function (event) {
    var req = event.request;
    if (req.method !== 'GET') return;
    var url = new URL(req.url);
    // Only the UI's own files: Cairn's API and anything else is never touched (and never cached).
    if (url.origin !== self.location.origin || url.pathname.indexOf(scopePath()) !== 0) return;
    if (url.pathname.indexOf(API) === 0) return;

    if (req.mode === 'navigate') {
        // The shell from the network (a deploy shows at once); without a network, the offline page.
        event.respondWith(fetch(req).catch(function () {
            return caches.open(CACHE).then(function (cache) { return cache.match('offline.html'); });
        }));
        return;
    }
    if (HASHED.test(url.pathname)) {
        // Content-hashed chunks never change: from the cache, else the network (and kept).
        event.respondWith(caches.open(CACHE).then(function (cache) {
            return cache.match(req).then(function (hit) {
                return hit || fetch(req).then(function (res) {
                    if (cacheable(res)) cache.put(req, res.clone());
                    return res;
                });
            });
        }));
        return;
    }
    // The rest of the shell: the network first, the cached copy when offline.
    event.respondWith(fetch(req).then(function (res) {
        if (cacheable(res) && SHELL.some(function (p) { return url.pathname === scopePath() + p; })) {
            var copy = res.clone();
            caches.open(CACHE).then(function (cache) { cache.put(req, copy); });
        }
        return res;
    }).catch(function () {
        return caches.match(req).then(function (hit) { return hit || Response.error(); });
    }));
});

// ---- push ------------------------------------------------------------------------------------------------
self.addEventListener('push', function (event) {
    var app = scopePath() + 'app.html';
    event.waitUntil(fetch(API + '/notify/inbox?unread=true&limit=1', { credentials: 'same-origin', cache: 'no-store' })
        .then(function (r) { return r.ok ? r.json() : []; })
        .catch(function () { return []; })
        .then(function (items) {
            var n = items && items[0];
            return self.registration.showNotification(n ? n.title : 'Cairn', {
                body: n ? String(n.body || '').slice(0, 240) : 'You have a new notification.',
                tag: n ? n.id : 'cairn-inbox',
                icon: scopePath() + 'icons/icon-192.png',
                badge: scopePath() + 'icons/icon-192.png',
                data: { url: n && n.action_url ? n.action_url : app + '#/discover' },
            });
        }));
});

self.addEventListener('notificationclick', function (event) {
    event.notification.close();
    var target = new URL((event.notification.data && event.notification.data.url) || 'app.html', self.registration.scope);
    if (target.origin !== self.location.origin) target = new URL('app.html#/discover', self.registration.scope);
    event.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (wins) {
        for (var i = 0; i < wins.length; i++) {
            if (new URL(wins[i].url).origin === target.origin && 'focus' in wins[i]) {
                wins[i].navigate(target.href);
                return wins[i].focus();
            }
        }
        return self.clients.openWindow(target.href);
    }));
});
