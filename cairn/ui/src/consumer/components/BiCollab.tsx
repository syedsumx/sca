import { Alert, Button, Checkbox, Drawer, Empty, Input, List, Modal, Popconfirm, Select, Space, Table, Tag, Tree } from 'antd';
import React, { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { BiComment, BiFolder, BiFolderDetail, BiKind, BiPopular, BiRole, BiStale, BiUsage, Dashboard, SavedQuery, Workbook } from '../api/types';
import { Muted } from './tokens';
import { ErrorNotice, notifyError } from './ErrorNotice';
import { When } from '../utils/time';

const KIND_NAME: Record<BiKind, string> = { query: 'saved query', workbook: 'workbook', dashboard: 'dashboard' };
const PAGE: Record<BiKind, string> = { query: '/sql', workbook: '/sql/workbooks', dashboard: '/dashboards' };
const when = (ms: number | null | undefined) => <When value={ms} />;
const short = (urn: string) => urn.replace(/^urn:li:corpuser:/, '');
const fail = (e: unknown) => notifyError(e);

/** Where an asset opens: dashboards open themselves, the others their page. */
export const assetLink = (kind: BiKind, id: string) => (kind === 'dashboard' ? `/dashboards?open=${encodeURIComponent(id)}` : PAGE[kind]);

/** The names of the BI assets the viewer may open, by kind and id. */
function useAssetNames() {
    const qs = useDheQuery<SavedQuery[]>('/sql/saved', (c, signal) => c.savedQueries({ signal }));
    const wbs = useDheQuery<Workbook[]>('/sql/workbooks', (c, signal) => c.workbooks({ signal }));
    const ds = useDheQuery<Dashboard[]>('/sql/dashboards', (c, signal) => c.dashboards({ signal }));
    return useMemo(() => {
        const m = new Map<string, string>();
        (qs.data ?? []).forEach((x) => m.set(`query:${x.id}`, x.name));
        (wbs.data ?? []).forEach((x) => m.set(`workbook:${x.id}`, x.name));
        (ds.data ?? []).forEach((x) => m.set(`dashboard:${x.id}`, x.name));
        return m;
    }, [qs.data, wbs.data, ds.data]);
}

// ---- comments -------------------------------------------------------------------------------------

/** An asset's comment threads: everyone who may open it reads and writes; authors edit theirs; the owner tidies. */
export function CommentsDrawer({ kind, id, name, role, open, onClose, tiles }: {
    kind: BiKind; id: string; name: string; role?: BiRole | null; open: boolean; onClose: () => void; tiles?: string[];
}) {
    const { client } = useConsumerApi();
    const [n, setN] = useState(0);
    const q = useDheQuery<BiComment[]>(open ? `/sql/comments/${kind}/${id}#${n}` : null, (c, signal) => c.biComments(kind, id, { signal }),
        { staleMs: 0 });
    const me = useDheQuery<{ urn: string }>(open ? '/me' : null, (c, signal) => c.me({ signal }));
    const [text, setText] = useState('');
    const [tile, setTile] = useState<number | undefined>(undefined);
    const [replyTo, setReplyTo] = useState<string | null>(null);
    const [reply, setReply] = useState('');
    const [editing, setEditing] = useState<{ id: string; body: string } | null>(null);
    const all = q.data ?? [];
    const roots = all.filter((c) => !c.parent_id);
    const replies = (pid: string) => all.filter((c) => c.parent_id === pid);
    const refresh = () => setN((x) => x + 1);
    const post = async (body: string, parent?: string) => {
        try {
            await client.commentBi(kind, id, { body, ...(parent ? { parent } : {}), ...(!parent && tile !== undefined ? { tile } : {}) });
            if (parent) {
                setReply('');
                setReplyTo(null);
            } else {
                setText('');
                setTile(undefined);
            }
            refresh();
        } catch (e) {
            fail(e);
        }
    };
    const save = async () => {
        if (!editing) return;
        try {
            await client.editBiComment(editing.id, editing.body);
            setEditing(null);
            refresh();
        } catch (e) {
            fail(e);
        }
    };
    const remove = async (cid: string) => {
        try {
            await client.deleteBiComment(cid);
            refresh();
        } catch (e) {
            fail(e);
        }
    };
    const one = (c: BiComment, depth: number) => {
        const mine = me.data?.urn === c.author;
        return (
            <div key={c.id} data-testid="comment" style={{ marginLeft: depth * 24, padding: '8px 0', borderBottom: '1px solid var(--cairn-border, #eee)' }}>
                <Space size={6} wrap>
                    <strong>{short(c.author)}</strong>
                    <Muted>{when(c.created_ms)}{c.edited_ms ? ' · edited' : ''}</Muted>
                    {c.tile !== null && <Tag>{tiles?.[c.tile] || `tile ${c.tile + 1}`}</Tag>}
                </Space>
                {editing?.id === c.id ? (
                    <Space.Compact style={{ width: '100%', marginTop: 4 }}>
                        <Input aria-label="Edit comment" value={editing.body} onChange={(e) => setEditing({ id: c.id, body: e.target.value })} />
                        <Button type="primary" onClick={save} disabled={!editing.body.trim()}>Save</Button>
                        <Button onClick={() => setEditing(null)}>Cancel</Button>
                    </Space.Compact>
                ) : (
                    <div style={{ whiteSpace: 'pre-wrap', marginTop: 2 }}>{c.deleted ? <Muted>(deleted)</Muted> : c.body}</div>
                )}
                {!c.deleted && editing?.id !== c.id && (
                    <Space size={4} style={{ marginTop: 2 }}>
                        {depth === 0 && <Button size="small" type="link" onClick={() => setReplyTo(c.id)}>Reply</Button>}
                        {mine && <Button size="small" type="link" onClick={() => setEditing({ id: c.id, body: c.body ?? '' })}>Edit</Button>}
                        {(mine || role === 'OWNER') && (
                            <Popconfirm title="Delete this comment?" onConfirm={() => remove(c.id)}>
                                <Button size="small" type="link" danger>Delete</Button>
                            </Popconfirm>
                        )}
                    </Space>
                )}
                {replyTo === c.id && (
                    <Space.Compact style={{ width: '100%', marginTop: 4 }}>
                        <Input aria-label="Reply" placeholder="Reply…" value={reply} onChange={(e) => setReply(e.target.value)}
                            onPressEnter={() => reply.trim() && post(reply.trim(), c.id)} />
                        <Button type="primary" disabled={!reply.trim()} onClick={() => post(reply.trim(), c.id)}>Reply</Button>
                    </Space.Compact>
                )}
                {replies(c.id).map((r) => one(r, depth + 1))}
            </div>
        );
    };
    return (
        <Drawer title={`Comments on “${name}”`} open={open} onClose={onClose} width={480} destroyOnClose>
            <Muted>Mention someone with @name: they are told if they may open this {KIND_NAME[kind]}.</Muted>
            <Input.TextArea aria-label="New comment" rows={3} style={{ marginTop: 8 }} value={text} placeholder="Ask or note something…"
                onChange={(e) => setText(e.target.value)} maxLength={10000} />
            <Space style={{ margin: '8px 0 12px' }} wrap>
                {tiles && tiles.length > 0 && (
                    <Select aria-label="About tile" allowClear placeholder="the whole dashboard" style={{ minWidth: 200 }} value={tile}
                        onChange={(v) => setTile(v ?? undefined)} options={tiles.map((t, i) => ({ value: i, label: t || `Tile ${i + 1}` }))} />
                )}
                <Button type="primary" disabled={!text.trim()} onClick={() => post(text.trim())}>Comment</Button>
            </Space>
            {q.error && <ErrorNotice error={q.error} />}
            {!q.loading && roots.length === 0 && <Empty description="No comments yet" image={Empty.PRESENTED_IMAGE_SIMPLE} />}
            {roots.map((c) => one(c, 0))}
        </Drawer>
    );
}

// ---- usage of one asset ---------------------------------------------------------------------------

/** Views over the last 30 days, by day (owners and editors). */
export function UsageDialog({ kind, id, name, open, onClose }: { kind: BiKind; id: string; name: string; open: boolean; onClose: () => void }) {
    const q = useDheQuery<BiUsage>(open ? `/sql/usage/${kind}/${id}` : null, (c, signal) => c.biUsage(kind, id, { signal }), { staleMs: 0 });
    const top = Math.max(1, ...(q.data?.days ?? []).map((d) => d.views));
    return (
        <Modal title={`Usage of “${name}”`} open={open} onCancel={onClose} footer={null} destroyOnClose>
            {q.error && <ErrorNotice error={q.error} />}
            {q.data && (
                <>
                    <p data-testid="usage-total">
                        <strong>{q.data.views}</strong> view{q.data.views === 1 ? '' : 's'} by <strong>{q.data.viewers}</strong>{' '}
                        {q.data.viewers === 1 ? 'person' : 'people'} since {q.data.since}
                    </p>
                    <Table size="small" rowKey="day" pagination={false} dataSource={[...q.data.days].reverse()}
                        locale={{ emptyText: 'Nobody has opened it in this time' }}
                        columns={[
                            { title: 'Day', dataIndex: 'day' },
                            { title: 'Views', dataIndex: 'views', render: (v: number) => (
                                <Space><span style={{ display: 'inline-block', height: 8, width: Math.round((v / top) * 120),
                                    background: 'var(--cairn-accent, #1677ff)', borderRadius: 2 }} />{v}</Space>) },
                            { title: 'People', dataIndex: 'viewers' },
                        ]} />
                </>
            )}
        </Modal>
    );
}

/** The Comments and Usage buttons for an asset. */
export function BiCollabButtons({ kind, id, name, role, tiles }: { kind: BiKind; id: string; name: string; role?: BiRole | null; tiles?: string[] }) {
    const [open, setOpen] = useState<'comments' | 'usage' | null>(null);
    return (
        <>
            <Button onClick={() => setOpen('comments')}>Comments</Button>
            {(role === 'OWNER' || role === 'EDITOR') && <Button onClick={() => setOpen('usage')}>Usage</Button>}
            <CommentsDrawer kind={kind} id={id} name={name} role={role} tiles={tiles} open={open === 'comments'} onClose={() => setOpen(null)} />
            <UsageDialog kind={kind} id={id} name={name} open={open === 'usage'} onClose={() => setOpen(null)} />
        </>
    );
}

// ---- folders --------------------------------------------------------------------------------------

/** Folders of dashboards, workbooks and saved queries. Each person sees in a folder only what they may open. */
export function BiFolders() {
    const { client } = useConsumerApi();
    const [n, setN] = useState(0);
    const list = useDheQuery<BiFolder[]>(`/sql/folders#${n}`, (c, signal) => c.biFolders({ signal }), { staleMs: 0 });
    const me = useDheQuery<{ urn: string }>('/me', (c, signal) => c.me({ signal }));
    const [sel, setSel] = useState<string | null>(null);
    const detail = useDheQuery<BiFolderDetail>(sel ? `/sql/folders/${sel}#${n}` : null, (c, signal) => c.biFolder(sel as string, { signal }),
        { staleMs: 0 });
    const names = useAssetNames();
    const [making, setMaking] = useState<{ name: string; shared: boolean; parent: string | null } | null>(null);
    const [adding, setAdding] = useState<string | undefined>(undefined);
    const [renaming, setRenaming] = useState<string | null>(null);
    const refresh = () => setN((x) => x + 1);
    const folders = list.data ?? [];
    const byId = new Map(folders.map((f) => [f.id, f]));
    const tree = useMemo(() => {
        const ids = new Set(folders.map((f) => f.id));
        const kids = (pid: string | null): { key: string; title: React.ReactNode; children: unknown[] }[] =>
            folders.filter((f) => (pid === null ? !f.parent_id || !ids.has(f.parent_id) : f.parent_id === pid)).map((f) => ({
                key: f.id,
                title: <span>{f.name}{f.shared && <Tag style={{ marginLeft: 6 }}>shared</Tag>}</span>,
                children: kids(f.id),
            }));
        return kids(null);
    }, [folders]);
    const cur = sel ? (detail.data ?? byId.get(sel)) : undefined;
    const mine = !!cur && cur.owner === me.data?.urn;
    const make = async () => {
        if (!making) return;
        try {
            const f = await client.makeBiFolder({ name: making.name.trim(), shared: making.shared, parent: making.parent });
            setMaking(null);
            setSel(f.id);
            refresh();
        } catch (e) {
            fail(e);
        }
    };
    const act = async (fn: () => Promise<unknown>, after?: () => void) => {
        try {
            await fn();
            after?.();
            refresh();
        } catch (e) {
            fail(e);
        }
    };
    const options = [...names.entries()].filter(([k]) => !(detail.data?.items ?? []).some((i) => `${i.kind}:${i.id}` === k))
        .map(([k, v]) => ({ value: k, label: `${v} (${KIND_NAME[k.split(':')[0] as BiKind]})` }));
    return (
        <div data-testid="bi-folders" style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
            <div style={{ flex: '1 1 220px', minWidth: 220, maxWidth: 360 }}>
                <Space style={{ marginBottom: 8 }} wrap>
                    <Button type="primary" onClick={() => setMaking({ name: '', shared: false, parent: null })}>New folder</Button>
                    {mine && <Button onClick={() => setMaking({ name: '', shared: false, parent: sel })}>New folder inside</Button>}
                </Space>
                {list.error && <ErrorNotice error={list.error} />}
                {folders.length === 0 && !list.loading ? <Empty description="No folders yet" image={Empty.PRESENTED_IMAGE_SIMPLE} /> : (
                    <Tree aria-label="Folders" treeData={tree as never} selectedKeys={sel ? [sel] : []} defaultExpandAll
                        onSelect={(k) => setSel((k[0] as string) ?? null)} />
                )}
            </div>
            <div style={{ flex: '3 1 320px', minWidth: 0 }}>
                {!cur && <Muted>Choose a folder.</Muted>}
                {cur && (
                    <>
                        <Space wrap style={{ marginBottom: 8 }}>
                            <h3 style={{ margin: 0 }}>{cur.name}</h3>
                            {!mine && <Muted>shared by {short(cur.owner)}</Muted>}
                            {mine && (
                                <>
                                    <Checkbox checked={cur.shared} onChange={(e) => act(() => client.changeBiFolder(cur.id, { shared: e.target.checked }))}>
                                        Shared with everyone
                                    </Checkbox>
                                    <Button size="small" onClick={() => setRenaming(cur.name)}>Rename</Button>
                                    <Popconfirm title="Delete this folder? What is in it is not deleted."
                                        onConfirm={() => act(() => client.deleteBiFolder(cur.id), () => setSel(null))}>
                                        <Button size="small" danger>Delete</Button>
                                    </Popconfirm>
                                </>
                            )}
                        </Space>
                        <Muted>Sharing a folder shows its name; each person sees in it only what is shared with them.</Muted>
                        {mine && (
                            <Space.Compact style={{ width: '100%', margin: '8px 0' }}>
                                <Select aria-label="Add to folder" showSearch optionFilterProp="label" placeholder="Add a dashboard, workbook or query"
                                    style={{ flex: 1 }} value={adding} onChange={setAdding} options={options} />
                                <Button disabled={!adding} onClick={() => {
                                    const [kind, ...rest] = (adding as string).split(':');
                                    act(() => client.fileBi(cur.id, kind as BiKind, rest.join(':')), () => setAdding(undefined));
                                }}>Add</Button>
                            </Space.Compact>
                        )}
                        {detail.error && <ErrorNotice error={detail.error} />}
                        <List size="small" loading={detail.loading} locale={{ emptyText: 'Nothing here that you may open' }}
                            dataSource={[...(detail.data?.folders ?? []).map((f) => ({ folder: f })),
                                ...(detail.data?.items ?? []).map((i) => ({ item: i }))]}
                            renderItem={(row) => 'folder' in row ? (
                                <List.Item><a onClick={() => setSel(row.folder.id)}>📁 {row.folder.name}</a></List.Item>
                            ) : (
                                <List.Item data-testid="folder-item" actions={mine ? [
                                    <Button key="x" size="small" aria-label={`Take out ${names.get(`${row.item.kind}:${row.item.id}`) ?? row.item.id}`}
                                        onClick={() => act(() => client.fileBi(cur.id, row.item.kind, row.item.id, true))}>Take out</Button>] : []}>
                                    <Space>
                                        <Tag>{KIND_NAME[row.item.kind]}</Tag>
                                        <Link to={assetLink(row.item.kind, row.item.id)}>{names.get(`${row.item.kind}:${row.item.id}`) ?? row.item.id}</Link>
                                    </Space>
                                </List.Item>
                            )} />
                    </>
                )}
            </div>
            <Modal title="Rename the folder" open={renaming !== null && !!cur} onCancel={() => setRenaming(null)} okText="Rename"
                okButtonProps={{ disabled: !renaming?.trim() }} destroyOnClose
                onOk={() => cur && renaming?.trim() && act(() => client.changeBiFolder(cur.id, { name: renaming.trim() }), () => setRenaming(null))}>
                <Input aria-label="New name" autoFocus maxLength={200} value={renaming ?? ''} onChange={(e) => setRenaming(e.target.value)} />
            </Modal>
            <Modal title={making?.parent ? `New folder in “${byId.get(making.parent)?.name ?? ''}”` : 'New folder'} open={!!making}
                onCancel={() => setMaking(null)} onOk={make} okText="Create" okButtonProps={{ disabled: !making?.name.trim() }} destroyOnClose>
                <Input aria-label="Folder name" autoFocus placeholder="Name" maxLength={200} value={making?.name ?? ''}
                    onChange={(e) => making && setMaking({ ...making, name: e.target.value })} onPressEnter={make} />
                <Checkbox style={{ marginTop: 8 }} checked={making?.shared} onChange={(e) => making && setMaking({ ...making, shared: e.target.checked })}>
                    Shared with everyone
                </Checkbox>
            </Modal>
        </div>
    );
}

// ---- what is used, and what is not ----------------------------------------------------------------

/** The most viewed assets the viewer may open, and their own assets nobody opened in 30 days. */
export function BiUsageOverview() {
    const popular = useDheQuery<BiPopular[]>('/sql/usage/popular', (c, signal) => c.biPopular({ signal }), { staleMs: 0 });
    const stale = useDheQuery<BiStale[]>('/sql/usage/stale', (c, signal) => c.biStale({ signal }), { staleMs: 0 });
    const names = useAssetNames();
    const nameOf = (r: { kind: BiKind; id: string }) => (
        <Link to={assetLink(r.kind, r.id)}>{names.get(`${r.kind}:${r.id}`) ?? r.id}</Link>
    );
    return (
        <div data-testid="bi-usage">
            <h3>Most viewed in the last 30 days</h3>
            {popular.error && <ErrorNotice error={popular.error} />}
            <Table size="small" rowKey={(r) => `${r.kind}:${r.id}`} pagination={false} loading={popular.loading} dataSource={popular.data ?? []}
                locale={{ emptyText: 'Nothing viewed yet' }} scroll={{ x: true }}
                columns={[
                    { title: 'Name', key: 'n', render: (_: unknown, r: BiPopular) => nameOf(r) },
                    { title: 'Kind', dataIndex: 'kind', render: (k: BiKind) => <Tag>{KIND_NAME[k]}</Tag> },
                    { title: 'Views', dataIndex: 'views' },
                    { title: 'People', dataIndex: 'viewers' },
                ]} />
            <h3 style={{ marginTop: 24 }}>Yours, unopened for 30 days</h3>
            <Muted>Candidates to tidy away.</Muted>
            {stale.error && <ErrorNotice error={stale.error} />}
            <Table size="small" rowKey={(r) => `${r.kind}:${r.id}`} pagination={{ pageSize: 10, hideOnSinglePage: true }} loading={stale.loading}
                dataSource={stale.data ?? []} locale={{ emptyText: 'Everything of yours has been opened lately' }} scroll={{ x: true }}
                columns={[
                    { title: 'Name', key: 'n', render: (_: unknown, r: BiStale) => <Link to={assetLink(r.kind, r.id)}>{r.name}</Link> },
                    { title: 'Kind', dataIndex: 'kind', render: (k: BiKind) => <Tag>{KIND_NAME[k]}</Tag> },
                    { title: 'Last changed', dataIndex: 'updated_ms', render: (ms: number | null) => (ms ? when(ms) : '') },
                ]} />
        </div>
    );
}
