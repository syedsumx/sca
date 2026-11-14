import { Tag, Tooltip } from 'antd';
import React from 'react';
import { Link } from 'react-router-dom';

import { isKnownState, StatusBadge } from '../components/StatusBadge';
import { Muted } from '../components/tokens';
import { Schedule, When, exactTime, looksLikeCron } from '../utils/time';

/** Column names people read, for API fields whose own names are a developer's (L150). */
const COLUMN_TITLES: Record<string, string> = {
    user_id: 'Owner', owner_id: 'Owner', last_updated_timestamp: 'Updated', latest_updated_timestamp: 'Updated',
    creation_timestamp: 'Created', updated_ms: 'Updated', created_ms: 'Created', dataset_urn: 'Dataset', dataset: 'Dataset',
    asset_urn: 'Asset',
};

/** A column's title: a person's word for the field where there is one, else the field made readable. */
export function columnTitle(key: string): string {
    return COLUMN_TITLES[key] ?? humanize(key);
}

export function humanize(key: string): string {
    const s = key.replace(/_ms$/, '').replace(/_usd$/, ' (USD)').replace(/_/g, ' ');
    return s.charAt(0).toUpperCase() + s.slice(1);
}

export function shortUrn(v: string): string {
    const m = /^urn:li:(?:corpuser|corpGroup):(.+)$/.exec(v);
    if (m) return m[1];
    const d = /^urn:li:dataset:\(urn:li:dataPlatform:[^,]+,([^,]+),/.exec(v);
    if (d) return d[1];
    return v.length > 64 ? `${v.slice(0, 61)}…` : v;
}

/** A value as a person reads it: times, owners, states, lists and nested objects formatted by field name and type. */
export function formatValue(key: string, v: unknown, full = false): React.ReactNode {
    if (v === null || v === undefined || v === '') return <Muted>—</Muted>;
    // On/off settings read as a state (one status vocabulary, L150); other flags as plain words.
    if (typeof v === 'boolean') {
        if (/(^|_)(enabled|active|paused|disabled|healthy|ok)$/.test(key)) {
            const on = /(paused|disabled)$/.test(key) ? !v : v;
            return <StatusBadge state={on ? 'enabled' : 'disabled'} plain />;
        }
        return v ? 'Yes' : 'No';
    }
    if (typeof v === 'number') {
        if (/(_ms|_at|timestamp|_time)$/.test(key) && v > 1e11) {
            return full ? <><When value={v} /> <Muted>({exactTime(v)})</Muted></> : <When value={v} />;
        }
        if (/(usd|cost|amount|spent)/.test(key)) return `$${v.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
        if (/bytes$/.test(key)) return v >= 1e9 ? `${(v / 1e9).toFixed(1)} GB` : v >= 1e6 ? `${(v / 1e6).toFixed(1)} MB` : `${v} B`;
        return v.toLocaleString();
    }
    if (typeof v === 'string') {
        if (/^\d{4}-\d{2}-\d{2}T/.test(v) && !Number.isNaN(Date.parse(v))) {
            return full ? <><When value={v} /> <Muted>({exactTime(v)})</Muted></> : <When value={v} />;
        }
        // Run, health and approval states in the one status vocabulary (StatusBadge); kinds and tiers stay tags.
        // A result, outcome or decision is a badge only when it is a state word; otherwise it is the value itself.
        const stateField = /(^|_)(status|state|health|phase)$/.test(key);
        if ((stateField || (/(^|_)(decision|outcome|result)$/.test(key) && isKnownState(v))) && v.length < 32) {
            return <StatusBadge state={v.toLowerCase()} plain={!full} />;
        }
        if (/(^|_)(kind|type|mode|severity|tier)$/.test(key) && v.length < 32) {
            return <Tag>{v}</Tag>;
        }
        // A schedule in words, with the cron itself on hover.
        if (/(^|_)(schedule|cron)(_expr|_expression)?$/.test(key) && looksLikeCron(v)) {
            return full ? <><Schedule cron={v} /> <Muted><code>{v}</code></Muted></> : <Schedule cron={v} />;
        }
        // A dataset is shown by its name and opens its asset page; other URNs by their short name.
        if (v.startsWith('urn:li:dataset:')) {
            return <Tooltip title={v}><Link to={`/asset/${encodeURIComponent(v)}`}>{shortUrn(v)}</Link></Tooltip>;
        }
        if (v.startsWith('urn:li:')) return <Tooltip title={v}>{shortUrn(v)}</Tooltip>;
        if (full && (v.includes('\n') || v.length > 120)) {
            return <pre style={{ whiteSpace: 'pre-wrap', margin: 0, fontSize: 12 }}>{v}</pre>;
        }
        return full ? v : v.length > 80 ? <Tooltip title={v}>{`${v.slice(0, 77)}…`}</Tooltip> : v;
    }
    if (Array.isArray(v)) {
        if (v.length === 0) return <Muted>none</Muted>;
        if (v.every((x) => typeof x === 'string' || typeof x === 'number')) {
            const shown = full ? v : v.slice(0, 4);
            return (
                <span>
                    {shown.map((x) => (
                        <Tag key={String(x)} style={{ marginBottom: 2 }}>{typeof x === 'string' ? shortUrn(x) : x}</Tag>
                    ))}
                    {!full && v.length > 4 && <Muted>+{v.length - 4}</Muted>}
                </span>
            );
        }
        if (!full) return `${v.length} item${v.length === 1 ? '' : 's'}`;
        return <pre style={{ whiteSpace: 'pre-wrap', margin: 0, fontSize: 12 }}>{JSON.stringify(v, null, 2)}</pre>;
    }
    if (typeof v === 'object') {
        // A status object (a stream's {status, error, lag_ms, rows}): its state as a badge, the error on hover, and
        // the numbers beside it, so a failing or lagging row shows in the table.
        const o = v as Record<string, unknown>;
        if (!full && typeof o.status === 'string' && o.status.length < 32) {
            const extra = Object.entries(o).filter(([k, x]) => k !== 'status' && typeof x === 'number')
                .slice(0, 3).map(([k, x]) => `${k.replace(/_/g, ' ')} ${(x as number).toLocaleString('en-US')}`).join(' · ');
            const badge = <StatusBadge state={o.status.toLowerCase()} plain />;
            return (
                <span>
                    {typeof o.error === 'string' && o.error ? <Tooltip title={o.error}><span>{badge}</span></Tooltip> : badge}
                    {extra && <Muted> {extra}</Muted>}
                </span>
            );
        }
        if (!full) {
            const keys = Object.keys(v as object);
            return <Muted>{keys.slice(0, 3).join(', ')}{keys.length > 3 ? '…' : ''}</Muted>;
        }
        return <pre style={{ whiteSpace: 'pre-wrap', margin: 0, fontSize: 12 }}>{JSON.stringify(v, null, 2)}</pre>;
    }
    return String(v);
}
