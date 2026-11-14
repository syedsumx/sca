import { Alert, Button, Descriptions, Empty, Form, Input, InputNumber, Modal, Select, Space, Spin, Switch, Table, Tabs,
    Tag, Typography, message } from 'antd';
import React, { useEffect, useState } from 'react';
import { useHistory, useLocation } from 'react-router-dom';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import { ErrorNotice, describeError, notifyError } from '../components/ErrorNotice';
import { DiagnoseButton } from '../components/SreAgent';
import { Muted } from '../components/tokens';
import { When } from '../utils/time';

/** A managed PostgreSQL instance as `GET /v1/databases/instances` lists it. */
export interface PgInstance {
    name: string;
    runtime: string;
    state: string;
    version: string | null;
    cpu?: number;
    memory_mb?: number;
    storage_gb?: number;
    replicas: number | null;
    pooler: { mode: string; instances: number } | null;
    maintenance_window: { day: string; start: string; hours: number } | null;
    host?: string | null;
    port?: number | null;
    pending: { op: string }[];
    parameters: { name: string; value: string }[];
    backup_schedules: { name: string; every_hours: number; retention_days: number; verify: boolean }[];
    error?: string | null;
    note?: string;
}

export interface PgList {
    instances: PgInstance[];
    runtime: string | null;
    backups: boolean;
    parameters: string[];
}

interface PgBackup {
    id: string; schedule: string; state: string; started_ms: number; finished_ms: number | null; bytes: number | null;
    verified: string | null; error: string | null;
}

export const NEW_INSTANCE = 'New instance';
const BASE = '/v1/databases/instances';
const MEMORY = [512, 1024, 2048, 4096, 8192, 16384, 32768];
const DAYS = ['daily', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];

export function bytes(n: number | null | undefined): string {
    if (n == null) return '—';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let v = n;
    let i = 0;
    while (v >= 1024 && i < units.length - 1) {
        v /= 1024;
        i += 1;
    }
    return `${v < 10 && i ? v.toFixed(1) : Math.round(v)} ${units[i]}`;
}

/** A local date and time (an <input type="datetime-local"> value) as the ISO time with zone the API takes. */
export function toIso(local: string): string {
    return new Date(local).toISOString();
}

const healthColor = (h?: string) => (h === 'healthy' ? 'green' : h === 'degraded' ? 'orange' : h ? 'red' : 'default');

/** The create form's values as the API's body. */
export function createBody(v: Record<string, unknown>): Record<string, unknown> {
    const body: Record<string, unknown> = {
        name: v.name, version: v.version ?? '16', cpu: v.cpu ?? 1, memory_mb: v.memory_mb ?? 1024,
        storage_gb: v.storage_gb ?? 10, replicas: v.replicas ?? 0,
    };
    if (v.pooling) body.pooler = { mode: v.pool_mode ?? 'transaction', instances: 2 };
    if (v.window_day) body.maintenance_window = { day: v.window_day, start: v.window_start || '02:00', hours: v.window_hours ?? 2 };
    return body;
}

