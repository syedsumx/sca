import { Alert, Button, Descriptions, Input, Select, Space, Table, Tabs, Tag, Typography } from 'antd';
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';

import { useConsumerApi } from '../api/hooks';
import { Muted } from '../components/tokens';
import { describeError } from '../components/ErrorNotice';
import { Picker } from './pickers';

/** One resource of a change's plan: what happens to it, field by field. */
export interface PlanItem {
    action: 'create' | 'update' | 'replace' | 'delete';
    kind: string;
    key: string;
    terraform_type: string;
    fields: { field: string; before: unknown; after: unknown }[];
}

export interface ChangePlan {
    changes: PlanItem[];
    counts: Record<string, number>;
    digest: string;
}

/** A change request (GET /v1/iac/changes/{id}). */
export interface ChangeRequest {
    id: string;
    title: string;
    status: 'proposed' | 'approved' | 'rejected' | 'applied' | 'failed' | 'withdrawn';
    author: string;
    agent: string | null;
    sponsor: string;
    intent: string;
    allow_delete: boolean;
    kinds: string[];
    approved_by: string | null;
    approved_ms: number | null;
    comment: string | null;
    applied_by: string | null;
    applied_ms: number | null;
    error: string | null;
    created_ms: number;
    updated_ms: number;
    commit: { branch: string | null; commit: string | null; pull_request: unknown } | null;
    plan: ChangePlan;
    bundle?: string;
    terraform?: string;
    history?: { at_ms: number; by: string; action: string; comment?: string }[];
    result?: { done: string[] } | null;
    variables?: string[];
    may_approve?: boolean;
    may_apply?: boolean;
}

interface AgentStatus {
    ready: boolean;
    model: string | null;
    why: string | null;
}

interface Draft {
    reply: string;
    change: ChangeRequest | null;
    flows: { name: string }[];
    tool_calls: { name: string; ok: boolean; error?: string }[];
}

const STATUS_COLOR: Record<string, string> = {
    proposed: 'gold', approved: 'blue', applied: 'green', failed: 'red', rejected: 'default', withdrawn: 'default',
};
const ACTION_COLOR: Record<string, string> = { create: 'green', update: 'blue', replace: 'orange', delete: 'red' };
const enc = encodeURIComponent;

function who(urn: string | null | undefined): string {
    return urn ? urn.split(':').pop() ?? urn : '—';
}

function show(v: unknown): string {
    if (v === null || v === undefined) return '—';
    if (typeof v === 'string') return v;
    return JSON.stringify(v, null, 1);
}

/** A plan as people read it: each resource, what happens to it, and each field before and after. */
export function PlanDiff({ plan, changeId }: { plan: ChangePlan; changeId?: string }) {
    if (!plan.changes.length) return <Muted>Nothing would change.</Muted>;
    return (
        <div aria-label="Plan">
            {plan.changes.map((c) => (
                <section key={`${c.kind}/${c.key}`} style={{ marginBottom: 12 }} aria-label={`${c.action} ${c.kind}/${c.key}`}>
                    <Space wrap>
                        <Tag color={ACTION_COLOR[c.action]}>{c.action}</Tag>
                        <code>{c.kind}/{c.key}</code>
                        <Muted>{c.terraform_type}</Muted>
                        {c.kind === 'flows' && c.action !== 'delete' && changeId && (
                            <Link to={`/flows?change=${enc(changeId)}&preview=${enc(c.key)}`}>Open in canvas</Link>
                        )}
                    </Space>
                    <Table
                        size="small"
                        pagination={false}
                        rowKey="field"
                        dataSource={c.fields}
                        style={{ marginTop: 4 }}
                        columns={[
                            { title: 'Field', dataIndex: 'field', key: 'field', width: 160, render: (v: string) => <code>{v}</code> },
                            {
                                title: 'Before', dataIndex: 'before', key: 'before',
                                render: (v: unknown) => <pre style={{ margin: 0, whiteSpace: 'pre-wrap', maxHeight: 160, overflow: 'auto' }}>{show(v)}</pre>,
                            },
                            {
                                title: 'After', dataIndex: 'after', key: 'after',
                                render: (v: unknown) => <pre style={{ margin: 0, whiteSpace: 'pre-wrap', maxHeight: 160, overflow: 'auto' }}>{show(v)}</pre>,
                            },
                        ]}
                    />
                </section>
            ))}
        </div>
    );
}

