'use strict';
/*
 * The preload of a workspace's page (sandboxed, context-isolated). The page gets one read-only object,
 * `window.cairnDesktop` ({desktop: true, version}), and its notifications go to the app: `new Notification(...)` and
 * `registration.showNotification(...)` are shown by the operating system through the main process, which knows the
 * workspace and opens the right page when one is clicked. Nothing else crosses: no Node.js, no IPC for the page.
 */
const { contextBridge, ipcRenderer, webFrame } = require('electron');

const version = (process.argv.find((a) => a.startsWith('--cairn-desktop-version=')) || '').split('=')[1] || '';

function shim() {
    const post = (title, opts) => {
        const o = opts && typeof opts === 'object' ? opts : {};
        const data = o.data && typeof o.data === 'object' ? o.data : {};
        window.postMessage({ __cairnDesktop: 'notify', title: String(title), body: o.body ? String(o.body) : '',
            tag: o.tag ? String(o.tag) : '', url: typeof data.url === 'string' ? data.url : '' }, window.location.origin);
    };
    class DesktopNotification extends EventTarget {
        constructor(title, opts = {}) {
            super();
            this.title = String(title);
            this.body = opts.body || '';
            this.tag = opts.tag || '';
            this.data = opts.data === undefined ? null : opts.data;
            this.onclick = null;
            this.onclose = null;
            this.onerror = null;
            this.onshow = null;
            post(title, opts);
            setTimeout(() => {
                const ev = new Event('show');
                this.dispatchEvent(ev);
                if (typeof this.onshow === 'function') this.onshow(ev);
            }, 0);
        }

        close() {}

        static get permission() {
            return 'granted';
        }

        static requestPermission(cb) {
            if (typeof cb === 'function') cb('granted');
            return Promise.resolve('granted');
        }

        static get maxActions() {
            return 0;
        }
    }
    Object.defineProperty(window, 'Notification', { value: DesktopNotification, configurable: true, writable: true });
    if (window.ServiceWorkerRegistration) {
        window.ServiceWorkerRegistration.prototype.showNotification = function showNotification(title, opts) {
            post(title, opts);
            return Promise.resolve();
        };
    }
}

if (window.location.protocol === 'https:' || window.location.protocol === 'http:') {
    contextBridge.exposeInMainWorld('cairnDesktop', Object.freeze({ desktop: true, version }));
    window.addEventListener('message', (e) => {
        if (e.source !== window || e.origin !== window.location.origin) return;
        const d = e.data;
        if (!d || typeof d !== 'object' || d.__cairnDesktop !== 'notify') return;
        ipcRenderer.send('ws:notify', {
            title: String(d.title || '').slice(0, 200), body: String(d.body || '').slice(0, 1000),
            tag: String(d.tag || '').slice(0, 200), url: String(d.url || '').slice(0, 2048),
        });
    });
    if (typeof contextBridge.executeInMainWorld === 'function') {
        contextBridge.executeInMainWorld({ func: shim });
    } else {
        webFrame.executeJavaScript(`(${shim.toString()})()`);
    }
}
