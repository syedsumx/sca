'use strict';
/*
 * The preload of Cairn Desktop's own pages (the top bar and the panel: add a workspace, offline, settings). Each
 * function below is one request the main process checks; there is no general channel. Workspace pages never get
 * this preload.
 */
const { contextBridge, ipcRenderer } = require('electron');

if (window.location.protocol === 'file:') {
    const call = (channel) => (...args) => ipcRenderer.invoke(channel, ...args);
    contextBridge.exposeInMainWorld('cairnDesktop', Object.freeze({
        onState: (cb) => {
            ipcRenderer.on('state', (_e, s) => cb(s));
            ipcRenderer.send('local:ready');
        },
        addWorkspace: call('ws:add'),
        switchWorkspace: call('ws:switch'),
        workspaceMenu: call('bar:menu'),
        retry: call('ws:retry'),
        ask: call('ws:ask'),
        cachedPage: call('cache:page'),
        closePanel: call('panel:close'),
        openPanel: call('panel:open'),
        setPrefs: call('prefs:set'),
        installCli: call('cli:install'),
        cliSignIn: call('cli:sign-in'),
        checkUpdates: call('updates:check'),
        installUpdate: call('updates:install'),
    }));
}