/** "Ask an agent": a change in plain words, drafted by Cairn's assistant through the AI gateway and proposed. */
function AskAnAgent({ onProposed }: { onProposed: (c: ChangeRequest) => void }) {
    const { client } = useConsumerApi();
    const [status, setStatus] = useState<AgentStatus | null>(null);
    const [prompt, setPrompt] = useState('');
    const [busy, setBusy] = useState(false);
    const [draft, setDraft] = useState<Draft | null>(null);
    const [error, setError] = useState<string | null>(null);
    useEffect(() => {
        client.request<AgentStatus>('GET', '/v1/iac/changes/assistant').then(setStatus).catch(() => setStatus(null));
    }, [client]);
    const ask = async () => {
        setBusy(true);
        setError(null);
        try {
            const out = await client.request<Draft>('POST', '/v1/iac/changes/draft', { prompt });
            setDraft(out);
            if (out.change) onProposed(out.change);
        } catch (e) {
            setError(describeError(e));
        } finally {
            setBusy(false);
        }
    };
    return (
        <section aria-label="Ask an agent" style={{ marginBottom: 16, maxWidth: 900 }}>
            <Typography.Title level={5} style={{ marginTop: 0 }}>Ask an agent</Typography.Title>
            {status && !status.ready ? (
                <Alert type="info" showIcon message="Ask an agent is not available" description={status.why} />
            ) : (
                <>
                    <Input.TextArea
                        aria-label="Describe the change"
                        value={prompt}
                        onChange={(e) => setPrompt(e.target.value)}
                        autoSize={{ minRows: 2, maxRows: 6 }}
                        placeholder="Build a nightly flow that refreshes revenue, checks it, then emails the report"
                    />
                    <Space style={{ marginTop: 8 }}>
                        <Button type="primary" loading={busy} disabled={!prompt.trim()} onClick={ask}>Draft the change</Button>
                        <Muted>It is proposed for approval; nothing changes until someone approves and applies it.</Muted>
                    </Space>
                </>
            )}
            {error && <Alert style={{ marginTop: 8 }} type="error" showIcon message="The agent could not draft it" description={error} />}
            {draft && (
                <Alert
                    style={{ marginTop: 8 }}
                    type={draft.change ? 'success' : 'warning'}
                    showIcon
                    message={draft.change ? `Proposed: ${draft.change.title}` : 'Nothing was proposed'}
                    description={
                        <>
                            <div>{draft.reply}</div>
                            {draft.change && draft.flows.map((f) => (
                                <div key={f.name}>
                                    <Link to={`/flows?change=${enc(draft.change!.id)}&preview=${enc(f.name)}`}>Open the flow {f.name} in the canvas</Link>
                                </div>
                            ))}
                        </>
                    }
                />
            )}
        </section>
    );
}

