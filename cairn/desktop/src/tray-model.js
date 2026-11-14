'use strict';
/*
 * The tray (menu bar) icon's state, without Electron: the unread count, its tooltip, and its menu (switch
 * workspace, Ask Cairn…, show, quit). The app turns the `items` into a native menu.
 */

function badge(n) {
    if (!n) return '';
    return n > 99 ? '99+' : String(n);
}

/**
 * workspaces: [{name, unread, offline, signedIn}]; active: name. → {count, label, tooltip, items}. Items are
 * {id, label, type?, checked?, enabled?}; ids: `show`, `ask`, `switch:<name>`, `add`, `quit`.
 */
function trayModel(workspaces = [], active = null) {
    const count = workspaces.reduce((n, w) => n + (Number.isInteger(w.unread) && w.unread > 0 ? w.unread : 0), 0);
    const lines = workspaces.map((w) => {
        const state = w.offline ? 'offline' : w.signedIn === false ? 'not signed in'
            : w.unread ? `${w.unread} unread` : 'no unread';
        return `${w.name}: ${state}`;
    });
    const tooltip = ['Cairn', ...lines].join('\n');
    const items = [
        { id: 'show', label: 'Show Cairn' },
        { id: 'ask', label: 'Ask Cairn…', enabled: !!active },
        { type: 'separator' },
        ...workspaces.map((w, i) => ({
            id: `switch:${w.name}`, type: 'radio', checked: w.name === active,
            label: `${w.name}${w.unread ? ` (${badge(w.unread)})` : ''}${w.offline ? ' (offline)' : ''}`,
            accelerator: i < 9 ? `CommandOrControl+${i + 1}` : undefined,
        })),
        { id: 'add', label: 'Add workspace…' },
        { type: 'separator' },
        { id: 'quit', label: 'Quit Cairn' },
    ];
    return { count, label: badge(count), tooltip, items };
}

module.exports = { trayModel, badge };