function CreateInstance({ list, open, onClose, onDone }: { list: PgList; open: boolean; onClose: () => void;
    onDone: () => void }) {
    const { client } = useConsumerApi();
    const [form] = Form.useForm();
    const [busy, setBusy] = useState(false);
    const k8s = list.runtime === 'kubernetes';
    const submit = async () => {
        const v = await form.validateFields();
        setBusy(true);
        try {
            await client.request('POST', BASE, createBody(v));
            message.success(`Instance ${v.name} created`);
            form.resetFields();
            onDone();
        } catch (e) {
            notifyError(e);
        } finally {
            setBusy(false);
        }
    };
    return (
        <Modal title="New PostgreSQL instance" open={open} onCancel={onClose} onOk={submit} okText="Create"
            confirmLoading={busy} destroyOnClose>
            <Form form={form} layout="vertical" initialValues={{ version: '16', cpu: 1, memory_mb: 1024, storage_gb: 10, replicas: 0 }}>
                <Form.Item name="name" label="Name" rules={[{ required: true, pattern: /^[a-z]([a-z0-9-]{0,22}[a-z0-9])?$/,
                    message: "Lowercase letters, digits and '-', starting with a letter (up to 24)" }]}>
                    <Input placeholder="orders-pg" />
                </Form.Item>
                <Form.Item name="version" label="PostgreSQL version">
                    <Select options={['14', '15', '16', '17'].map((x) => ({ value: x, label: `PostgreSQL ${x}` }))} />
                </Form.Item>
                <Space wrap>
                    <Form.Item name="cpu" label="CPU (cores)"><InputNumber min={0.25} max={64} step={0.25} /></Form.Item>
                    <Form.Item name="memory_mb" label="Memory">
                        <Select style={{ width: 120 }} options={MEMORY.map((m) => ({ value: m, label: bytes(m * 1024 * 1024) }))} />
                    </Form.Item>
                    <Form.Item name="storage_gb" label="Storage (GB)"><InputNumber min={1} max={16384} /></Form.Item>
                </Space>
                <Form.Item name="replicas" label="Standbys (high availability)"
                    extra={k8s ? 'Standbys stream from the primary; one is promoted if the primary is lost.'
                        : 'Standbys need the Kubernetes runtime.'}>
                    <InputNumber min={0} max={5} disabled={!k8s} />
                </Form.Item>
                <Space wrap>
                    <Form.Item name="pooling" label="Connection pooling (PgBouncer)" valuePropName="checked">
                        <Switch disabled={!k8s} />
                    </Form.Item>
                    <Form.Item name="pool_mode" label="Pool mode">
                        <Select style={{ width: 140 }} disabled={!k8s} options={[{ value: 'transaction' }, { value: 'session' }]}
                            placeholder="transaction" />
                    </Form.Item>
                </Space>
                <Typography.Text strong>Maintenance window (UTC)</Typography.Text>
                <Space wrap>
                    <Form.Item name="window_day" label="Day"><Select allowClear style={{ width: 110 }} placeholder="none"
                        options={DAYS.map((d) => ({ value: d }))} /></Form.Item>
                    <Form.Item name="window_start" label="Start"><Input style={{ width: 90 }} placeholder="02:00" /></Form.Item>
                    <Form.Item name="window_hours" label="Hours"><InputNumber min={1} max={8} placeholder="2" /></Form.Item>
                </Space>
            </Form>
        </Modal>
    );
}

function Status({ name }: { name: string }) {
    const { client } = useConsumerApi();
    const q = useDheQuery<Record<string, any>>(`pg:${name}:status`, (c, signal) =>
        c.request('GET', `${BASE}/${name}/status`, undefined, { signal }), { staleMs: 10_000, pollMs: 30_000 });
    if (q.error) return <ErrorNotice error={q.error} area="The instance's status" />;
    const s = q.data;
    if (!s) return <Spin />;
    const act = async (path: string, body?: unknown) => {
        try {
            await client.request('POST', `${BASE}/${name}/${path}`, body ?? {});
            message.success('Done');
            await q.refresh();
        } catch (e) {
            notifyError(e);
        }
    };
    const rs = s.runtime_state ?? {};
    return (
        <>
            <Space style={{ marginBottom: 12 }}>
                <Tag color={healthColor(s.health)}>{s.health}</Tag>
                {s.pending?.length ? <Button onClick={() => act('apply-pending')}>Apply pending changes now</Button> : null}
                {s.runtime !== 'external' && <Button onClick={() => act('restart')}>Restart</Button>}
                {s.runtime === 'kubernetes' && <Button onClick={() => act('switchover')}>Switch over to a standby</Button>}
                <DiagnoseButton instance={name} />
            </Space>
            {(s.problems ?? []).map((p: string) => <Alert key={p} type="warning" showIcon message={p} style={{ marginBottom: 8 }} />)}
            <Descriptions size="small" bordered column={1}>
                <Descriptions.Item label="Server">{s.server_version ?? '—'}{s.in_recovery ? ' (standby)' : ''}</Descriptions.Item>
                <Descriptions.Item label="Connections">
                    {s.connections ? `${s.connections.total} of ${s.connections.max} (${s.connections.active} active)` : '—'}
                </Descriptions.Item>
                <Descriptions.Item label="Storage">
                    {s.storage ? `${bytes(s.storage.databases_bytes)} data, ${bytes(s.storage.wal_bytes)} WAL` : '—'}
                    {s.storage?.percent != null ? ` (${s.storage.percent}% of ${bytes(s.storage.limit_bytes)})` : ''}
                </Descriptions.Item>
                <Descriptions.Item label="WAL archiving">
                    {s.wal_archiving ? (s.wal_archiving.enabled ? `${s.wal_archiving.ok ? 'working' : 'failing'}, last ${
                        s.wal_archiving.last_archived_wal ?? 'none yet'}` : 'off') : '—'}
                </Descriptions.Item>
                <Descriptions.Item label="Last backup">
                    {s.last_backup ? new Date(s.last_backup.finished_ms).toLocaleString() : 'none yet'}
                </Descriptions.Item>
                <Descriptions.Item label="Replication">
                    {(s.replication ?? []).length ? s.replication.map((r: any) =>
                        `${r.standby}: ${r.state}, ${bytes(r.lag_bytes)} behind`).join('; ') : 'no standbys'}
                </Descriptions.Item>
                {rs.primary && <Descriptions.Item label="Primary">{rs.primary}</Descriptions.Item>}
            </Descriptions>
        </>
    );
}

