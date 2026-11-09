'use strict';
// The panel over the workspace's page: add a workspace, offline (with the pages kept for reading), settings.
(function () {
    const $ = (id) => document.getElementById(id);
    const api = window.cairnDesktop;
    let state = null;
    let shownMode = null;
    let viewing = null;

    const clean = (e) => String(e && e.message ? e.message : e).replace(/^Error invoking remote method '[^']+': (Error: )?/, '');
    const when = (ms) => new Date(ms).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });

    // ---- add ------------------------------------------------------------------------------------------------
    $('add-form').addEventListener('submit', async (e) => {
        e.preventDefault();
        $('add-error').textContent = '';
        $('add-submit').disabled = true;
        $('add-submit').textContent = 'Checking…';
        try {
            const prompt = state && state.panel && state.panel.prompt;
            await api.addWorkspace({ url: $('url').value, name: $('name').value.trim() || undefined,
                route: prompt ? prompt.route : undefined });
            $('url').value = '';
            $('name').value = '';
        } catch (err) {
            $('add-error').textContent = clean(err);
        } finally {
            $('add-submit').disabled = false;
            $('add-submit').textContent = 'Add workspace';
        }
    });
    $('add-cancel').addEventListener('click', () => api.closePanel());

    // ---- offline --------------------------------------------------------------------------------------------
    $('retry').addEventListener('click', async () => {
        if (!state || !state.panel) return;
        $('retry-state').textContent = 'Trying…';
        try {
            const status = await api.retry(state.panel.workspace);
            $('retry-state').textContent = status === 'offline' ? 'Still can’t reach it.' : '';
        } catch (err) {
            $('retry-state').textContent = clean(err);
        }
    });

    async function view(id) {
        const page = await api.cachedPage(state.panel.workspace, id);
        if (!page) return;
        viewing = id;
        $('viewer-title').textContent = page.title;
        $('viewer-asof').textContent = `Read-only · as of ${when(page.at)}`;
        $('frame').srcdoc = page.html;
        $('viewer').hidden = false;
        $('viewer-close').focus();
    }
    function closeViewer() {
        viewing = null;
        $('viewer').hidden = true;
        $('frame').srcdoc = '';
    }
    $('viewer-close').addEventListener('click', closeViewer);
    document.addEventListener('keydown', (e) => {
        if (e.key !== 'Escape') return;
        if (viewing) closeViewer();
        else api.closePanel();
    });

    function renderOffline(s) {
        const ws = s.workspaces.find((w) => w.name === s.panel.workspace);
        $('offline-title').textContent = `${s.panel.workspace} can’t be reached`;
        $('offline-text').textContent = s.panel.reason || (ws && ws.offlineSince
            ? `Offline since ${when(ws.offlineSince)}. Check your connection or VPN; Cairn Desktop tries again on its own.`
            : 'Check your connection or VPN; Cairn Desktop tries again on its own.');
        const list = $('cached');
        list.textContent = '';
        for (const p of s.cached || []) {
            const li = document.createElement('li');
            const b = document.createElement('button');
            b.type = 'button';
            const t = document.createElement('span');
            t.textContent = p.title;
            const at = document.createElement('span');
            at.className = 'muted';
            at.textContent = `as of ${when(p.at)}`;
            b.append(t, at);
            b.addEventListener('click', () => view(p.id));
            li.append(b);
            list.append(li);
        }
        const none = !(s.cached || []).length;
        $('cached-none').textContent = none
            ? (ws && !ws.offlineCache ? 'Nothing is kept for offline reading in this workspace (turned off).'
                : 'Nothing kept yet. Dashboards and reports you open are kept here, read-only and encrypted, to read while Cairn can’t be reached.')
            : 'Read-only copies of what you last opened; they are not updated until Cairn is back.';
    }

    // ---- settings -------------------------------------------------------------------------------------------
    async function setPrefs(p) {
        $('prefs-error').textContent = '';
        try {
            const r = await api.setPrefs(p);
            if (r && r.shortcutOk === false) $('prefs-error').textContent = 'That shortcut is taken by another app or is not valid.';
        } catch (err) {
            $('prefs-error').textContent = clean(err);
        }
    }
    $('pref-updates').addEventListener('change', (e) => setPrefs({ updates: e.target.checked }));
    $('pref-offline').addEventListener('change', (e) => setPrefs({ offlineCache: e.target.checked }));
    const shortcut = () => setPrefs({ globalShortcut: { enabled: $('pref-shortcut').checked, accelerator: $('pref-accelerator').value.trim() } });
    $('pref-shortcut').addEventListener('change', shortcut);
    $('pref-accelerator').addEventListener('change', shortcut);
    $('check-updates').addEventListener('click', () => api.checkUpdates().catch((e) => { $('prefs-error').textContent = clean(e); }));
    $('install-update').addEventListener('click', () => api.installUpdate());
    $('install-cli').addEventListener('click', () => api.installCli().catch((e) => { $('prefs-error').textContent = clean(e); }));
    $('cli-sign-in').addEventListener('click', () => api.cliSignIn().catch((e) => { $('prefs-error').textContent = clean(e); }));
    $('prefs-close').addEventListener('click', () => api.closePanel());

    function renderPrefs(s) {
        $('version').textContent = s.appVersion;
        const u = s.updates || {};
        const pol = u.policy || {};
        $('pref-updates').checked = s.prefs.updates !== false;
        $('pref-updates').disabled = pol.updates === 'off' && pol.source && pol.source.updates !== 'you';
        const notes = [];
        if (pol.updates === 'off' && pol.source && pol.source.updates !== 'you') notes.push(`Turned off by ${pol.source.updates === 'computer' ? 'your computer’s policy' : `the ${pol.source.updates} workspace`}.`);
        if (pol.updates === 'notify') notes.push('You are told about updates; your administrator installs them.');
        if (pol.pinned) notes.push(`Pinned to ${pol.pinned} by ${pol.source.pinned === 'computer' ? 'your computer’s policy' : `the ${pol.source.pinned} workspace`}.`);
        if (s.platform === 'darwin') notes.push('On macOS this build tells you about updates and links to the download (installing them needs a signed app).');
        if (u.decision && u.decision.reason && u.status !== 'idle') notes.push(`Last check: ${u.status}${u.available ? ` (${u.available})` : ''}, ${u.decision.reason}.`);
        if (u.error) notes.push(`Last check failed: ${u.error}`);
        $('updates-note').textContent = notes.join(' ');
        $('install-update').hidden = u.status !== 'downloaded';
        $('pref-offline').checked = s.prefs.offlineCache !== false;
        $('pref-offline').disabled = s.machine.offline_cache === false || !s.cacheAvailable;
        $('offline-note').textContent = !s.cacheAvailable
            ? 'Not available: this computer has no keychain to encrypt the copies with, so nothing is kept.'
            : s.machine.offline_cache === false ? 'Turned off by your computer’s policy.'
                : 'Kept per workspace, encrypted with your keychain, deleted when you sign out. A workspace’s administrator can turn it off.';
        $('pref-shortcut').checked = !!(s.prefs.globalShortcut && s.prefs.globalShortcut.enabled);
        $('pref-shortcut').disabled = s.machine.global_shortcut === false;
        if (document.activeElement !== $('pref-accelerator')) $('pref-accelerator').value = (s.prefs.globalShortcut && s.prefs.globalShortcut.accelerator) || '';
        $('install-cli').disabled = !s.cli || s.machine.cli_install === false;
        const ws = s.workspaces.find((w) => w.name === s.active);
        $('cli-sign-in').disabled = !s.cli || !ws || !ws.signedIn;
        $('cli-note').textContent = !s.cli ? 'This build has no command line inside.'
            : ws ? `“Sign in the command line too” signs cairn in to ${ws.name} as you, with one click; its tokens go to your keychain.` : '';
    }

    // ---- state ----------------------------------------------------------------------------------------------
    api.onState((s) => {
        state = s;
        const mode = s.panel ? s.panel.mode : null;
        for (const id of ['add', 'offline', 'prefs']) $(id).hidden = id !== mode;
        if (mode === 'add') {
            const prompt = s.panel.prompt;
            $('add-title').textContent = prompt ? `Add “${prompt.workspace}”?` : s.workspaces.length ? 'Add a workspace' : 'Welcome to Cairn';
            $('add-intro').textContent = prompt
                ? `A link asks to open ${prompt.route} in a workspace called “${prompt.workspace}”, which is not set up here. Add its address to open it.`
                : 'Your organisation’s Cairn address. You sign in there as usual; the command line (cairn) sees the same workspaces.';
            if (prompt && !$('name').value) $('name').value = prompt.workspace;
            $('add-cancel').hidden = !s.workspaces.length;
            if (s.profilesError) $('add-error').textContent = s.profilesError;
            if (shownMode !== 'add') $('url').focus();
        } else if (mode === 'offline') {
            renderOffline(s);
            if (shownMode !== 'offline') $('retry').focus();
        } else if (mode === 'prefs') {
            renderPrefs(s);
        }
        if (mode !== 'offline' && viewing) closeViewer();
        shownMode = mode;
    });
})();
