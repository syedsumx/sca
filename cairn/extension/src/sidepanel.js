import { render } from './render.js';

const root = document.getElementById('root');
const open = document.getElementById('open');
// ?url=… shows the card for that page directly (for previews and tests).
const fixed = new URLSearchParams(location.search).get('url');

async function refresh() {
    let tabId;
    if (!fixed) {
        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
        tabId = tab?.id;
    }
    const result = await chrome.runtime.sendMessage({ type: 'cairn:card', tabId, url: fixed });
    root.innerHTML = render(result || { status: 'not-bi' });
    if (result?.status === 'ok') {
        open.href = result.data.card.url;
        open.hidden = false;
    } else {
        open.hidden = true;
    }
}

chrome.runtime.onMessage.addListener((msg) => {
    if (msg.type === 'cairn:changed') void refresh();
});
chrome.tabs?.onActivated?.addListener(() => void refresh());
void refresh();