function Backups({ inst, list }: { inst: PgInstance; list: PgList }) {
    const { client } = useConsumerApi();
    const name = inst.name;
    const q = useDheQuery<{ backups: PgBackup[] }>(`pg:${name}:backups`, (c, signal) =>
        c.request('GET', `${BASE}/${name}/backups`, undefined, { signal }));
    const [restore, setRestore] = useState(false);
    const [form] = Form.useForm();
    const [schedForm] = Form.useForm();
    const run = async (fn: () => Promise<unknown>, ok: string) => {
        try {
            await fn();
            message.success(ok);
            await q.refresh();
        } catch (e) {
            notifyError(e);
        }
    };
    if (!list.backups) return <Alert type="info" showIcon message="Backups are not set up on this Cairn (CAIRN_PG_BACKUP_STORE)." />;
    return (
        <>
            <Space style={{ marginBottom: 12 }} wrap>
                <Button type="primary" onClick={() => run(() => client.request('POST', `${BASE}/${name}/backups`, {}), 'Backed up')}>
                    Back up now
                </Button>
                <Button onClick={() => setRestore(true)}>Restore to a point in time</Button>
            </Space>
            {q.error ? <ErrorNotice error={q.error} area="Backups" /> : (
                <Table<PgBackup> size="small" rowKey="id" loading={q.loading} dataSource={q.data?.backups ?? []} pagination={false}
                    columns={[
                        { title: 'Backup', dataIndex: 'id' }, { title: 'Schedule', dataIndex: 'schedule' },
                        { title: 'State', dataIndex: 'state' },
                        { title: 'Finished', dataIndex: 'finished_ms', render: (v) => <When value={v} empty={'—'} /> },
                        { title: 'Size', dataIndex: 'bytes', render: (v) => bytes(v) },
                        { title: 'Restore check', dataIndex: 'verified', render: (v, b) => v ?? (b.state === 'completed' ? (
                            <Button size="small" onClick={() => run(() => client.request('POST', `${BASE}/${name}/backups/${b.id}/verify`, {}),
                                'Checked')}>Check</Button>) : '—') },
                    ]} />
            )}
            <Typography.Title level={5} style={{ marginTop: 16 }}>Schedules</Typography.Title>
            <Table size="small" rowKey="name" pagination={false} dataSource={inst.backup_schedules}
                locale={{ emptyText: 'No schedule: WAL is still archived, base backups only on demand.' }}
                columns={[{ title: 'Name', dataIndex: 'name' }, { title: 'Every (hours)', dataIndex: 'every_hours' },
                    { title: 'Kept (days)', dataIndex: 'retention_days' },
                    { title: 'Checked by a restore', dataIndex: 'verify', render: (v) => (v ? 'yes' : 'no') }]} />
            <Form form={schedForm} layout="inline" style={{ marginTop: 8 }} initialValues={{ every_hours: 24, retention_days: 7 }}
                onFinish={(v) => run(() => client.request('PUT', `${BASE}/${name}/backup-schedules/${v.name}`,
                    { every_hours: v.every_hours, retention_days: v.retention_days, verify: !!v.verify }), 'Schedule saved')}>
                <Form.Item name="name" rules={[{ required: true }]}><Input placeholder="daily" style={{ width: 110 }} /></Form.Item>
                <Form.Item name="every_hours" label="every"><InputNumber min={1} max={168} /></Form.Item>
                <Form.Item name="retention_days" label="keep days"><InputNumber min={1} max={3650} /></Form.Item>
                <Form.Item name="verify" valuePropName="checked" label="check"><Switch /></Form.Item>
                <Button htmlType="submit">Save schedule</Button>
            </Form>
            <Modal title={`Restore ${name} into a new instance`} open={restore} onCancel={() => setRestore(false)} okText="Restore"
                onOk={async () => {
                    const v = await form.validateFields();
                    const body: Record<string, unknown> = { name: v.name };
                    if (v.at) body.target_time = toIso(v.at);
                    if (v.databases) {
                        body.databases = Object.fromEntries(String(v.databases).split(',').map((p) => p.trim())
                            .filter(Boolean).map((p) => p.split('=').map((x) => x.trim())));
                    }
                    await run(() => client.request('POST', `${BASE}/${name}/restore`, body), `Restored into ${v.name}`);
                    setRestore(false);
                }}>
                <Form form={form} layout="vertical">
                    <Form.Item name="name" label="New instance" rules={[{ required: true }]}><Input placeholder={`${name}-restored`} /></Form.Item>
                    <Form.Item name="at" label="As of (your local time; empty: as late as the archive goes)">
                        <Input type="datetime-local" step={1} />
                    </Form.Item>
                    <Form.Item name="databases" label="Databases to keep, renamed (old=new, comma-separated)"
                        extra="Cairn databases not named here are dropped from the copy. Logins never carry over.">
                        <Input placeholder="orders=orders_restored" />
                    </Form.Item>
                </Form>
            </Modal>
        </>
    );
}

