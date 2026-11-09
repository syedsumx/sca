import { Alert, Button, Checkbox, Input, InputNumber, Modal, Popconfirm, Select, Space, Table, Tag, message } from 'antd';
import React, { useState } from 'react';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { GitFolder, GitPullResult, GitPushResult, SavedQuery } from '../api/types';
import { Muted, Page, Surface } from '../components/tokens';
import { HelpButton } from '../guide/PageHeader';

type Outcome = { kind: 'pull'; folder: string; result: GitPullResult } | { kind: 'push'; folder: string; result: GitPushResult };

function PullSummary({ r, onForce }: { r: GitPullResult; onForce?: () => void }) {
    const parts: [string, string[]][] = [
        ['New queries', r.created],
        ['Updated', r.updated],
        ['Deleted', r.deleted],
        ['Notebooks published', r.notebooks],
    ];
    const changed = parts.some(([, v]) => v.length);
    return (
        <>
            {!changed && !r.conflicts.length && !r.errors.length && <Alert type="success" showIcon message="Already up to date." />}
            {parts
                .filter(([, v]) => v.length)
                .map(([label, files]) => (
                    <div key={label}>
                        <strong>{label}:</strong> {files.join(', ')}
                    </div>
                ))}
            {r.conflicts.length > 0 && (
                <Alert
                    style={{ marginTop: 8 }}
                    type="warning"
                    showIcon
                    message={`${r.conflicts.length} conflict${r.conflicts.length > 1 ? 's' : ''}: left as they are in Cairn`}
                    description={
                        <>
                            <ul style={{ margin: 0, paddingLeft: 16 }}>
                                {r.conflicts.map((c) => (
                                    <li key={c.file}>
                                        {c.file}: {c.why}
                                    </li>
                                ))}
                            </ul>
                            {onForce && (
                                <Popconfirm title="Take git's version and lose the changes made in Cairn?" onConfirm={onForce}>
                                    <Button size="small" danger style={{ marginTop: 8 }}>
                                        Take git's version
                                    </Button>
                                </Popconfirm>
                            )}
                        </>
                    }
                />
            )}
            {r.errors.length > 0 && (
                <Alert
                    style={{ marginTop: 8 }}
                    type="error"
                    showIcon
                    message="Files that could not be read"
                    description={
                        <ul style={{ margin: 0, paddingLeft: 16 }}>
                            {r.errors.map((e) => (
                                <li key={e.file}>
                                    {e.file}: {e.error}
                                </li>
                            ))}
                        </ul>
                    }
                />
            )}
        </>
    );
}

/** Git folders: saved queries and notebooks kept in a git repository. Pull brings git's version in; push
 * proposes Cairn's on a new branch (with a pull request for configured hosts). */
