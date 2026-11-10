import { Alert, Button, Descriptions, Empty, Input, Modal, Select, Space, Spin, Table, Tabs, Tag, Typography, message } from 'antd';
import React, { useEffect, useState } from 'react';
import { useHistory, useLocation } from 'react-router-dom';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import { ErrorNotice, notifyError } from '../components/ErrorNotice';
import { Muted } from '../components/tokens';
import { RESOURCE_SPECS } from './catalog';
import { ActionDialog, createAction } from './ResourceView';

/** A managed Airflow deployment as `GET /v1/airflow/deployments` lists it. */
export interface AirflowDeployment {
    name: string;
    runtime: string;
    state: string;
    environment: string;
    airflow_version: string;
    executor: string | null;
    team: string | null;
    url: string | null;
    image: string | null;
    custom_image: string | null;
    dags_release: string | null;
    pipeline_user: string;
    promotes_to: string | null;
    approval: { required: boolean } | null;
    hibernation: { awake: { days: string[]; from: string; to: string }[]; timezone: string } | null;
    error?: string | null;
}

export interface AirflowList {
    deployments: AirflowDeployment[];
    runtime: string | null;
    versions: string[];
    executors: string[];
}

interface Deploy {
    id: string; seq: number; kind: 'dags' | 'image'; state: string; image: string | null; sha256: string | null;
    files: number | null; git_sha: string | null; note: string | null; source: string | null; created_by: string;
    created_ms: number; checked: { dags?: string[]; import_errors?: Record<string, string> } | null; error: string | null;
}

interface Promotion {
    id: string; deploy: string; source: string; target: string; state: string; requested_by: string;
    requested_ms: number; decided_by: string | null; comment: string | null; error: string | null;
}

export const NEW_DEPLOYMENT = 'New deployment';
const BASE = '/v1/airflow/deployments';
const COMPONENTS = ['scheduler', 'api-server', 'dag-processor', 'triggerer'];

const stateColor = (s?: string) => (s === 'ready' || s === 'healthy' || s === 'live' || s === 'applied' ? 'green'
    : s === 'failed' || s === 'refused' || s === 'down' || s === 'rejected' ? 'red'
        : s === 'hibernating' || s === 'superseded' ? 'default' : s === 'degraded' || s === 'pending' ? 'orange' : 'blue');

/** Who and where a deploy came from, in words. */
export function deploySource(d: Pick<Deploy, 'source' | 'git_sha'>): string {
    const s = d.source ?? '';
    const sha = d.git_sha ? ` (git ${d.git_sha.slice(0, 7)})` : '';
    if (s.startsWith('rollback:')) return `rollback to ${s.slice(9)}${sha}`;
    if (s.startsWith('promoted:')) return `promoted from ${s.slice(9).split('/')[0]}${sha}`;
    if (s.startsWith('git:')) return `git folder${sha}`;
    return `deployed${sha}`;
}

function Status({ dep, onChange }: { dep: AirflowDeployment; onChange: () => void }) {
    const { client } = useConsumerApi();
    const q = useDheQuery<Record<string, any>>(`airflow:${dep.name}:status`, (c, signal) =>
        c.request('GET', `${BASE}/${dep.name}/status`, undefined, { signal }), { staleMs: 10_000, pollMs: 30_000 });
    const act = async (path: string) => {
        try {
            await client.request('POST', `${BASE}/${dep.name}/${path}`, {});
            message.success(path === 'wake' ? 'Awake' : 'Hibernating');
            await q.refresh();
            onChange();
        } catch (e) {
            notifyError(e);
        }
    };
    if (q.error) return <ErrorNotice error={q.error} area="The deployment's status" />;
    const s = q.data;
    if (!s) return <Spin />;
    const comps = s.components ?? {};
    return (
        <>
            <Space style={{ marginBottom: 12 }} wrap>
                <Tag color={stateColor(s.health)}>{s.health}</Tag>
                {dep.url && <a href={dep.url} target="_blank" rel="noreferrer">Open Airflow</a>}
                {dep.environment !== 'prod' && dep.state === 'ready' && <Button onClick={() => act('hibernate')}>Hibernate</Button>}
                {dep.state === 'hibernating' && <Button type="primary" onClick={() => act('wake')}>Wake</Button>}
            </Space>
            {(s.problems ?? []).map((p: string) => <Alert key={p} type="warning" showIcon message={p} style={{ marginBottom: 8 }} />)}
            <Descriptions size="small" bordered column={1}>
                {['metadatabase', 'scheduler', 'dag_processor', 'triggerer'].map((k) => (
                    <Descriptions.Item key={k} label={k.replace('_', ' ')}>{comps[k]?.status ?? '—'}</Descriptions.Item>
                ))}
                <Descriptions.Item label="Scheduler heartbeat">
                    {s.scheduler_heartbeat_age_s != null ? `${s.scheduler_heartbeat_age_s} s ago` : '—'}</Descriptions.Item>
                <Descriptions.Item label="Tasks">
                    {s.tasks ? `${s.tasks.running} running, ${s.tasks.queued} queued` : '—'}</Descriptions.Item>
                <Descriptions.Item label="Pipeline user">{dep.pipeline_user}</Descriptions.Item>
                <Descriptions.Item label="Image">{dep.image ?? '—'}</Descriptions.Item>
            </Descriptions>
            {(s.import_errors ?? []).length > 0 && (
                <Table size="small" rowKey="file" style={{ marginTop: 12 }} pagination={false} dataSource={s.import_errors}
                    columns={[{ title: 'DAG file', dataIndex: 'file' }, { title: 'Import error', dataIndex: 'error', ellipsis: true }]} />
            )}
        </>
    );
}

