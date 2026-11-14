'use strict';
// The top bar: the workspace switcher (a native menu), its state, messages, the unread count and Ask Cairn.
(function () {
    const $ = (id) => document.getElementById(id);
    const api = window.cairnDesktop;

    $('switcher').addEventListener('click', () => api.workspaceMenu());
    $('ask').addEventListener('click', () => api.ask());

    api.onState((s) => {
        const ws = s.workspaces.find((w) => w.name === s.active);
        $('ws-name').textContent = ws ? ws.name : 'No workspace';
        $('dot').className = `dot ${ws ? ws.status : ''}`;
        $('ask').disabled = !ws || ws.status !== 'online';
        const status = $('status');
        status.textContent = '';
        status.className = '';
        if (ws && ws.status === 'offline') {
            status.className = 'chip offline';
            status.textContent = 'Offline';
        } else if (ws && ws.inSignIn) {
            status.className = 'chip signin';
            status.textContent = `Signing in at ${ws.inSignIn.replace(/^https:\/\//, '')}`;
        } else if (ws && ws.signedIn === false) {
            status.className = 'chip signin';
            status.textContent = 'Not signed in';
        }
        const unread = s.workspaces.reduce((n, w) => n + (w.unread || 0), 0);
        $('unread').textContent = unread ? String(unread > 99 ? '99+' : unread) : '';
        $('unread').setAttribute('aria-label', unread ? `${unread} unread notifications` : '');
        const t = $('toast');
        t.textContent = s.toast ? s.toast.text : '';
        t.className = s.toast ? s.toast.kind : '';
        t.title = s.toast ? s.toast.text : '';
    });
})();
