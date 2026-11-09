import { Tag, Tooltip } from 'antd';
import React from 'react';

import { Muted } from '../components/tokens';

export const STATUS_COLORS: Record<string, string> = {
    running: 'green', ok: 'green', ready: 'green', active: 'green', approved: 'green', finished: 'green', succeeded: 'green',
    healthy: 'green', deployed: 'green', enabled: 'green', sent: 'green', accepted: 'green', granted: 'green', passed: 'green',
    pending: 'gold', proposed: 'gold', draft: 'gold', queued: 'gold', waiting: 'gold', starting: 'gold', review: 'gold',
    paused: 'default', stopped: 'default', idle: 'default', disabled: 'default', expired: 'default',
    failed: 'red', error: 'red', rejected: 'red', revoked: 'red', denied: 'red', failing: 'red', unhealthy: 'red',
};

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

export function when(ms: number): string {
    const d = new Date(ms);
    const diff = Date.now() - ms;
    const abs = Math.abs(diff);
    const rel =
        abs < 60_000 ? 'just now' : abs < 3_600_000 ? `${Math.round(abs / 60_000)} min` : abs < 86_400_000
            ? `${Math.round(abs / 3_600_000)} h` : `${Math.round(abs / 86_400_000)} d`;
    const label = rel === 'just now' ? rel : diff >= 0 ? `${rel} ago` : `in ${rel}`;
    return `${label} · ${d.toLocaleString()}`;
}

/** A value as a person reads it: times, owners, states, lists and nested objects formatted by field name and type. */
export function formatValue(key: string, v: unknown, full = false): React.ReactNode {
    if (v === null || v === undefined || v === '') return <Muted>—</Muted>;
    if (typeof v === 'boolean') return v ? <Tag color="green">yes</Tag> : <Tag>no</Tag>;
    if (typeof v === 'number') {
        if (/(_ms|_at|timestamp|_time)$/.test(key) && v > 1e11) {
            const [rel, abs] = when(v).split(' · ');
            return full ? `${rel} (${abs})` : <Tooltip title={abs}>{rel}</Tooltip>;
        }
        if (/(usd|cost|amount|spent)/.test(key)) return `$${v.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
        if (/bytes$/.test(key)) return v >= 1e9 ? `${(v / 1e9).toFixed(1)} GB` : v >= 1e6 ? `${(v / 1e6).toFixed(1)} MB` : `${v} B`;
        return v.toLocaleString();
    }
    if (typeof v === 'string') {
        if (/^\d{4}-\d{2}-\d{2}T/.test(v) && !Number.isNaN(Date.parse(v))) {
            const [rel, abs] = when(Date.parse(v)).split(' · ');
            return full ? `${rel} (${abs})` : <Tooltip title={abs}>{rel}</Tooltip>;
        }
        if (/(^|_)(status|state|kind|type|mode|severity|tier|decision|outcome)$/.test(key) && v.length < 32) {
            return <Tag color={STATUS_COLORS[v.toLowerCase()] ?? 'blue'}>{v}</Tag>;
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
        if (!full) {
            const keys = Object.keys(v as object);
            return <Muted>{keys.slice(0, 3).join(', ')}{keys.length > 3 ? '…' : ''}</Muted>;
        }
        return <pre style={{ whiteSpace: 'pre-wrap', margin: 0, fontSize: 12 }}>{JSON.stringify(v, null, 2)}</pre>;
    }
    return String(v);
}