function Parameters({ inst, list, onChange }: { inst: PgInstance; list: PgList; onChange: () => void }) {
    const { client } = useConsumerApi();
    const [form] = Form.useForm();
    const run = async (fn: () => Promise<unknown>) => {
        try {
            const out = (await fn()) as { pending_restart?: boolean } | undefined;
            message.success(out?.pending_restart ? 'Saved: applied at the next restart (in the maintenance window)' : 'Saved');
            onChange();
        } catch (e) {
            notifyError(e);
        }
    };
    return (
        <>
            <Table size="small" rowKey="name" pagination={false} dataSource={inst.parameters}
                locale={{ emptyText: 'PostgreSQL defaults' }}
                columns={[{ title: 'Parameter', dataIndex: 'name' }, { title: 'Value', dataIndex: 'value' },
                    { title: '', render: (_, p) => <Button size="small" danger
                        onClick={() => run(() => client.request('DELETE', `${BASE}/${inst.name}/parameters/${p.name}`))}>Reset</Button> }]} />
            <Form form={form} layout="inline" style={{ marginTop: 8 }}
                onFinish={(v) => run(() => client.request('PUT', `${BASE}/${inst.name}/parameters/${v.name}`, { value: v.value }))}>
                <Form.Item name="name" rules={[{ required: true }]}>
                    <Select showSearch style={{ width: 260 }} placeholder="parameter" aria-label="Parameter"
                        options={list.parameters.map((p) => ({ value: p }))} />
                </Form.Item>
                <Form.Item name="value" rules={[{ required: true }]}><Input placeholder="value" aria-label="Value" /></Form.Item>
                <Button htmlType="submit">Set</Button>
            </Form>
        </>
    );
}

function Metrics({ name, external }: { name: string; external: boolean }) {
    const m = useDheQuery<Record<string, any>>(`pg:${name}:metrics`, (c, signal) =>
        c.request('GET', `${BASE}/${name}/metrics`, undefined, { signal }), { staleMs: 15_000 });
    const sq = useDheQuery<{ queries: any[] }>(`pg:${name}:slow`, (c, signal) =>
        c.request('GET', `${BASE}/${name}/slow-queries`, undefined, { signal, query: { limit: '10' } }));
    return (
        <>
            {m.error ? <ErrorNotice error={m.error} area="Metrics" /> : (
                <Table size="small" rowKey="database" loading={m.loading} pagination={false} dataSource={m.data?.databases ?? []}
                    columns={[{ title: 'Database', dataIndex: 'database' }, { title: 'Size', dataIndex: 'size_bytes', render: bytes },
                        { title: 'Connections', dataIndex: 'connections' }, { title: 'Commits', dataIndex: 'commits' },
                        { title: 'Rollbacks', dataIndex: 'rollbacks' },
                        { title: 'Cache hits', dataIndex: 'cache_hit_ratio', render: (v) => (v == null ? '—' : `${(v * 100).toFixed(1)}%`) },
                        { title: 'Deadlocks', dataIndex: 'deadlocks' }]} />
            )}
            <Typography.Title level={5} style={{ marginTop: 16 }}>Slowest statements</Typography.Title>
            {sq.error ? <Muted>{external ? 'Needs pg_stat_statements on the server.' : describeError(sq.error)}</Muted> : (
                <Table size="small" rowKey="queryid" loading={sq.loading} pagination={false} dataSource={sq.data?.queries ?? []}
                    columns={[{ title: 'Statement', dataIndex: 'query', ellipsis: true }, { title: 'Database', dataIndex: 'database' },
                        { title: 'Calls', dataIndex: 'calls' }, { title: 'Total ms', dataIndex: 'total_ms' },
                        { title: 'Mean ms', dataIndex: 'mean_ms' }]} />
            )}
        </>
    );
}

