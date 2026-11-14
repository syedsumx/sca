import { Button, Empty, Input, Modal, Select, Space, Tag, Tooltip, Tree, message } from 'antd';
import type { DataNode, EventDataNode } from 'antd/lib/tree';
import React, { useCallback, useMemo, useState } from 'react';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { BiFolder, GitFolder, GitFolderFile, SavedQuery } from '../api/types';
import { BiAssetButtons } from './BiAdmin';
import { describeError, notifyError } from './ErrorNotice';
import { RowActions } from './RowActions';
import { Muted } from './tokens';

const GIT_STATE: Record<GitFolderFile['state'], { label: string; color: string; hint: string }> = {
    synced: { label: '', color: '', hint: 'As in git' },
    modified: { label: 'M', color: 'orange', hint: 'Changed in Cairn since the last pull: push to propose it' },
    proposed: { label: 'P', color: 'blue', hint: 'Pushed as it is, on a branch for review' },
    missing: { label: 'D', color: 'red', hint: 'Deleted in Cairn; still in git' },
};

interface Props {
    me: string | undefined;
    saved: SavedQuery[];
    /** The saved query open in the editor, highlighted in the tree. */
    current?: string;
    onOpen: (q: SavedQuery) => void;
    onNew: () => void;
    /** After a change here (rename, move, delete, pull): the saved list is read again. */
    onChanged: () => void;
}

/**
 * The SQL editor's files (L163): the person's saved queries, their folders (and folders shared with everyone),
 * queries shared with them, and their git folders with each file's state against git. A file opens in a tab; its
 * menu renames, files it in a folder, shows its history (versions, compare, restore), shares it or deletes it.
 */
