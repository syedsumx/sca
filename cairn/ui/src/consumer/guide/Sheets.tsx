import { Button, Modal, Space, Switch, message } from 'antd';
import React from 'react';
import styled from 'styled-components';

import { Muted, tokens } from '../components/tokens';
import { MessageKey, useT } from '../i18n';
import { useGuide } from './GuideProvider';
import { SidebarPrefs, toggleHidden, togglePin } from './nav';

const isMac = () => typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || '');

/** The shortcuts the workspace answers, in the order the sheet lists them. */
export const SHORTCUTS: { keys: (mac: boolean) => string[]; what: MessageKey }[] = [
    { keys: (mac) => [mac ? '⌘ K' : 'Ctrl K'], what: 'shortcuts.palette' },
    { keys: () => ['/'], what: 'shortcuts.search' },
    { keys: (mac) => [mac ? '⌘ J' : 'Ctrl J'], what: 'shortcuts.assistant' },
    { keys: () => ['?'], what: 'shortcuts.sheet' },
    { keys: () => ['g', 'h'], what: 'shortcuts.getStarted' },
    { keys: () => ['↑', '↓', 'Enter'], what: 'shortcuts.move' },
    { keys: () => ['Esc'], what: 'shortcuts.close' },
];

const Table = styled.table`
    width: 100%;
    border-collapse: collapse;
    th,
    td {
        text-align: left;
        padding: 8px 6px;
        border-bottom: 1px solid ${tokens.color.border};
    }
    th {
        font-size: 12px;
        color: ${tokens.color.textMuted};
        text-transform: uppercase;
        letter-spacing: 0.04em;
    }
    kbd {
        display: inline-block;
        min-width: 22px;
        text-align: center;
        font: 12px ui-monospace, SFMono-Regular, Menlo, monospace;
        border: 1px solid ${tokens.color.border};
        border-bottom-width: 2px;
        border-radius: 5px;
        padding: 1px 6px;
        margin-right: 4px;
        background: ${tokens.color.surfaceAlt};
    }
`;

/** "?": the keyboard shortcuts. */
export function ShortcutsSheet() {
    const g = useGuide();
    const t = useT();
    const mac = isMac();
    return (
        <Modal open={g.shortcuts} onCancel={() => g.setShortcuts(false)} footer={null} title={t('shortcuts.title')}>
            <Table>
                <thead>
                    <tr><th scope="col">{t('shortcuts.keys')}</th><th scope="col">{t('shortcuts.action')}</th></tr>
                </thead>
                <tbody>
                    {SHORTCUTS.map((s) => (
                        <tr key={s.what}>
                            <td>{s.keys(mac).map((k) => <kbd key={k}>{k}</kbd>)}</td>
                            <td>{t(s.what)}</td>
                        </tr>
                    ))}
                </tbody>
            </Table>
        </Modal>
    );
}

/** A success message with an Undo button (for changes the API can take back). */
export function undoToast(text: string, undoLabel: string, onUndo: () => void | Promise<void>, undoneText?: string) {
    const key = `undo-${Date.now()}`;
    message.success({
        key,
        duration: 8,
        content: (
            <span role="status">
                {text}{' '}
                <Button type="link" size="small" onClick={async () => {
                    message.destroy(key);
                    await onUndo();
                    if (undoneText) message.info(undoneText);
                }}>
                    {undoLabel}
                </Button>
            </span>
        ),
    });
}

const Rows = styled.ul`
    list-style: none;
    margin: 0;
    padding: 0;
    li {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 6px 0;
        border-bottom: 1px solid ${tokens.color.border};
    }
    .name {
        flex: 1;
    }
`;

/** "Customise sidebar": pin and hide entries, show everything; kept in the profile. */
export function SidebarCustomiser({ entries }: { entries: { to: string; label: string }[] }) {
    const g = useGuide();
    const t = useT();
    const prefs: SidebarPrefs = g.ui.sidebar;
    const set = (next: SidebarPrefs) => void g.save({ sidebar: next });
    return (
        <Modal open={g.customise} onCancel={() => g.setCustomise(false)} title={t('nav.customise')}
            footer={
                <Space>
                    <Button onClick={() => set({ pinned: [], hidden: [], show_all: false })}>{t('nav.reset')}</Button>
                    <Button type="primary" onClick={() => g.setCustomise(false)}>{t('nav.done')}</Button>
                </Space>
            }>
            <p><Muted>{t('nav.customiseIntro')}</Muted></p>
            <label style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 12 }}>
                <Switch checked={prefs.show_all} onChange={(on) => set({ ...prefs, show_all: on })} />
                {t('nav.showEverything')}
            </label>
            <Rows>
                {entries.map((e) => {
                    const pinned = prefs.pinned.includes(e.to);
                    const hidden = prefs.hidden.includes(e.to);
                    return (
                        <li key={e.to}>
                            <span className="name">{e.label}</span>
                            <Button size="small" aria-pressed={pinned} onClick={() => set(togglePin(prefs, e.to))}>
                                {t(pinned ? 'nav.unpin' : 'nav.pin', { page: e.label })}
                            </Button>
                            <Button size="small" aria-pressed={hidden} onClick={() => {
                                set(toggleHidden(prefs, e.to));
                                if (!hidden) {
                                    undoToast(t('nav.hide', { page: e.label }), t('toast.undo'),
                                        () => g.save({ sidebar: { hidden: prefs.hidden } }), t('toast.undone'));
                                }
                            }}>
                                {t(hidden ? 'nav.unhide' : 'nav.hide', { page: e.label })}
                            </Button>
                        </li>
                    );
                })}
            </Rows>
        </Modal>
    );
}
