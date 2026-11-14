// Service worker: remembers each tab's BI page, opens the side panel from the toolbar button, and
// fetches the catalog card with the user's Cairn session (host permission on the Cairn URL).
import { lookup } from './lookup.js';
import { patternsFor, recognise } from './match.js';

const pages = new Map(); // tabId -> BiPage

let baked = null; // defaults.json: the Cairn address built in with `node build.mjs --cairn-url`

async function settings() {
    if (baked === null) baked = await fetch(chrome.runtime.getURL('defaults.json')).then((r) => r.json()).catch(() => ({}));
    const s = await chrome.storage.sync.get({ cairnUrl: '', custom: {} });
    return { cairnUrl: (s.cairnUrl || baked.cairnUrl || '').replace(/\/+$/, ''), custom: s.custom || {} };
}

async function registerCustomHosts() {
    const { custom } = await settings();
    const matches = patternsFor(custom);
    await chrome.scripting.unregisterContentScripts({ ids: ['cairn-custom'] }).catch(() => {});
    if (matches.length) {
        await chrome.scripting.registerContentScripts([
            { id: 'cairn-custom', matches, js: ['content.js'], runAt: 'document_idle' },
        ]);
    }
}

chrome.runtime.onInstalled.addListener(() => {
    chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
    void registerCustomHosts();
});
chrome.storage.onChanged.addListener(() => void registerCustomHosts());
chrome.tabs.onRemoved.addListener((tabId) => pages.delete(tabId));

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
    if (msg.type === 'cairn:page' && sender.tab) {
        void settings().then(({ custom }) => {
            const page = recognise(msg.url, custom);
            if (page) pages.set(sender.tab.id, page);
            else pages.delete(sender.tab.id);
            chrome.runtime.sendMessage({ type: 'cairn:changed', tabId: sender.tab.id }).catch(() => {});
        });
        return false;
    }
    if (msg.type === 'cairn:card') {
        void (async () => {
            const { cairnUrl, custom } = await settings();
            const page = msg.url ? recognise(msg.url, custom) : pages.get(msg.tabId);
            if (!page) return reply({ status: 'not-bi' });
            reply({ ...(await lookup(cairnUrl, page.key)), page, cairnUrl });
        })();
        return true;
    }
    return false;
});
