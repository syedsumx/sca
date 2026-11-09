import { Alert, Button, Space } from 'antd';
import React, { useState } from 'react';

import { useT } from '../i18n';
import { ErrorContext, presentError } from '../utils/errors';

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