function ChangeDetail({ id, onChanged }: { id: string; onChanged: () => void }) {
    const { client } = useConsumerApi();
    const [cr, setCr] = useState<ChangeRequest | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [comment, setComment] = useState('');
    const [vars, setVars] = useState<Record<string, string>>({});
    const [busy, setBusy] = useState(false);
    const [folders, setFolders] = useState<{ id: string; name: string }[]>([]);
    const [folder, setFolder] = useState<string | undefined>();
    const [drift, setDrift] = useState<ChangePlan | null>(null);

    const load = useCallback(async () => {
        try {
            setCr(await client.request<ChangeRequest>('GET', `/v1/iac/changes/${enc(id)}`));
        } catch (e) {
            setError(describeError(e));
        }
    }, [client, id]);
    useEffect(() => {
        void load();
        client.request<{ id: string; name: string }[]>('GET', '/v1/git-folders').then(setFolders).catch(() => setFolders([]));
    }, [load, client]);

    const act = async (what: 'approve' | 'reject' | 'apply' | 'commit') => {
        setBusy(true);
        setError(null);
        setDrift(null);
        try {
            const body = what === 'apply' ? { variables: vars } : what === 'commit' ? { git_folder: folder } : { comment };
            await client.request('POST', `/v1/iac/changes/${enc(id)}/${what}`, body);
            setVars({});
            await load();
            onChanged();
        } catch (e) {
            const err = e as Error & { body?: { plan?: ChangePlan } };
            setError(describeError(e));
            if (err.body?.plan) setDrift(err.body.plan);
            await load();
        } finally {
            setBusy(false);
        }
    };

    if (!cr) return error ? <Alert type="error" showIcon message={error} /> : <Muted>Loading the change{'…'}</Muted>;
    return (
        <section aria-label={`Change ${cr.title}`}>
            <Space align="center" wrap>
                <Typography.Title level={4} style={{ margin: 0 }}>{cr.title}</Typography.Title>
                <Tag color={STATUS_COLOR[cr.status]}>{cr.status}</Tag>
            </Space>
            <Descriptions size="small" column={2} style={{ marginTop: 8, maxWidth: 900 }}>
                <Descriptions.Item label="Proposed by">{cr.agent ? `${cr.agent} for ${who(cr.sponsor)}` : who(cr.author)}</Descriptions.Item>
                <Descriptions.Item label="When">{new Date(cr.created_ms).toLocaleString()}</Descriptions.Item>
                <Descriptions.Item label="Why" span={2}>{cr.intent}</Descriptions.Item>
                {cr.approved_by && <Descriptions.Item label="Approved by">{who(cr.approved_by)}{cr.comment ? `: ${cr.comment}` : ''}</Descriptions.Item>}
                {cr.applied_by && <Descriptions.Item label="Applied by">{who(cr.applied_by)}</Descriptions.Item>}
                {cr.commit?.branch && <Descriptions.Item label="Branch"><code>{cr.commit.branch}</code></Descriptions.Item>}
                {cr.allow_delete && <Descriptions.Item label="Deletes"><Tag color="red">allowed</Tag></Descriptions.Item>}
            </Descriptions>
            {error && <Alert style={{ margin: '8px 0' }} type="error" showIcon message={error} />}
            {drift && (
                <Alert style={{ margin: '8px 0' }} type="warning" showIcon message="It changed since it was approved"
                    description={<PlanDiff plan={drift} changeId={cr.id} />} />
            )}
            {cr.error && <Alert style={{ margin: '8px 0' }} type="error" showIcon message="Applying failed" description={cr.error} />}
            <Tabs
                items={[
                    { key: 'plan', label: 'What it changes', children: <PlanDiff plan={cr.plan} changeId={cr.id} /> },
                    { key: 'yaml', label: 'Bundle', children: <pre aria-label="Bundle YAML" style={{ whiteSpace: 'pre-wrap' }}>{cr.bundle}</pre> },
                    { key: 'tf', label: 'Terraform', children: <pre aria-label="Terraform" style={{ whiteSpace: 'pre-wrap' }}>{cr.terraform}</pre> },
                    {
                        key: 'history', label: 'History',
                        children: (
                            <ul aria-label="History">
                                {(cr.history ?? []).map((h, i) => (
                                    <li key={i}>{new Date(h.at_ms).toLocaleString()} {'·'} {who(h.by)} {h.action}{h.comment ? `: ${h.comment}` : ''}</li>
                                ))}
                                {(cr.result?.done ?? []).map((l) => <li key={l}><code>{l}</code></li>)}
                            </ul>
                        ),
                    },
                ]}
            />
            {cr.status === 'proposed' && (
                <section aria-label="Decide" style={{ maxWidth: 900 }}>
                    {cr.may_approve ? (
                        <Muted>You may approve it: you could make this change yourself. You then apply it, as you.</Muted>
                    ) : (
                        <Muted>Someone with the right to make every change in it approves it (not the person who proposed it).</Muted>
                    )}
                    <Input.TextArea aria-label="Comment" value={comment} onChange={(e) => setComment(e.target.value)} placeholder="Comment (optional)"
                        autoSize={{ minRows: 1, maxRows: 4 }} style={{ margin: '8px 0' }} />
                    <Space>
                        <Button type="primary" disabled={!cr.may_approve} loading={busy} onClick={() => act('approve')}>Approve</Button>
                        <Button danger loading={busy} onClick={() => act('reject')}>Reject</Button>
                    </Space>
                </section>
            )}
            {cr.status === 'approved' && cr.may_apply && (
                <section aria-label="Apply" style={{ maxWidth: 900, marginTop: 8 }}>
                    <Muted>Cairn plans it again first, and stops if anything changed since you approved it.</Muted>
                    {(cr.variables ?? []).map((v) => (
                        <label key={v} style={{ display: 'block', marginTop: 8 }}>
                            <code>{v}</code> (used for this apply only, never kept)
                            <Input.Password aria-label={`Value of ${v}`} value={vars[v] ?? ''} onChange={(e) => setVars({ ...vars, [v]: e.target.value })} />
                        </label>
                    ))}
                    <Button style={{ marginTop: 8 }} type="primary" loading={busy} onClick={() => act('apply')}>Apply</Button>
                </section>
            )}
            {['proposed', 'approved'].includes(cr.status) && folders.length > 0 && (
                <Space style={{ marginTop: 12 }} wrap aria-label="Commit to git">
                    <div style={{ minWidth: 260 }}>
                        <Picker id={`iac-git-folder-${id}`} ariaLabel="Git folder" placeholder="A git folder of yours"
                            field={{ name: 'git_folder', label: 'Git folder', type: 'picker', picker: 'git_folder' }}
                            value={folder} onChange={(v) => setFolder(v ? String(v) : undefined)} />
                    </div>
                    <Button disabled={!folder} loading={busy} onClick={() => act('commit')}>Commit to a branch for review</Button>
                </Space>
            )}
        </section>
    );
}

