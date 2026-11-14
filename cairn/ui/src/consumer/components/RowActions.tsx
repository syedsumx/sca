import { Button, Dropdown, Modal, Space } from 'antd';
import React, { useState } from 'react';

import { useT } from '../i18n';
import { notifyError } from './ErrorNotice';
import { Icon } from './Icons';

export interface RowAction {
    key: string;
    /** The words on the button or menu item ("Edit", "Run now", "Delete"). */
    label: string;
    onSelect: () => void | Promise<unknown>;
    /** Destructive: shown in red, inside the menu, and confirmed first unless `confirm` is false. */
    danger?: boolean;
    /** Ask before acting (default: for a dangerous action). The text is the question; true asks "Delete <name>?". */
    confirm?: boolean | string;
    disabled?: boolean;
}

/**
 * A row's actions, calmly: the main one (Edit, Run now) stays visible; everything else, and every destructive action,
 * is in a "More actions" menu. A destructive action asks first ("Delete orders?" with Delete and Cancel).
 * Accessible names: the menu button is "More actions for <name>", its items are named by their labels.
 */
export function RowActions({ name, primary = [], actions = [], size = 'small' }: {
    /** What the row is called (for the menu button's and the confirmation's words). */
    name: string;
    /** Always visible, as buttons. */
    primary?: RowAction[];
    /** In the "⋯" menu. */
    actions?: RowAction[];
    size?: 'small' | 'middle';
}) {
    const t = useT();
    const [asking, setAsking] = useState<RowAction | null>(null);
    const [busy, setBusy] = useState(false);
    const select = (a: RowAction) => {
        const ask = a.confirm ?? !!a.danger;
        if (ask) setAsking(a);
        else {
            try {
                const done = a.onSelect();
                if (done && typeof (done as Promise<unknown>).catch === 'function') (done as Promise<unknown>).catch(notifyError);
            } catch (e) {
                notifyError(e);
            }
        }
    };
    const confirm = async () => {
        if (!asking) return;
        setBusy(true);
        try {
            await asking.onSelect();
        } catch (e) {
            notifyError(e);                         // never a silent close, as if a refused delete had happened
        } finally {
            setBusy(false);
            setAsking(null);
        }
    };
    const question = asking
        ? typeof asking.confirm === 'string' ? asking.confirm : t('row.deleteConfirm', { name })
        : '';
    return (
        <Space size={4} className="cairn-row-actions" onClick={(e) => e.stopPropagation()}>
            {primary.map((a) => (
                <Button key={a.key} size={size} danger={a.danger} disabled={a.disabled} onClick={() => select(a)}
                    aria-label={`${a.label}: ${name}`}>
                    {a.label}
                </Button>
            ))}
            {actions.length > 0 && (
                <Dropdown trigger={['click']} placement="bottomRight"
                    menu={{
                        items: actions.map((a) => ({ key: a.key, label: a.label, danger: a.danger, disabled: a.disabled })),
                        onClick: ({ key, domEvent }) => {
                            domEvent.stopPropagation();
                            const a = actions.find((x) => x.key === key);
                            if (a) select(a);
                        },
                    }}>
                    <Button size={size} type="text" aria-label={t('row.more', { name })} aria-haspopup="menu"
                        title={t('row.more', { name })} icon={<Icon name="more" size={18} />} />
                </Dropdown>
            )}
            {asking && (
                <Modal open title={question} onCancel={() => setAsking(null)} destroyOnClose width={440}
                    footer={[
                        <Button key="cancel" onClick={() => setAsking(null)}>{t('row.cancel')}</Button>,
                        <Button key="ok" type="primary" danger={asking.danger} loading={busy} onClick={() => void confirm()}>
                            {asking.label}
                        </Button>,
                    ]}>
                    <p style={{ margin: 0 }}>{asking.danger ? t('row.deleteHint') : null}</p>
                </Modal>
            )}
        </Space>
    );
}

/** The API a page reads, for developers, behind a small collapsed "API" disclosure. */
export function ApiDisclosure({ paths, style }: { paths: string[]; style?: React.CSSProperties }) {
    const t = useT();
    const shown = paths.filter(Boolean);
    if (!shown.length) return null;
    return (
        <details className="cairn-api" style={{ fontSize: 12, ...style }}>
            <summary aria-label={t('api.label')} style={{ cursor: 'pointer', display: 'inline-block', color: 'var(--cairn-text-muted, #475569)' }}>
                {t('api.show')}
            </summary>
            <div style={{ marginTop: 4, display: 'flex', flexDirection: 'column', gap: 2 }}>
                {shown.map((p) => <code key={p} className="cairn-path">{p}</code>)}
            </div>
        </details>
    );
}
