import { Alert, Button, Input, Modal, Select, Space, Table, Tag, message } from 'antd';
import React, { useState } from 'react';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { BiImpact, BiKind, BiRole, BiShares, BiVersion, BiVersionDiff } from '../api/types';
import { BiCollabButtons } from './BiCollab';
import { Muted } from './tokens';
import { ErrorNotice, notifyError } from './ErrorNotice';
import { When } from '../utils/time';

const KIND_NAME: Record<BiKind, string> = { query: 'saved query', workbook: 'workbook', dashboard: 'dashboard' };
const when = (ms: number | null | undefined) => <When value={ms} />;
const short = (urn: string) => urn.replace(/^urn:li:corp(user|Group):/, (_, k) => (k === 'Group' ? 'group:' : ''));

/** Who an asset is shared with (owner and editors see it; only the owner changes it). */
export function ShareDialog({ kind, id, name, role, open, onClose }: {
    kind: BiKind; id: string; name: string; role?: BiRole | null; open: boolean; onClose: () => void;
}) {
    const { client } = useConsumerApi();
    const [n, setN] = useState(0);
    const q = useDheQuery<BiShares>(open ? `/sql/shares/${kind}/${id}#${n}` : null, (c, signal) => c.biShares(kind, id, { signal }));
    const [who, setWho] = useState('');
    const [as, setAs] = useState<'VIEWER' | 'EDITOR'>('VIEWER');
    const owner = role === 'OWNER';
    const change = async (principal: string, r: 'VIEWER' | 'EDITOR' | null) => {
        try {
            await client.shareBi(kind, id, principal, r);
            message.success(r ? `Shared with ${short(principal)}` : `No longer shared with ${short(principal)}`);
            setWho('');
            setN((x) => x + 1);
        } catch (e) {
            notifyError(e);
        }
    };
    const principal = who.startsWith('urn:li:') ? who : who.startsWith('group:') ? `urn:li:corpGroup:${who.slice(6)}` : `urn:li:corpuser:${who}`;
    return (
        <Modal title={`Permissions: ${KIND_NAME[kind]} “${name}”`} open={open} onCancel={onClose} footer={null} destroyOnClose>
            <Muted>Who may open or edit it. Permissions never share data: everyone sees only the rows their own policies allow. (Sharing data outside the organisation is External sharing.)</Muted>
            {q.error && <ErrorNotice error={q.error} style={{ marginTop: 8 }} />}
            {owner && (
                <Space style={{ margin: '12px 0' }} wrap>
                    <Input aria-label="Share with" placeholder="person, or group:name" value={who} onChange={(e) => setWho(e.target.value)}
                        style={{ width: 240 }} />
                    <Select aria-label="Role" value={as} onChange={setAs} style={{ width: 120 }}
                        options={[{ value: 'VIEWER', label: 'Viewer' }, { value: 'EDITOR', label: 'Editor' }]} />
                    <Button type="primary" disabled={!who.trim()} onClick={() => change(principal, as)}>Add</Button>
                </Space>
            )}
            <Table size="small" rowKey="principal" pagination={false} loading={q.loading}
                dataSource={q.data?.shares ?? []}
                locale={{ emptyText: 'Only its owner so far' }}
                columns={[
                    { title: 'Who', dataIndex: 'principal', render: (p: string) => short(p) },
                    { title: 'Role', dataIndex: 'role', render: (r: string) => <Tag>{r.toLowerCase()}</Tag> },
                    ...(owner ? [{ title: '', key: 'x', render: (_: unknown, r: { principal: string }) => (
                        <Button size="small" danger aria-label={`Stop sharing with ${short(r.principal)}`}
                            onClick={() => change(r.principal, null)}>Remove</Button>) }] : []),
                ]} />
        </Modal>
    );
}