/** Change requests: what agents and people propose to Cairn's resources as code, for approval, then applied. */
export function ChangesPanel({ initial }: { initial?: string | null }) {
    const { client } = useConsumerApi();
    const [items, setItems] = useState<ChangeRequest[] | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [current, setCurrent] = useState<string | null>(initial ?? null);
    const [status, setStatus] = useState<string>('');
    const load = useCallback(async () => {
        try {
            setItems(await client.request<ChangeRequest[]>('GET', '/v1/iac/changes', undefined, status ? { query: { status } } : undefined));
        } catch (e) {
            setError(describeError(e));
        }
    }, [client, status]);
    useEffect(() => {
        void load();
    }, [load]);
    const rows = useMemo(() => items ?? [], [items]);
    return (
        <section aria-label="Change requests">
            <AskAnAgent onProposed={(c) => { void load(); setCurrent(c.id); }} />
            <Space style={{ marginBottom: 8 }} wrap>
                <Select aria-label="Status" value={status} onChange={setStatus} style={{ minWidth: 160 }}
                    options={[{ value: '', label: 'Every status' }, ...['proposed', 'approved', 'applied', 'failed', 'rejected', 'withdrawn'].map((s) => ({ value: s, label: s }))]} />
                <Muted>Agents propose with the iac:propose scope (MCP tools iac_plan and iac_propose); people approve and apply.</Muted>
            </Space>
            {error && <Alert type="error" showIcon message={error} />}
            <Table<ChangeRequest>
                size="middle"
                rowKey="id"
                loading={!items && !error}
                dataSource={rows}
                pagination={rows.length > 20 ? { pageSize: 20, showSizeChanger: false } : false}
                onRow={(r) => ({ onClick: () => setCurrent(r.id) })}
                columns={[
                    { title: 'Change', dataIndex: 'title', key: 'title', render: (v: string, r) => <Button type="link" style={{ padding: 0 }} onClick={() => setCurrent(r.id)}>{v}</Button> },
                    { title: 'Status', dataIndex: 'status', key: 'status', render: (v: string) => <Tag color={STATUS_COLOR[v]}>{v}</Tag> },
                    {
                        title: 'What', key: 'counts',
                        render: (_: unknown, r) => Object.entries(r.plan?.counts ?? {}).filter(([k, n]) => n && k !== 'unchanged').map(([k, n]) => `${n} to ${k}`).join(', ') || '—',
                    },
                    { title: 'Resources', dataIndex: 'kinds', key: 'kinds', render: (v: string[]) => v.map((k) => <Tag key={k}>{k}</Tag>) },
                    { title: 'Proposed by', key: 'who', render: (_: unknown, r) => (r.agent ? `${r.agent} for ${who(r.sponsor)}` : who(r.author)) },
                    { title: 'When', dataIndex: 'created_ms', key: 'when', render: (v: number) => new Date(v).toLocaleString() },
                ]}
            />
            {current && (
                <div style={{ marginTop: 16 }}>
                    <ChangeDetail key={current} id={current} onChanged={() => void load()} />
                </div>
            )}
        </section>
    );
}
