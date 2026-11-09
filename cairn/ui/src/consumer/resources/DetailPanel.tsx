/** A row's details as a labelled, formatted panel: times, durations, people, datasets as links, states as chips. */
import { Button, Descriptions, Space, Tag, Tooltip } from 'antd';
import React, { useState } from 'react';
import { Link } from 'react-router-dom';

import { assetPath } from '../components/Assets';
import { Muted } from '../components/tokens';
import { useT } from '../i18n';
import { actorName } from '../utils/format';
import { durationParts } from './forms';
import { formatValue, humanize, shortUrn } from './format';
import { unitText } from './FormRenderer';

type Row = Record<string, unknown>;

const DURATION_KEY = /(?:^|_)(seconds|minutes|hours|days)$/;

function Value({ k, v, depth, secret, format }: {
    k: string; v: unknown; depth: number; secret: boolean; format: (k: string, v: unknown, full?: boolean) => React.ReactNode;
}) {
    const t = useT();
    const [shown, setShown] = useState(false);
    if (secret && v !== null && v !== undefined && v !== '') {
        return shown ? (
            <span>{typeof v === 'string' ? v : JSON.stringify(v)} <Button size="small" type="link" onClick={() => setShown(false)}>{t('detail.hide')}</Button></span>
        ) : (
            <span><Muted>••••••</Muted> <Button size="small" type="link" onClick={() => setShown(true)}>{t('detail.reveal')}</Button></span>
        );
    }
    if (typeof v === 'string' && v.startsWith('urn:li:dataset:')) {
        return <Tooltip title={v}><Link to={assetPath(v)}>{shortUrn(v)}</Link></Tooltip>;
    }
    if (typeof v === 'string' && /^urn:li:corp(user|Group):/.test(v)) {
        return <Tooltip title={v}><span>{actorName(v)}{v.includes('corpGroup') && <Muted> ({t('detail.group')})</Muted>}</span></Tooltip>;
    }
    if (typeof v === 'number' && DURATION_KEY.test(k)) {
        const unit = DURATION_KEY.exec(k)![1];
        const more = durationParts(v, unit);
        return <span>{v.toLocaleString()} {unitText(t, unit, v)}{more && <Muted> ({more.n} {unitText(t, more.unit, more.n)})</Muted>}</span>;
    }
    if (typeof v === 'boolean') return v ? <Tag color="green">{t('detail.yes')}</Tag> : <Tag>{t('detail.no')}</Tag>;
    if (v && typeof v === 'object' && !Array.isArray(v) && depth < 2 && Object.keys(v).length <= 16) {
        const entries = Object.entries(v as Row);
        if (!entries.length) return <Muted>{t('detail.none')}</Muted>;
        return (
            <Descriptions column={1} size="small" bordered labelStyle={{ width: 140 }}>
                {entries.map(([sk, sv]) => (
                    <Descriptions.Item key={sk} label={humanize(sk)}>
                        <Value k={sk} v={sv} depth={depth + 1} secret={false} format={format} />
                    </Descriptions.Item>
                ))}
            </Descriptions>
        );
    }
    if (Array.isArray(v) && v.length && v.length <= 12 && v.every((x) => x && typeof x === 'object' && !Array.isArray(x)) && depth < 2) {
        return (
            <Space direction="vertical" style={{ width: '100%' }} size={6}>
                {v.map((x, i) => <Value key={i} k={k} v={x} depth={depth + 1} secret={false} format={format} />)}
            </Space>
        );
    }
    if (Array.isArray(v) && v.every((x) => typeof x === 'string')) {
        if (!v.length) return <Muted>{t('detail.none')}</Muted>;
        return (
            <span>
                {(v as string[]).map((x) => (x.startsWith('urn:li:dataset:')
                    ? <Tag key={x}><Link to={assetPath(x)}>{shortUrn(x)}</Link></Tag>
                    : <Tag key={x} style={{ marginBottom: 2 }}>{shortUrn(x)}</Tag>))}
            </span>
        );
    }
    return <>{format(k, v, true)}</>;
}

/** The details, then "View JSON" (with a copy button) for the record as the API returned it. */
export function DetailPanel({ row, secret = [], format = formatValue }: {
    row: Row; secret?: string[]; format?: (k: string, v: unknown, full?: boolean) => React.ReactNode;
}) {
    const t = useT();
    const [json, setJson] = useState(false);
    const [copied, setCopied] = useState(false);
    const text = JSON.stringify(Object.fromEntries(Object.entries(row).map(([k, v]) => [k, secret.includes(k) && v ? '••••••' : v])), null, 2);
    return (
        <div>
            <Descriptions column={1} size="small" bordered labelStyle={{ width: 170 }}>
                {Object.entries(row).map(([k, v]) => (
                    <Descriptions.Item key={k} label={humanize(k)}>
                        <Value k={k} v={v} depth={0} secret={secret.includes(k)} format={format} />
                    </Descriptions.Item>
                ))}
            </Descriptions>
            <Space style={{ marginTop: 12 }}>
                <Button size="small" aria-expanded={json} onClick={() => setJson(!json)}>{json ? t('detail.hideJson') : t('detail.viewJson')}</Button>
                {json && (
                    <Button size="small" onClick={() => {
                        navigator.clipboard?.writeText(text).then(() => setCopied(true), () => setCopied(false));
                    }}>{copied ? t('detail.copied') : t('detail.copy')}</Button>
                )}
            </Space>
            {json && <pre aria-label={t('detail.json')} style={{ whiteSpace: 'pre-wrap', fontSize: 12, marginTop: 8, maxHeight: 360, overflow: 'auto' }}>{text}</pre>}
        </div>
    );
}