function Deploys({ dep, list }: { dep: AirflowDeployment; list: AirflowList }) {
    const { client } = useConsumerApi();
    const q = useDheQuery<{ deploys: Deploy[] }>(`airflow:${dep.name}:deploys`, (c, signal) =>
        c.request('GET', `${BASE}/${dep.name}/deploys`, undefined, { signal }));
    const p = useDheQuery<{ promotions: Promotion[] }>(`airflow:${dep.name}:promotions`, (c, signal) =>
        c.request('GET', `${BASE}/${dep.name}/promotions`, undefined, { signal }));
    const [promoting, setPromoting] = useState(false);
    const [target, setTarget] = useState<string | undefined>(dep.promotes_to ?? undefined);
    const [comment, setComment] = useState('');
    const run = async (fn: () => Promise<unknown>, ok: string) => {
        try {
            await fn();
            message.success(ok);
            await Promise.all([q.refresh(), p.refresh()]);
        } catch (e) {
            notifyError(e);
        }
    };
    const others = list.deployments.filter((d) => d.name !== dep.name && d.environment !== dep.environment);
    return (
        <>
            <Space style={{ marginBottom: 12 }} wrap>
                <Button type="primary" disabled={!dep.dags_release} onClick={() => setPromoting(true)}>Promote</Button>
                <Muted>Deploy from a project with <code>cairn airflow deploy {dep.name} --dags-only</code>.</Muted>
            </Space>
            {q.error ? <ErrorNotice error={q.error} area="Deploys" /> : (
                <Table<Deploy> size="small" rowKey="id" loading={q.loading} dataSource={q.data?.deploys ?? []} pagination={false}
                    locale={{ emptyText: 'Nothing deployed yet' }}
                    columns={[
                        { title: '#', dataIndex: 'seq' },
                        { title: 'What', render: (_, d) => (d.kind === 'dags' ? `DAGs (${d.files ?? 0} files)` : d.image) },
                        { title: 'State', dataIndex: 'state', render: (v) => <Tag color={stateColor(v)}>{v}</Tag> },
                        { title: 'From', render: (_, d) => deploySource(d) },
                        { title: 'By', dataIndex: 'created_by' },
                        { title: 'When', dataIndex: 'created_ms', render: (v) => new Date(v).toLocaleString() },
                        { title: 'Note', dataIndex: 'note', render: (v, d) => v ?? (d.checked?.import_errors &&
                            Object.keys(d.checked.import_errors).length ? `does not import: ${Object.keys(d.checked.import_errors).join(', ')}` : '') },
                        { title: '', render: (_, d) => (d.state === 'superseded' ? (
                            <Button size="small" onClick={() => run(() => client.request('POST', `${BASE}/${dep.name}/deploys/${d.id}/rollback`, {}),
                                `Rolled back to #${d.seq}`)}>Roll back</Button>) : null) },
                    ]} />
            )}
            <Typography.Title level={5} style={{ marginTop: 16 }}>Promotions</Typography.Title>
            <Table<Promotion> size="small" rowKey="id" dataSource={p.data?.promotions ?? []} pagination={false}
                locale={{ emptyText: 'No promotions' }}
                columns={[
                    { title: 'From', dataIndex: 'source' }, { title: 'To', dataIndex: 'target' },
                    { title: 'State', dataIndex: 'state', render: (v) => <Tag color={stateColor(v)}>{v}</Tag> },
                    { title: 'Asked by', dataIndex: 'requested_by' }, { title: 'Decided by', dataIndex: 'decided_by' },
                    { title: '', render: (_, x) => (x.state === 'pending' ? (
                        <Space>
                            <Button size="small" type="primary" onClick={() => run(() => client.request('POST',
                                `/v1/airflow/promotions/${x.id}/approve`, { comment: '' }), 'Approved and deployed')}>Approve</Button>
                            <Button size="small" danger onClick={() => run(() => client.request('POST',
                                `/v1/airflow/promotions/${x.id}/reject`, { comment: '' }), 'Rejected')}>Reject</Button>
                        </Space>) : null) },
                ]} />
            <Modal title={`Promote ${dep.name}'s live DAGs`} open={promoting} okText="Promote" onCancel={() => setPromoting(false)}
                onOk={async () => {
                    await run(() => client.request('POST', `${BASE}/${dep.name}/promote`, { to: target, comment }),
                        'Promoted (or waiting for approval)');
                    setPromoting(false);
                }}>
                <Typography.Paragraph>To</Typography.Paragraph>
                <Select style={{ width: '100%' }} value={target} onChange={setTarget} aria-label="Promote to"
                    options={others.map((d) => ({ value: d.name, label: `${d.name} (${d.environment}${d.approval?.required ? ', needs approval' : ''})` }))} />
                <Input.TextArea style={{ marginTop: 12 }} placeholder="Why (optional)" value={comment} aria-label="Comment"
                    onChange={(e) => setComment(e.target.value)} />
            </Modal>
        </>
    );
}