/** Every saved version: compare one with the version before it, and restore it (owners and editors). */
export function HistoryDialog({ kind, id, name, role, open, onClose, onRestored }: {
    kind: BiKind; id: string; name: string; role?: BiRole | null; open: boolean; onClose: () => void; onRestored?: () => void;
}) {
    const { client } = useConsumerApi();
    const [n, setN] = useState(0);
    const list = useDheQuery<BiVersion[]>(open ? `/sql/versions/${kind}/${id}#${n}` : null, (c, signal) => c.biVersions(kind, id, { signal }));
    const [diff, setDiff] = useState<BiVersionDiff | null>(null);
    const canRestore = role === 'OWNER' || role === 'EDITOR';
    const compare = async (v: number) => {
        try {
            setDiff(await client.biVersionDiff(kind, id, v - 1, v));
        } catch (e) {
            notifyError(e);
        }
    };
    const restore = async (v: number) => {
        try {
            await client.restoreBiVersion(kind, id, v);
            message.success(`Version ${v} restored`);
            setDiff(null);
            setN((x) => x + 1);
            onRestored?.();
        } catch (e) {
            notifyError(e);
        }
    };
    const latest = list.data?.[0]?.version;
    return (
        <Modal title={`History of “${name}”`} open={open} onCancel={onClose} footer={null} width={760} destroyOnClose>
            {list.error && <ErrorNotice error={list.error} />}
            <Table size="small" rowKey="version" pagination={{ pageSize: 8, hideOnSinglePage: true }} loading={list.loading}
                dataSource={list.data ?? []}
                columns={[
                    { title: 'Version', dataIndex: 'version' },
                    { title: 'Name', dataIndex: 'name' },
                    { title: 'Saved by', dataIndex: 'saved_by', render: (s: string) => short(s) },
                    { title: 'When', dataIndex: 'saved_ms', render: when },
                    { title: '', key: 'x', render: (_: unknown, r: BiVersion) => (
                        <Space size={4}>
                            {r.version > 1 && <Button size="small" onClick={() => compare(r.version)}>Changes</Button>}
                            {canRestore && r.version !== latest && (
                                <Button size="small" aria-label={`Restore version ${r.version}`} onClick={() => restore(r.version)}>Restore</Button>
                            )}
                        </Space>
                    ) },
                ]} />
            {diff && (
                <div data-testid="version-diff" style={{ marginTop: 12 }}>
                    <Muted>Version {diff.a} → {diff.b}: {diff.changed.join(', ') || 'no changes'}</Muted>
                    <pre style={{ maxHeight: 280, overflow: 'auto', fontSize: 12, background: 'var(--cairn-subtle, #f6f8fa)', padding: 8 }}>
                        {diff.diff.split('\n').map((l, i) => (
                            <div key={i} style={{ color: l.startsWith('+') && !l.startsWith('+++') ? '#1a7f37'
                                : l.startsWith('-') && !l.startsWith('---') ? '#cf222e' : undefined }}>{l}</div>
                        ))}
                    </pre>
                </div>
            )}
        </Modal>
    );
}

/** The Share and History buttons for an asset. */
export function BiAssetButtons({ kind, id, name, role, onRestored, tiles }: {
    kind: BiKind; id: string; name: string; role?: BiRole | null; onRestored?: () => void; tiles?: string[];
}) {
    const [open, setOpen] = useState<'share' | 'history' | null>(null);
    return (
        <>
            {(role === 'OWNER' || role === 'EDITOR') && <Button onClick={() => setOpen('share')}>Permissions</Button>}
            <Button onClick={() => setOpen('history')}>History</Button>
            <BiCollabButtons kind={kind} id={id} name={name} role={role} tiles={tiles} />
            <ShareDialog kind={kind} id={id} name={name} role={role} open={open === 'share'} onClose={() => setOpen(null)} />
            <HistoryDialog kind={kind} id={id} name={name} role={role} open={open === 'history'} onClose={() => setOpen(null)}
                onRestored={onRestored} />
        </>
    );
}

/** What breaks in BI if a column (or the whole table) goes: shown on a dataset's page. */
export function BiImpactPanel({ dataset, columns }: { dataset: string; columns: string[] }) {
    const [column, setColumn] = useState<string | undefined>(undefined);
    const q = useDheQuery<BiImpact>(`/sql/impact?${dataset}#${column ?? ''}`, (c, signal) => c.biImpact(dataset, column, { signal }));
    const hidden = (q.data?.others ?? []).reduce((a, o) => a + o.assets, 0);
    return (
        <div data-testid="bi-impact">
            <Space style={{ marginBottom: 8 }} wrap>
                <Muted>If this goes:</Muted>
                <Select aria-label="Column" allowClear placeholder="the whole table" style={{ minWidth: 200 }} value={column}
                    onChange={(v) => setColumn(v || undefined)} options={columns.map((c) => ({ value: c, label: c }))} />
            </Space>
            {q.error && <ErrorNotice error={q.error} />}
            <Table size="small" rowKey={(r) => `${r.kind}:${r.id}`} pagination={false} loading={q.loading}
                dataSource={q.data?.assets ?? []}
                locale={{ emptyText: 'No saved query, workbook or dashboard you can see would break' }}
                columns={[
                    { title: 'Breaks', dataIndex: 'name' },
                    { title: 'Kind', dataIndex: 'kind', render: (k: BiKind) => <Tag>{KIND_NAME[k]}</Tag> },
                    { title: 'Owner', dataIndex: 'owner', render: (o: string) => short(o) },
                    { title: 'Reads', dataIndex: 'columns', render: (c: string[], r) =>
                        r.through_queries.length ? `through ${r.through_queries.length} saved quer${r.through_queries.length === 1 ? 'y' : 'ies'}`
                            : c.map((x) => (x === '*' ? 'every column' : x)).join(', ') },
                ]} />
            {hidden > 0 && (
                <Muted>…and {hidden} more you cannot open, owned by {(q.data?.others ?? []).map((o) => short(o.owner)).join(', ')}.</Muted>
            )}
        </div>
    );
}
