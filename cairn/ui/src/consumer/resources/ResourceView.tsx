import { Alert, Button, Descriptions, Drawer, Empty, Input, Modal, Popconfirm, Space, Table, Tag, Tooltip, Typography } from 'antd';
import React, { useMemo, useState } from 'react';
import styled from 'styled-components';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import { Muted, tokens } from '../components/tokens';
import { CopyAsCode } from './IacPanel';
import type { ResourceAction, ResourceSpec } from './types';

type Row = Record<string, unknown>;

const STATUS_COLORS: Record<string, string> = {
    running: 'green', ok: 'green', ready: 'green', active: 'green', approved: 'green', finished: 'green', succeeded: 'green',
    healthy: 'green', deployed: 'green', enabled: 'green', sent: 'green', accepted: 'green', granted: 'green', passed: 'green',
    pending: 'gold', proposed: 'gold', draft: 'gold', queued: 'gold', waiting: 'gold', starting: 'gold', review: 'gold',
    paused: 'default', stopped: 'default', idle: 'default', disabled: 'default', expired: 'default',
    failed: 'red', error: 'red', rejected: 'red', revoked: 'red', denied: 'red', failing: 'red', unhealthy: 'red',
};

const Wrap = styled.section`
    .cairn-resource-head {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 16px;
        margin-bottom: 14px;
    }
    .cairn-resource-head p {
        margin: 0;
        color: ${tokens.color.textMuted};
        max-width: 760px;
    }
    .ant-table-wrapper {
        background: ${tokens.color.surface};
        border: 1px solid ${tokens.color.border};
        border-radius: ${tokens.radius.md};
        overflow: hidden;
    }
    code.cairn-path {
        font-size: 12px;
        color: ${tokens.color.textMuted};
        background: ${tokens.color.surfaceAlt};
        padding: 1px 6px;
        border-radius: 6px;
    }
`;

export function humanize(key: string): string {
    const s = key.replace(/_ms$/, '').replace(/_usd$/, ' (USD)').replace(/_/g, ' ');
    return s.charAt(0).toUpperCase() + s.slice(1);
}

