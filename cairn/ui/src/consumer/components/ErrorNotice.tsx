import { Alert, Button, Space, message } from 'antd';
import React, { useState } from 'react';
import { useHistory } from 'react-router-dom';
import styled from 'styled-components';

import { useT } from '../i18n';
import { ErrorContext, PresentedError, describeError, presentError } from '../utils/errors';
import { Icon } from './Icons';
import { tokens } from './tokens';

export { describeError } from '../utils/errors';

/** A failed action as a toast in the presenter's words (what happened and what to do). */
export function notifyError(error: unknown, ctx: ErrorContext = {}): void {
    void message.error(describeError(error, ctx));
}

const Neutral = styled.div`
    display: flex;
    gap: 12px;
    align-items: flex-start;
    padding: 14px 16px;
    border: 1px dashed ${tokens.color.borderStrong};
    border-radius: ${tokens.radius.md};
    background: ${tokens.color.surfaceSunken};
    color: ${tokens.color.textSubtle};
    .nc-icon {
        display: inline-grid;
        place-items: center;
        width: 32px;
        height: 32px;
        border-radius: 50%;
        background: ${tokens.color.surface};
        border: 1px solid ${tokens.color.border};
        color: ${tokens.color.textMuted};
        flex: none;
    }
    .nc-title {
        font-weight: 600;
        color: ${tokens.color.text};
    }
`;

/** Where a not-connected service is connected: a page of the workspace (through the router when there is one), or the
 * operator manual. */
function ConnectLink({ to, label }: { to: string; label: string }) {
    const history = useHistory() as ReturnType<typeof useHistory> | undefined;
    if (to.startsWith('/docs/') || !history) {
        return <a href={to} target={to.startsWith('/docs/') ? '_blank' : undefined} rel="noreferrer">{label}</a>;
    }
    return (
        <a href={history.createHref({ pathname: to.split('?')[0], search: to.includes('?') ? `?${to.split('?')[1]}` : '' })}
            onClick={(e) => {
                e.preventDefault();
                history.push(to);
            }}>
            {label}
        </a>
    );
}

/**
 * A service or integration this Cairn has not connected yet (its API is not mounted): a calm, neutral state with where
 * to connect it, never a red error. Access is unchanged: nothing is shown that the API did not return.
 */
export function NotConnected({ presented, style }: { presented: PresentedError; style?: React.CSSProperties }) {
    return (
        <Neutral role="status" data-error-kind="notConnected" style={style}>
            <span className="nc-icon"><Icon name="plug" size={16} /></span>
            <div>
                <div className="nc-title">{presented.title}</div>
                <div>
                    {presented.hint}
                    {presented.connect && <>{' '}<ConnectLink to={presented.connect.to} label={presented.connect.label} /></>}
                </div>
            </div>
        </Neutral>
    );
}

/** Any error from Cairn as a sentence, what to do about it, and the technical detail under "Details" with a copy button. */
export function ErrorNotice({ error, style, ...ctx }: ErrorContext & { error: unknown; style?: React.CSSProperties }) {
    const t = useT();
    const p = presentError(error, t, ctx);
    const [open, setOpen] = useState(false);
    const [copied, setCopied] = useState(false);
    const copy = async () => {
        try {
            await navigator.clipboard.writeText(p.detail);
            setCopied(true);
        } catch {
            setCopied(false);
        }
    };
    if (p.kind === 'notConnected') return <NotConnected presented={p} style={style} />;
    const warn = p.kind === 'forbidden' || p.kind === 'unconfigured' || p.kind === 'invalid' || p.kind === 'rate' ||
        p.kind === 'conflict';
    return (
        <Alert
            type={warn ? 'warning' : 'error'}
            showIcon
            style={style}
            data-error-kind={p.kind}
            message={p.title}
            description={
                <div>
                    <div>
                        {p.hint}
                        {p.docs && (
                            <>
                                {' '}
                                <a href={p.docs} target="_blank" rel="noreferrer">{t('err.unconfigured.docs')}</a>
                            </>
                        )}
                    </div>
                    {p.detail && (
                        <div style={{ marginTop: 6 }}>
                            <Space size={4}>
                                <Button type="link" size="small" style={{ padding: 0 }} aria-expanded={open} onClick={() => setOpen(!open)}>
                                    {t('err.details')}
                                </Button>
                                {open && (
                                    <Button size="small" type="text" onClick={() => void copy()}>
                                        {copied ? t('err.copied') : t('err.copyDetails')}
                                    </Button>
                                )}
                            </Space>
                            {open && (
                                <pre style={{ whiteSpace: 'pre-wrap', margin: '4px 0 0', fontSize: 12, maxHeight: 220, overflow: 'auto' }}>
                                    {p.detail}
                                </pre>
                            )}
                        </div>
                    )}
                </div>
            }
        />
    );
}