/** Managed Postgres: the instances Cairn runs (and the server it was given), each with its status, backups and
 *  point-in-time restore, parameters and metrics. */
export function PostgresInstancesView() {
    const q = useDheQuery<PgList>(`resource:${BASE}`, (c, signal) => c.request<PgList>('GET', BASE, undefined, { signal }));
    const [creating, setCreating] = useState(false);
    const [selected, setSelected] = useState<string | null>(null);
    const location = useLocation();
    const history = useHistory();
    useEffect(() => {                                     // `?action=New instance` (the command palette)
        const params = new URLSearchParams(location.search);
        if (params.get('action') !== NEW_INSTANCE) return;
        setCreating(true);
        params.delete('action');
        history.replace({ pathname: location.pathname, search: params.toString() ? `?${params}` : '' });
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [location.search]);
    if (q.error) return <ErrorNotice error={q.error} area="Managed Postgres" />;
    const list = q.data;
    if (!list) return <Spin />;
    const inst = list.instances.find((i) => i.name === selected) ?? null;
    return (
        <>
            <Space style={{ marginBottom: 12 }}>
                <Button type="primary" onClick={() => setCreating(true)} disabled={!list.runtime}>{NEW_INSTANCE}</Button>
                {!list.runtime && <Muted>Managed instances are not enabled on this Cairn (CAIRN_PG_RUNTIME).</Muted>}
            </Space>
            {list.instances.length === 0 ? <Empty description="No instances yet" /> : (
                <Table<PgInstance> size="small" rowKey="name" dataSource={list.instances} pagination={false}
                    onRow={(r) => ({ onClick: () => setSelected(r.name), style: { cursor: 'pointer' } })}
                    columns={[
                        { title: 'Instance', dataIndex: 'name', render: (v) => <a>{v}</a> },
                        { title: 'State', dataIndex: 'state', render: (v, r) => <Tag color={v === 'ready' ? 'green' : v === 'failed' ? 'red' : 'blue'}>
                            {v}{r.pending?.length ? ' · changes pending' : ''}</Tag> },
                        { title: 'Runtime', dataIndex: 'runtime' },
                        { title: 'Version', dataIndex: 'version', render: (v) => v ?? '—' },
                        { title: 'Size', render: (_, r) => (r.cpu ? `${r.cpu} CPU, ${bytes((r.memory_mb ?? 0) * 1024 * 1024)}, ${r.storage_gb} GB` : '—') },
                        { title: 'Standbys', dataIndex: 'replicas', render: (v) => v ?? '—' },
                        { title: 'Where', render: (_, r) => (r.host ? `${r.host}:${r.port}` : '—') },
                    ]} />
            )}
            <CreateInstance list={list} open={creating} onClose={() => setCreating(false)}
                onDone={() => { setCreating(false); void q.refresh(); }} />
            {inst && (
                <div style={{ marginTop: 16 }}>
                    <Typography.Title level={4}>{inst.name}</Typography.Title>
                    {inst.error && <Alert type="error" showIcon message={inst.error} style={{ marginBottom: 8 }} />}
                    <Tabs items={[
                        { key: 'status', label: 'Status', children: <Status name={inst.name} /> },
                        ...(inst.runtime === 'external' ? [] : [
                            { key: 'backups', label: 'Backups & restore', children: <Backups inst={inst} list={list} /> },
                            { key: 'parameters', label: 'Parameters', children: <Parameters inst={inst} list={list} onChange={() => void q.refresh()} /> },
                        ]),
                        { key: 'metrics', label: 'Metrics', children: <Metrics name={inst.name} external={inst.runtime === 'external'} /> },
                    ]} />
                </div>
            )}
        </>
    );
}
