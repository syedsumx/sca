'use strict';
/*
 * The application menu: the platform's standard menus and shortcuts (Edit, View, Window, and the app menu on macOS)
 * plus Workspace (switch with Ctrl/Cmd+1…9, add, sign in the command line, sign out, remove) and Go (back, forward,
 * Ask Cairn…). `do(action, arg)` carries the choices out; it is a template so tests can read it without Electron.
 */

function menuTemplate({ platform = process.platform, workspaces = [], active = null, appName = 'Cairn' }, act) {
    const mac = platform === 'darwin';
    const on = (action, arg) => () => act(action, arg);
    const has = !!active;
    const settings = { label: mac ? 'Settings…' : 'Settings', accelerator: 'CommandOrControl+,', click: on('prefs') };
    const template = [];
    if (mac) {
        template.push({ label: appName, submenu: [
            { role: 'about' }, { label: 'Check for Updates…', click: on('check-updates') }, { type: 'separator' },
            settings, { type: 'separator' }, { role: 'services' }, { type: 'separator' },
            { role: 'hide' }, { role: 'hideOthers' }, { role: 'unhide' }, { type: 'separator' }, { role: 'quit' },
        ] });
    }
    template.push({ label: mac ? 'File' : '&File', submenu: [
        { label: 'Add Workspace…', accelerator: 'CommandOrControl+Shift+N', click: on('add') },
        ...(mac ? [] : [{ type: 'separator' }, settings]),
        { type: 'separator' },
        mac ? { role: 'close' } : { role: 'quit' },
    ] });
    template.push({ label: mac ? 'Edit' : '&Edit', submenu: [
        { role: 'undo' }, { role: 'redo' }, { type: 'separator' }, { role: 'cut' }, { role: 'copy' }, { role: 'paste' },
        ...(mac ? [{ role: 'pasteAndMatchStyle' }] : []), { role: 'delete' }, { role: 'selectAll' },
        ...(mac ? [{ type: 'separator' }, { label: 'Speech', submenu: [{ role: 'startSpeaking' }, { role: 'stopSpeaking' }] }] : []),
    ] });
    template.push({ label: mac ? 'View' : '&View', submenu: [
        { label: 'Reload', accelerator: 'CommandOrControl+R', click: on('reload'), enabled: has },
        { type: 'separator' },
        { label: 'Actual Size', accelerator: 'CommandOrControl+0', click: on('zoom', 0), enabled: has },
        { label: 'Zoom In', accelerator: 'CommandOrControl+Plus', click: on('zoom', 1), enabled: has },
        { label: 'Zoom In', accelerator: 'CommandOrControl+=', click: on('zoom', 1), visible: false, acceleratorWorksWhenHidden: true },
        { label: 'Zoom Out', accelerator: 'CommandOrControl+-', click: on('zoom', -1), enabled: has },
        { type: 'separator' },
        { role: 'togglefullscreen' },
        ...(process.env.CAIRN_DESKTOP_DEVTOOLS === '1'
            ? [{ label: 'Developer Tools', accelerator: 'Alt+CommandOrControl+I', click: on('devtools') }] : []),
    ] });
    template.push({ label: mac ? 'Workspace' : '&Workspace', submenu: [
        ...workspaces.map((w, i) => ({
            label: `${w.name}${w.offline ? ' (offline)' : ''}`, type: 'radio', checked: w.name === active,
            accelerator: i < 9 ? `CommandOrControl+${i + 1}` : undefined, click: on('switch', w.name),
        })),
        ...(workspaces.length ? [{ type: 'separator' }] : []),
        { label: 'Add Workspace…', click: on('add') },
        { label: 'Sign In the Command Line Too', click: on('cli-sign-in'), enabled: has },
        { type: 'separator' },
        { label: 'Sign Out of This Workspace…', click: on('sign-out'), enabled: has },
        { label: 'Remove This Workspace…', click: on('remove'), enabled: has },
    ] });
    template.push({ label: mac ? 'Go' : '&Go', submenu: [
        { label: 'Back', accelerator: mac ? 'Command+[' : 'Alt+Left', click: on('back'), enabled: has },
        { label: 'Forward', accelerator: mac ? 'Command+]' : 'Alt+Right', click: on('forward'), enabled: has },
        { type: 'separator' },
        { label: 'Ask Cairn…', accelerator: 'CommandOrControl+Shift+A', click: on('ask'), enabled: has },
        { label: 'Home', accelerator: 'CommandOrControl+Shift+H', click: on('home'), enabled: has },
    ] });
    template.push({ role: 'windowMenu' });
    template.push({ role: 'help', submenu: [
        { label: 'Cairn Desktop Help', click: on('help') },
        { label: 'Install Command Line Tool…', click: on('install-cli') },
        ...(mac ? [] : [{ label: 'Check for Updates…', click: on('check-updates') }, { type: 'separator' }, { role: 'about' }]),
    ] });
    return template;
}

module.exports = { menuTemplate };