function shortUrn(v: string): string {
    const m = /^urn:li:corpuser:(.+)$/.exec(v);
    if (m) return m[1];
    const d = /^urn:li:dataset:\(urn:li:dataPlatform:[^,]+,([^,]+),/.exec(v);
    if (d) return d[1];
    return v.length > 64 ? `${v.slice(0, 61)}…` : v;
}

function when(ms: number): string {
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

/** `/x/{name}/run` with the row's fields; null when the row lacks one. */
function fill(path: string, row: Row | null): string | null {
    let missing = false;
    const out = path.replace(/\{(\w+)\}/g, (_m, k: string) => {
        const v = row?.[k];
        if (v === undefined || v === null) {
            missing = true;
            return '';
        }
        return encodeURIComponent(String(v));
    });
    return missing ? null : out;
}

function rowKey(spec: ResourceSpec, row: Row, i: number): string {
    for (const k of [spec.rowKey, 'id', 'name', 'key', 'query_id', 'agent_id'].filter(Boolean) as string[]) {
        if (row[k] !== undefined && row[k] !== null) return String(row[k]);
    }
    return String(i);
}

function extract(spec: ResourceSpec, data: unknown): { rows: Row[]; summary: Row | null } {
    if (Array.isArray(data)) return { rows: data as Row[], summary: null };
    if (data && typeof data === 'object') {
        const obj = data as Row;
        if (spec.listKey && Array.isArray(obj[spec.listKey])) {
            const rest = { ...obj };
            delete rest[spec.listKey];
            return { rows: obj[spec.listKey] as Row[], summary: Object.keys(rest).length ? rest : null };
        }
        return { rows: [], summary: obj };
    }
    return { rows: [], summary: null };
}

function ActionDialog({ action, row, onDone, onClose }: {
    action: ResourceAction; row: Row | null; onDone: () => void; onClose: () => void;
}) {
    const { client } = useConsumerApi();
    const path = fill(action.path, row) ?? action.path;
    const [body, setBody] = useState(action.body ? JSON.stringify(action.body, null, 2) : '');
    const [busy, setBusy] = useState(false);
    const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
    const run = async () => {
        let parsed: unknown;
        try {
            parsed = body.trim() ? JSON.parse(body) : undefined;
        } catch {
            setResult({ ok: false, text: 'The request body is not valid JSON.' });
            return;
        }
        setBusy(true);
        try {
            const out = await client.request<unknown>(action.method, path, parsed);
            setResult({ ok: true, text: out === undefined || out === '' ? 'Done.' : JSON.stringify(out, null, 2) });
            onDone();
        } catch (e) {
            setResult({ ok: false, text: (e as Error).message });
        } finally {
            setBusy(false);
        }
    };
    return (
        <Modal open title={action.label} onCancel={onClose} width={640}
            footer={[
                <Button key="close" onClick={onClose}>Close</Button>,
                <Button key="run" type="primary" danger={action.method === 'DELETE'} loading={busy} onClick={run}>
                    {action.label}
                </Button>,
            ]}>
            <p><code className="cairn-path">{action.method} {path}</code></p>
            {action.body !== null && action.body !== undefined && (
                <>
                    <Typography.Text type="secondary">Request</Typography.Text>
                    <Input.TextArea aria-label="Request body" value={body} onChange={(e) => setBody(e.target.value)}
                        autoSize={{ minRows: 3, maxRows: 14 }} style={{ fontFamily: 'ui-monospace, Menlo, monospace', fontSize: 12 }} />
                </>
            )}
            {result && (
                <Alert style={{ marginTop: 12 }} type={result.ok ? 'success' : 'error'} showIcon
                    message={result.ok ? 'Cairn answered' : 'Cairn refused'}
                    description={<pre style={{ whiteSpace: 'pre-wrap', margin: 0, maxHeight: 280, overflow: 'auto', fontSize: 12 }}>{result.text}</pre>} />
            )}
        </Modal>
    );
}

/** One backend area as a table: its list endpoint, each row's details, and its actions, all against the real API. */
export function ResourceView({ spec }: { spec: ResourceSpec }) {
    const q = useDheQuery<unknown>(spec.path ? `resource:${spec.path}` : null, (c, signal) =>
        c.request<unknown>('GET', spec.path as string, undefined, { signal }),
    );
    const [detail, setDetail] = useState<Row | null>(null);
    const [dialog, setDialog] = useState<{ action: ResourceAction; row: Row | null } | null>(null);
    const status = (q.error as { status?: number } | undefined)?.status;
    const denied = status === 401 || status === 403 || /\b40[13]\b/.test(q.error?.message ?? '');
    const { rows, summary } = useMemo(() => extract(spec, q.data), [spec, q.data]);
    const rowActions = (spec.actions ?? []).filter((a) => /\{\w+\}/.test(a.path));
    const pageActions = (spec.actions ?? []).filter((a) => !/\{\w+\}/.test(a.path));
    const norm = (k: string, v: unknown) => (spec.seconds?.includes(k) && typeof v === 'number' ? v * 1000 : v);
    const show = (k: string, v: unknown, full = false) => formatValue(spec.seconds?.includes(k) ? `${k}_ms` : k, norm(k, v), full);
    const columnKeys = spec.columns.length ? spec.columns : Object.keys(rows[0] ?? {}).slice(0, 6);

    const columns = [
        ...columnKeys.map((k, i) => ({
            title: humanize(k),
            dataIndex: k,
            key: k,
            render: (v: unknown, r: Row) =>
                i === 0 ? (
                    <a onClick={() => setDetail(r)} style={{ fontWeight: 600 }}>
                        {typeof v === 'string' || typeof v === 'number' ? shortUrn(String(v)) : show(k, v)}
                    </a>
                ) : show(k, v),
        })),
        ...(rowActions.length
            ? [{
                title: '',
                key: '__actions',
                align: 'right' as const,
                render: (_: unknown, r: Row) => (
                    <Space size={4} wrap>
                        {rowActions.map((a) =>
                            a.method === 'DELETE' ? (
                                <Popconfirm key={a.label} title={`${a.label}?`} onConfirm={() => setDialog({ action: a, row: r })}>
                                    <Button size="small" danger type="text">{a.label}</Button>
                                </Popconfirm>
                            ) : (
                                <Button key={a.label} size="small" onClick={() => setDialog({ action: a, row: r })}>{a.label}</Button>
                            ),
                        )}
                    </Space>
                ),
            }]
            : []),
    ];

    return (
        <Wrap aria-label={spec.title}>
            <div className="cairn-resource-head">
                <div>
                    <p>{spec.description}</p>
                    {spec.path && <code className="cairn-path">GET {spec.path}</code>}
                </div>
                <Space>
                    {pageActions.map((a) => (
                        <Button key={a.label} onClick={() => setDialog({ action: a, row: null })}>{a.label}</Button>
                    ))}
                    {spec.path && <Button onClick={() => q.refresh()} loading={q.loading}>Refresh</Button>}
                </Space>
            </div>
            {!spec.path && <Alert type="info" showIcon message={spec.notes || 'This area has no list in the API.'} />}
            {q.error && (
                <Alert type={denied ? 'warning' : 'error'} showIcon style={{ marginBottom: 12 }}
                    message={denied ? 'You do not have access to this area' : 'Could not load this area'}
                    description={q.error.message} />
            )}
            {summary && (
                <Descriptions bordered size="small" column={{ xs: 1, md: 2, lg: 3 }} style={{ marginBottom: 12, background: tokens.color.surface }}>
                    {Object.entries(summary).map(([k, v]) => (
                        <Descriptions.Item key={k} label={humanize(k)}>{show(k, v)}</Descriptions.Item>
                    ))}
                </Descriptions>
            )}
            {spec.path && (rows.length > 0 || !summary) && (
                <Table<Row>
                    size="middle"
                    loading={q.loading && !q.data}
                    dataSource={rows}
                    rowKey={(r) => rowKey(spec, r, rows.indexOf(r))}
                    columns={columns}
                    pagination={rows.length > 15 ? { pageSize: 15, showSizeChanger: false } : false}
                    scroll={{ x: 'max-content' }}
                    locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={`No ${spec.title.toLowerCase()} yet`} /> }}
                    onRow={(r) => ({ onDoubleClick: () => setDetail(r) })}
                />
            )}
            <Drawer open={!!detail} width={560} onClose={() => setDetail(null)}
                title={detail ? shortUrn(String(detail[columnKeys[0]] ?? spec.title)) : ''}>
                {detail && (
                    <>
                        <Descriptions column={1} size="small" bordered labelStyle={{ width: 170 }}>
                            {Object.entries(detail).map(([k, v]) => (
                                <Descriptions.Item key={k} label={humanize(k)}>{show(k, v, true)}</Descriptions.Item>
                            ))}
                        </Descriptions>
                        {rowActions.length > 0 && (
                            <Space style={{ marginTop: 16 }} wrap>
                                {rowActions.map((a) => (
                                    <Button key={a.label} danger={a.method === 'DELETE'} onClick={() => setDialog({ action: a, row: detail })}>
                                        {a.label}
                                    </Button>
                                ))}
                            </Space>
                        )}
                        {spec.iac && detail[spec.iac.id ?? 'id'] !== undefined && detail[spec.iac.id ?? 'id'] !== null && (
                            <div style={{ marginTop: 16 }}>
                                <CopyAsCode kind={spec.iac.kind} id={String(detail[spec.iac.id ?? 'id'])} />
                            </div>
                        )}
                    </>
                )}
            </Drawer>
            {dialog && (
                <ActionDialog action={dialog.action} row={dialog.row} onClose={() => setDialog(null)}
                    onDone={() => {
                        q.refresh();
                    }} />
            )}
        </Wrap>
    );
}
