/*
 * The installable Cairn workspace (a progressive web app): the same module the catalog mounts, full page, plus the
 * service worker (sw.js) that keeps the app shell for fast and offline starts, never data.
 * Served by the consumer UI at /cairn-ui/app.html, next to Cairn's API at /cairn/api on the same origin.
 */
(function () {
    'use strict';
    var api = document.documentElement.getAttribute('data-cairn-api') || '/cairn/api';

    function mount() {
        // init may return a promise or nothing (no shared modules): either way, then the module.
        return Promise.resolve().then(function () { return cairnConsumer.init({}); }).then(function () {
            return cairnConsumer.get('./mount');
        }).then(function (factory) {
            var root = document.getElementById('root');
            root.textContent = '';
            factory().mount(root, { apiBaseUrl: api, routerMode: 'hash' });
        });
    }

    mount().catch(function (e) {
        var root = document.getElementById('root');
        root.innerHTML = '<p class="booting" role="alert"></p>';
        root.firstChild.textContent = 'Cairn could not start: ' + (e && e.message ? e.message : e);
    });

    if ('serviceWorker' in navigator) {
        window.addEventListener('load', function () {
            navigator.serviceWorker.register('sw.js?api=' + encodeURIComponent(api), { scope: './' })
                .catch(function (e) { console.warn('Cairn: the service worker did not register', e); });
        });
    }
})();
