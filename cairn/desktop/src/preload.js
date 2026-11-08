'use strict';
// The only bridge into the page: the setup page saves Cairn's address. Cairn's own pages get nothing.
const { contextBridge, ipcRenderer } = require('electron');

if (location.protocol === 'file:') {
    contextBridge.exposeInMainWorld('cairnDesktop', {
        setCairnUrl: (url) => ipcRenderer.invoke('cairn:set-url', String(url)),
    });
}
