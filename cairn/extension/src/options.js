const $ = (id) => document.getElementById(id);
const lines = (v) => v.split(/\s+/).map((h) => h.trim().replace(/^https?:\/\//, '').replace(/\/.*$/, '')).filter(Boolean);

chrome.storage.sync.get({ cairnUrl: '', custom: {} }).then(({ cairnUrl, custom }) => {
    $('cairnUrl').value = cairnUrl;
    for (const tool of ['looker', 'tableau', 'powerbi']) $(tool).value = (custom[tool] || []).join('\n');
});

$('save').addEventListener('click', async () => {
    const cairnUrl = $('cairnUrl').value.trim().replace(/\/+$/, '');
    const custom = { looker: lines($('looker').value), tableau: lines($('tableau').value), powerbi: lines($('powerbi').value) };
    const origins = [cairnUrl && `${new URL(cairnUrl).origin}/*`, ...Object.values(custom).flat().map((h) => `https://${h}/*`)].filter(Boolean);
    // Host access is asked for only what the user typed: the Cairn address and their own BI hosts.
    const granted = origins.length ? await chrome.permissions.request({ origins }) : true;
    if (!granted) {
        $('saved').textContent = 'Permission was not granted; nothing saved.';
        return;
    }
    await chrome.storage.sync.set({ cairnUrl, custom });
    $('saved').textContent = 'Saved.';
});