export function WorkspaceFiles({ me, saved, current, onOpen, onNew, onChanged }: Props) {
    const { client } = useConsumerApi();
    const [tick, setTick] = useState(0);
    const folders = useDheQuery<BiFolder[]>(`/sql/folders#${tick}`, (c, signal) => c.biFolders({ signal }).catch(() => []));
    const git = useDheQuery<GitFolder[]>(`/v1/git-folders#${tick}`, (c, signal) => c.gitFolders({ signal }).catch(() => []));
    const [items, setItems] = useState<Record<string, string[]>>({});          // folder id -> saved query ids
    const [gitFiles, setGitFiles] = useState<Record<string, GitFolderFile[]>>({});
    const [filter, setFilter] = useState('');
    // The sections start open (they load at different times, so the open keys are held here, not as defaults).
    const [expanded, setExpanded] = useState<React.Key[]>(['mine', 'folders', 'shared', 'git']);
    const [dialog, setDialog] = useState<null | { kind: 'rename' | 'move' | 'folder' | 'push'; q?: SavedQuery;
        parent?: string | null; git?: GitFolder }>(null);
    const [text, setText] = useState('');
    const [target, setTarget] = useState<string | undefined>();
    const byId = useMemo(() => new Map(saved.map((q) => [q.id, q])), [saved]);
    const mine = saved.filter((q) => q.owner === me);
    const shared = saved.filter((q) => q.owner !== me);
    const refresh = useCallback(() => {
        setItems({});
        setGitFiles({});
        setTick((t) => t + 1);
        onChanged();
    }, [onChanged]);

    const match = (q: SavedQuery) => !filter || q.name.toLowerCase().includes(filter.toLowerCase());

    const fileTitle = (q: SavedQuery, extra?: React.ReactNode) => (
        <span className="cairn-file" style={{ display: 'inline-flex', alignItems: 'center', gap: 6, maxWidth: '100%' }}
            data-current={q.id === current || undefined}>
            <span style={{ fontWeight: q.id === current ? 600 : undefined, overflow: 'hidden', textOverflow: 'ellipsis' }}>{q.name}</span>
            {extra}
            {q.shared && <Tag style={{ marginInlineEnd: 0 }}>shared</Tag>}
            <span onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
                <BiAssetButtons compact kind="query" id={q.id} name={q.name} role={q.role} onRestored={refresh}
                    actions={q.role === 'OWNER' || q.owner === me ? [
                        { key: 'rename', label: 'Rename', onSelect: () => { setText(q.name); setDialog({ kind: 'rename', q }); } },
                        { key: 'move', label: 'Move to folder', onSelect: () => { setTarget(undefined); setDialog({ kind: 'move', q }); } },
                        { key: 'delete', label: 'Delete', danger: true, confirm: `Delete ${q.name}? Its versions go with it.`,
                            onSelect: async () => {
                                try {
                                    await client.deleteSavedQuery(q.id);
                                    refresh();
                                } catch (e) {
                                    notifyError(e);
                                }
                            } },
                    ] : []} />
            </span>
        </span>
    );

    const leaf = (q: SavedQuery, prefix: string, extra?: React.ReactNode): DataNode =>
        ({ key: `${prefix}:q:${q.id}`, title: fileTitle(q, extra), isLeaf: true });

    // Folders: top-level ones, their children loaded when opened.
    const folderList = folders.data ?? [];
    const folderNode = (f: BiFolder): DataNode => {
        const kids = folderList.filter((x) => x.parent_id === f.id);
        const loaded = items[f.id];
        return {
            key: `folder:${f.id}`,
            title: (
                <span style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}>
                    <span>{f.name}</span>{f.shared && <Tag style={{ marginInlineEnd: 0 }}>shared</Tag>}
                    {f.owner === me && (
                        <span onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
                            <RowActions name={f.name} size="small" actions={[
                                { key: 'sub', label: 'New folder inside', onSelect: () => { setText(''); setDialog({ kind: 'folder', parent: f.id }); } },
                                { key: 'delete', label: 'Delete folder', danger: true,
                                    confirm: `Delete the folder ${f.name}? The queries in it stay.`,
                                    onSelect: async () => {
                                        try {
                                            await client.deleteBiFolder(f.id);
                                            refresh();
                                        } catch (e) {
                                            notifyError(e);
                                        }
                                    } },
                            ]} />
                        </span>
                    )}
                </span>
            ),
            children: loaded === undefined && !kids.length ? undefined : [
                ...kids.map(folderNode),
                ...(loaded ?? []).map((id) => byId.get(id)).filter((q): q is SavedQuery => !!q && match(q)).map((q) => leaf(q, `f${f.id}`)),
            ],
            isLeaf: false,
        };
    };

    const gitNode = (g: GitFolder): DataNode => ({
        key: `git:${g.id}`,
        title: (
            <span style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}>
                <span>{g.name}</span> <Muted style={{ fontSize: 12 }}>{g.branch}</Muted>
                {g.error && <Tooltip title={g.error}><Tag color="red">error</Tag></Tooltip>}
                <span onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
                    <RowActions name={g.name} size="small" actions={[
                        { key: 'pull', label: 'Pull', onSelect: async () => {
                            try {
                                const r = await client.pullGitFolder(g.id);
                                message.success(`Pulled: ${r.created.length} new, ${r.updated.length} changed, ${r.deleted.length} removed`
                                    + (r.conflicts.length ? `, ${r.conflicts.length} in conflict` : ''));
                                refresh();
                            } catch (e) {
                                notifyError(e);
                            }
                        } },
                        { key: 'push', label: 'Push…', onSelect: () => { setText(''); setDialog({ kind: 'push', git: g }); } },
                    ]} />
                </span>
            </span>
        ),
        isLeaf: false,
        children: gitFiles[g.id]?.map((f) => {
            const q = f.kind === 'query' ? byId.get(f.ref) : undefined;
            const st = GIT_STATE[f.state];
            const badge = st.label ? <Tooltip title={st.hint}><Tag color={st.color} aria-label={st.hint}>{st.label}</Tag></Tooltip> : null;
            // A git file is named by its file (one short line); the query it is in Cairn is its tooltip, and it opens on click.
            if (q) return { key: `g${g.id}:q:${q.id}`, isLeaf: true,
                title: <span className="cairn-file" title={q.name}>{f.file} {badge}</span> };
            return { key: `git:${g.id}:${f.file}`, isLeaf: true, selectable: false,
                title: <span><Muted>{f.file}</Muted> {badge}{f.kind === 'notebook' && <Tag>notebook</Tag>}</span> };
        }),
    });

    const top = folderList.filter((f) => !f.parent_id || !folderList.some((x) => x.id === f.parent_id));
    const tree: DataNode[] = [
        { key: 'mine', title: <strong>My queries</strong>, selectable: false,
            children: mine.filter(match).map((q) => leaf(q, 'mine')) },
        { key: 'folders', title: <strong>Folders</strong>, selectable: false, children: top.map(folderNode) },
        ...(shared.length ? [{ key: 'shared', title: <strong>Shared with me</strong>, selectable: false,
            children: shared.filter(match).map((q) => leaf(q, 'shared')) }] : []),
        ...((git.data ?? []).length ? [{ key: 'git', title: <strong>Git</strong>, selectable: false,
            children: (git.data ?? []).map(gitNode) }] : []),
    ];

    const loadData = async (node: EventDataNode<DataNode>) => {
        const key = String(node.key);
        try {
            if (key.startsWith('folder:')) {
                const fid = key.slice(7);
                const d = await client.biFolder(fid);
                setItems((m) => ({ ...m, [fid]: d.items.filter((i) => i.kind === 'query').map((i) => i.id) }));
            } else if (key.startsWith('git:') && key.split(':').length === 2) {
                const gid = key.slice(4);
                const files = await client.gitFolderFiles(gid);
                setGitFiles((m) => ({ ...m, [gid]: files }));
            }
        } catch (e) {
            notifyError(e);
        }
    };

    const ok = async () => {
        if (!dialog) return;
        try {
            if (dialog.kind === 'rename' && dialog.q) {
                const q = dialog.q;
                await client.saveQuery({ name: text.trim(), description: q.description, sql: q.sql, connection: q.connection,
                    shared: q.shared, chart: q.chart }, q.id);
            } else if (dialog.kind === 'move' && dialog.q && target) {
                await client.fileBi(target, 'query', dialog.q.id);
            } else if (dialog.kind === 'folder') {
                await client.makeBiFolder({ name: text.trim(), parent: dialog.parent ?? null });
            } else if (dialog.kind === 'push' && dialog.git) {
                const r = await client.pushGitFolder(dialog.git.id, text.trim());
                message.success(r.changed ? `Pushed ${r.changed} file(s) to ${r.branch}` : 'Nothing to push: git already has it');
            }
            setDialog(null);
            refresh();
        } catch (e) {
            message.error(describeError(e));
        }
    };

    const own = folderList.filter((f) => f.owner === me);
    return (
        <section aria-label="Files">
            <Space style={{ width: '100%', marginBottom: 8 }} wrap>
                <Button size="small" type="primary" onClick={onNew}>New file</Button>
                <Button size="small" onClick={() => { setText(''); setDialog({ kind: 'folder', parent: null }); }}>New folder</Button>
            </Space>
            <Input.Search size="small" allowClear placeholder="Find a file" aria-label="Find a file" value={filter}
                onChange={(e) => setFilter(e.target.value)} style={{ marginBottom: 8 }} />
            {saved.length || folderList.length ? (
                <Tree
                    blockNode
                    showLine={{ showLeafIcon: false }}
                    expandedKeys={expanded}
                    // Kept as asked even for a section that is not there yet (git folders load after the tree):
                    // only the node clicked opens or closes.
                    defaultExpandParent={false}
                    onExpand={(_, info) => setExpanded((cur) => (info.expanded
                        ? [...cur.filter((k) => k !== info.node.key), info.node.key]
                        : cur.filter((k) => k !== info.node.key)))}
                    treeData={tree}
                    loadData={loadData}
                    selectedKeys={current ? tree.flatMap(function keys(n: DataNode): string[] {
                        return [...(String(n.key).endsWith(`:q:${current}`) ? [String(n.key)] : []), ...(n.children ?? []).flatMap(keys)];
                    }) : []}
                    onSelect={(_, info) => {
                        const key = String(info.node.key);
                        const id = key.includes(':q:') ? key.split(':q:')[1] : undefined;
                        const q = id ? byId.get(id) : undefined;
                        if (q) onOpen(q);
                    }}
                />
            ) : (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="No saved queries yet: write one and save it" />
            )}
            <Modal
                open={!!dialog}
                title={dialog?.kind === 'rename' ? `Rename ${dialog.q?.name}` : dialog?.kind === 'move' ? `Move ${dialog.q?.name}`
                    : dialog?.kind === 'push' ? `Push ${dialog.git?.name}` : 'New folder'}
                okText={dialog?.kind === 'push' ? 'Push' : dialog?.kind === 'move' ? 'Move' : dialog?.kind === 'rename' ? 'Rename' : 'Create'}
                okButtonProps={{ disabled: dialog?.kind === 'move' ? !target : dialog?.kind !== 'push' && !text.trim() }}
                onOk={() => void ok()}
                onCancel={() => setDialog(null)}>
                {dialog?.kind === 'move' ? (
                    own.length ? (
                        <Select aria-label="Folder" style={{ width: '100%' }} value={target} onChange={setTarget}
                            placeholder="Choose a folder" options={own.map((f) => ({ value: f.id, label: f.name }))} />
                    ) : <Muted>Make a folder first (New folder).</Muted>
                ) : dialog?.kind === 'push' ? (
                    <>
                        <Muted style={{ display: 'block', marginBottom: 8 }}>
                            Your changed files go to a new branch for review, never straight onto {dialog.git?.branch}.
                        </Muted>
                        <Input aria-label="Commit message" placeholder="What changed" value={text} onChange={(e) => setText(e.target.value)} />
                    </>
                ) : (
                    <Input aria-label="Name" autoFocus value={text} onChange={(e) => setText(e.target.value)}
                        onPressEnter={() => void ok()} />
                )}
            </Modal>
        </section>
    );
}