export function GitFoldersPage() {
    const { client } = useConsumerApi();
    const [tick, setTick] = useState(0);
    const list = useDheQuery<GitFolder[]>(`/v1/git-folders#${tick}`, (c, signal) => c.gitFolders({ signal }));
    const queries = useDheQuery<SavedQuery[]>(`/sql/saved#${tick}`, (c, signal) => c.savedQueries({ signal }));
    const [creating, setCreating] = useState(false);
    const [form, setForm] = useState({ name: '', source: 'repo' as 'repo' | 'url', repo: '', url: '', branch: 'main', path: '', every: 60, scheduled: false });
    const [error, setError] = useState<string | null>(null);
    const [busy, setBusy] = useState<string | null>(null);
    const [outcome, setOutcome] = useState<Outcome | null>(null);
    const [pushing, setPushing] = useState<GitFolder | null>(null);
    const [push, setPush] = useState({ message: '', add: [] as string[] });

    const create = async () => {
        setError(null);
        try {
            await client.createGitFolder({
                name: form.name,
                ...(form.source === 'repo' ? { repo: form.repo } : { url: form.url }),
                branch: form.branch || 'main',
                path: form.path,
                ...(form.scheduled ? { every_minutes: form.every } : {}),
            });
            setCreating(false);
            setTick((t) => t + 1);
            message.success('Folder linked: pull it to bring its queries in');
        } catch (e) {
            setError((e as Error).message);
        }
    };

    const pull = async (f: GitFolder, force = false) => {
        setBusy(f.id);
        try {
            setOutcome({ kind: 'pull', folder: f.id, result: await client.pullGitFolder(f.id, force) });
        } catch (e) {
            message.error((e as Error).message);
        } finally {
            setBusy(null);
            setTick((t) => t + 1);
        }
    };

    const doPush = async () => {
        if (!pushing) return;
        const f = pushing;
        setBusy(f.id);
        try {
            const result = await client.pushGitFolder(f.id, push.message, push.add);
            setPushing(null);
            setOutcome({ kind: 'push', folder: f.id, result });
        } catch (e) {
            message.error((e as Error).message);
        } finally {
            setBusy(null);
            setTick((t) => t + 1);
        }
    };

    const remove = async (id: string) => {
        try {
            await client.deleteGitFolder(id);
            setTick((t) => t + 1);
        } catch (e) {
            message.error((e as Error).message);
        }
    };

    const folderName = (id: string) => list.data?.find((f) => f.id === id)?.name ?? id;

    return (
        <Page>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}><h1 style={{ marginBottom: 0 }}>Git folders</h1><HelpButton /></div>
            <Muted>
                Keep your saved queries and notebooks in git and review them like code. Pull brings in what was merged; push proposes
                your changes on a new branch, never on the folder's own.
            </Muted>
            <Surface style={{ marginTop: 16 }}>
                <Button type="primary" style={{ marginBottom: 12 }} onClick={() => setCreating(true)}>
                    Link a folder
                </Button>
                {list.error && <Alert type="error" message={list.error.message} />}
                <Table<GitFolder>
                    rowKey="id"
                    size="small"
                    loading={list.loading}
                    dataSource={list.data ?? []}
                    locale={{ emptyText: 'No folders yet.' }}
                    columns={[
                        { title: 'Name', dataIndex: 'name' },
                        { title: 'Repository', key: 'repo', render: (_: unknown, f) => f.repo ?? f.url },
                        { title: 'Branch', dataIndex: 'branch', render: (b: string, f) => `${b}${f.path ? ` : ${f.path}` : ''}` },
                        { title: 'Pulled', dataIndex: 'synced_ms', render: (v: number | null, f) => (v ? `${new Date(v).toLocaleString()} (${(f.head ?? '').slice(0, 8)})` : <Muted>never</Muted>) },
                        { title: 'Schedule', dataIndex: 'every_minutes', render: (m: number | null) => (m ? `every ${m} min` : <Muted>on demand</Muted>) },
                        { title: 'Status', dataIndex: 'error', render: (e: string | null) => (e ? <Tag color="red" title={e}>error</Tag> : <Tag color="green">ok</Tag>) },
                        {
                            title: '',
                            key: 'actions',
                            render: (_: unknown, f) => (
                                <Space>
                                    <Button size="small" loading={busy === f.id} onClick={() => void pull(f)}>
                                        Pull
                                    </Button>
                                    <Button
                                        size="small"
                                        disabled={busy === f.id}
                                        onClick={() => {
                                            setPush({ message: '', add: [] });
                                            setPushing(f);
                                        }}>
                                        Push
                                    </Button>
                                    <Popconfirm title="Unlink this folder? Its queries stay, as ordinary ones." onConfirm={() => void remove(f.id)}>
                                        <Button size="small" danger>
                                            Unlink
                                        </Button>
                                    </Popconfirm>
                                </Space>
                            ),
                        },
                    ]}
                />
                {list.data?.some((f) => f.error) && (
                    <Alert style={{ marginTop: 12 }} type="error" message={list.data.filter((f) => f.error).map((f) => `${f.name}: ${f.error}`).join('; ')} />
                )}
            </Surface>
            {outcome && (
                <Surface style={{ marginTop: 16 }}>
                    <h3>
                        {outcome.kind === 'pull' ? 'Pulled' : 'Pushed'} {folderName(outcome.folder)}
                    </h3>
                    {outcome.kind === 'pull' ? (
                        <PullSummary
                            r={outcome.result}
                            onForce={() => {
                                const f = list.data?.find((x) => x.id === outcome.folder);
                                if (f) void pull(f, true);
                            }}
                        />
                    ) : outcome.result.changed === 0 ? (
                        <Alert type="info" showIcon message="Nothing to push: git already has these queries." />
                    ) : (
                        <Alert
                            type="success"
                            showIcon
                            message={`${outcome.result.changed} file${outcome.result.changed > 1 ? 's' : ''} pushed to ${outcome.result.branch}`}
                            description={
                                outcome.result.pull_request ? (
                                    <a href={outcome.result.pull_request} target="_blank" rel="noreferrer">
                                        Review the pull request
                                    </a>
                                ) : (
                                    'Open a pull request from that branch on your git host.'
                                )
                            }
                        />
                    )}
                </Surface>
            )}
            <Modal title="Link a git folder" open={creating} okText="Link" onOk={() => void create()} onCancel={() => setCreating(false)}>
                {error && <Alert type="error" message={error} style={{ marginBottom: 12 }} />}
                <Space direction="vertical" style={{ width: '100%' }}>
                    <Input aria-label="Folder name" placeholder="Name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
                    <Select
                        aria-label="Source"
                        value={form.source}
                        onChange={(v) => setForm({ ...form, source: v })}
                        options={[
                            { value: 'repo', label: 'A repository set up for Cairn' },
                            { value: 'url', label: 'A repository URL your operator allows' },
                        ]}
                    />
                    {form.source === 'repo' ? (
                        <Input aria-label="Repository" placeholder="Repository name" value={form.repo} onChange={(e) => setForm({ ...form, repo: e.target.value })} />
                    ) : (
                        <Input aria-label="URL" placeholder="https://git.example.com/team/queries.git" value={form.url} onChange={(e) => setForm({ ...form, url: e.target.value })} />
                    )}
                    <Input aria-label="Branch" placeholder="main" value={form.branch} onChange={(e) => setForm({ ...form, branch: e.target.value })} />
                    <Input aria-label="Path" placeholder="Path in the repository (empty: the root)" value={form.path} onChange={(e) => setForm({ ...form, path: e.target.value })} />
                    <Space>
                        <Checkbox checked={form.scheduled} onChange={(e) => setForm({ ...form, scheduled: e.target.checked })}>
                            Pull every
                        </Checkbox>
                        <InputNumber aria-label="Minutes" min={5} max={1440} disabled={!form.scheduled} value={form.every} onChange={(v) => setForm({ ...form, every: Number(v ?? 60) })} />
                        minutes
                    </Space>
                </Space>
            </Modal>
            <Modal title={`Push ${pushing?.name ?? ''}`} open={!!pushing} okText="Push" confirmLoading={!!pushing && busy === pushing.id} onOk={() => void doPush()} onCancel={() => setPushing(null)}>
                <Muted>The folder's queries, as they are in Cairn, go to a new branch for review.</Muted>
                <Input.TextArea
                    aria-label="Commit message"
                    placeholder="What changed (the commit message)"
                    style={{ marginTop: 12 }}
                    value={push.message}
                    onChange={(e) => setPush({ ...push, message: e.target.value })}
                />
                <Select
                    mode="multiple"
                    aria-label="Also add"
                    placeholder="Also add these saved queries"
                    style={{ width: '100%', marginTop: 12 }}
                    value={push.add}
                    onChange={(v) => setPush({ ...push, add: v })}
                    options={(queries.data ?? []).map((q) => ({ value: q.id, label: q.name }))}
                />
            </Modal>
        </Page>
    );
}