function Logs({ dep }: { dep: AirflowDeployment }) {
    const [component, setComponent] = useState('scheduler');
    const q = useDheQuery<{ log: string }>(`airflow:${dep.name}:logs:${component}`, (c, signal) =>
        c.request('GET', `${BASE}/${dep.name}/logs`, undefined, { signal, query: { component, tail: '300' } }));
    return (
        <>
            <Select style={{ width: 200, marginBottom: 12 }} value={component} onChange={setComponent} aria-label="Component"
                options={COMPONENTS.map((c) => ({ value: c }))} />
            {q.error ? <ErrorNotice error={q.error} area="Logs" /> : q.loading ? <Spin /> : (
                <pre style={{ maxHeight: 420, overflow: 'auto', fontSize: 12 }}>{q.data?.log || '(empty)'}</pre>
            )}
        </>
    );
}

/** Managed Airflow: the deployments Cairn runs, each with its status, deploys (rollback, promotion) and logs. */
export function AirflowDeploymentsView() {
    const q = useDheQuery<AirflowList>(`resource:${BASE}`, (c, signal) => c.request<AirflowList>('GET', BASE, undefined, { signal }));
    const [creating, setCreating] = useState(false);
    const [selected, setSelected] = useState<string | null>(null);
    const location = useLocation();
    const history = useHistory();
    const spec = RESOURCE_SPECS['airflow-deployments'];
    const create = createAction(spec);
    useEffect(() => {                                     // `?action=New deployment` (the command palette)
        const params = new URLSearchParams(location.search);
        if (params.get('action') !== NEW_DEPLOYMENT) return;
        setCreating(true);
        params.delete('action');
        history.replace({ pathname: location.pathname, search: params.toString() ? `?${params}` : '' });
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [location.search]);
    if (q.error) return <ErrorNotice error={q.error} area="Managed Airflow" />;
    const list = q.data;
    if (!list) return <Spin />;
    const dep = list.deployments.find((d) => d.name === selected) ?? null;
    return (
        <>
            <Space style={{ marginBottom: 12 }}>
                <Button type="primary" onClick={() => setCreating(true)} disabled={!list.runtime}>{NEW_DEPLOYMENT}</Button>
                {!list.runtime && <Muted>Managed Airflow is not enabled on this Cairn (CAIRN_AIRFLOW_RUNTIME).</Muted>}
            </Space>
            {list.deployments.length === 0 ? <Empty description="No deployments yet" /> : (
                <Table<AirflowDeployment> size="small" rowKey="name" dataSource={list.deployments} pagination={false}
                    onRow={(r) => ({ onClick: () => setSelected(r.name), style: { cursor: 'pointer' } })}
                    columns={[
                        { title: 'Deployment', dataIndex: 'name', render: (v) => <a>{v}</a> },
                        { title: 'Environment', dataIndex: 'environment', render: (v) => <Tag>{v}</Tag> },
                        { title: 'State', dataIndex: 'state', render: (v) => <Tag color={stateColor(v)}>{v}</Tag> },
                        { title: 'Airflow', dataIndex: 'airflow_version' },
                        { title: 'Team', dataIndex: 'team', render: (v) => v ?? '—' },
                        { title: 'Promotes to', dataIndex: 'promotes_to', render: (v) => v ?? '—' },
                    ]} />
            )}
            {creating && create && (
                <ActionDialog spec={spec} action={create} row={null} onClose={() => setCreating(false)}
                    onDone={() => { setCreating(false); void q.refresh(); }} />
            )}
            {dep && (
                <div style={{ marginTop: 16 }}>
                    <Typography.Title level={4}>{dep.name}</Typography.Title>
                    {dep.error && <Alert type="error" showIcon message={dep.error} style={{ marginBottom: 8 }} />}
                    <Tabs items={[
                        { key: 'status', label: 'Status', children: <Status dep={dep} onChange={() => void q.refresh()} /> },
                        { key: 'deploys', label: 'Deploys & promotion', children: <Deploys dep={dep} list={list} /> },
                        { key: 'logs', label: 'Logs', children: <Logs dep={dep} /> },
                    ]} />
                </div>
            )}
        </>
    );
}
