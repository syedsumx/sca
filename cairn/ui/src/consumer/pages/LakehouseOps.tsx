import { Alert, Button, Checkbox, Form, Input, InputNumber, Modal, Select, Space, Table, Tag, Tooltip, message } from 'antd';
import React, { useState } from 'react';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import type {
    DerivedRun, DerivedTable, LakehouseShare, MaintenancePolicy, MaintenanceRun, Pipe, PipeFile, PipeReject, Stream, StreamReject,
    WarehouseState,
} from '../api/types';
import { Muted, Surface } from '../components/tokens';

const when = (ms: number | null | undefined) => (ms ? new Date(ms).toLocaleString() : '—');
const who = (urn: string) => urn.replace('urn:li:corpuser:', '');
const ok = (s: string | null | undefined) => (
    <Tag color={s === 'ok' || s === 'running' ? 'green' : s === 'error' ? 'red' : 'default'}>{s ?? 'not started'}</Tag>
);

/** Files landing in Cairn Store, loaded into lakehouse tables once each. */
export function PipesTab() {
    const { client } = useConsumerApi();
    const q = useDheQuery<Pipe[]>('/v1/pipes', (c, signal) => c.pipes({ signal }));
    return (
        <>
            <Muted>
                Each file under a pipe&apos;s prefix is loaded once, as soon as the store announces it (and by a sweep for
                anything an event missed). A file that does not fit is recorded and the next one still loads.
            </Muted>
            {q.error && <Alert type="error" message={q.error.message} showIcon style={{ marginTop: 12 }} />}
            <Surface style={{ marginTop: 12 }}>
                <Table<Pipe>
                    rowKey="id"
                    loading={q.loading}
                    dataSource={q.data ?? []}
                    pagination={false}
                    expandable={{ expandedRowRender: (p) => <><PipeFiles id={p.id} />{p.bad_rows === 'quarantine' && <PipeRejects id={p.id} />}</> }}
                    columns={[
                        { title: 'Pipe', dataIndex: 'name' },
                        { title: 'From', render: (_, p) => <code>{`s3://${p.bucket}/${p.prefix}`}</code> },
                        { title: 'Format', dataIndex: 'format' },
                        { title: 'Into', dataIndex: 'target', render: (t: string) => <code>{t}</code> },
                        { title: 'Bad rows', dataIndex: 'bad_rows', render: (b?: string) => (b === 'quarantine' ? 'set aside' : 'refuse the file') },
                        { title: 'State', dataIndex: 'enabled', render: (e: boolean) => <Tag color={e ? 'green' : 'default'}>{e ? 'on' : 'paused'}</Tag> },
                        {
                            title: '',
                            render: (_, p) => (
                                <Button size="small" onClick={async () => {
                                    const done = await client.sweepPipe(p.id);
                                    message.success(done.length ? `${done.length} new file(s) loaded` : 'Nothing new to load');
                                }}>
                                    Sweep now
                                </Button>
                            ),
                        },
                    ]}
                />
            </Surface>
        </>
    );
}

function PipeFiles({ id }: { id: string }) {
    const q = useDheQuery<PipeFile[]>(`/v1/pipes/${id}/files`, (c, signal) => c.pipeFiles(id, { signal }));
    return (
        <Table<PipeFile>
            size="small"
            rowKey={(f) => `${f.key}@${f.version}`}
            loading={q.loading}
            dataSource={q.data ?? []}
            pagination={{ pageSize: 10 }}
            columns={[
                { title: 'File', dataIndex: 'key', render: (k: string) => <code>{k}</code> },
                { title: 'Result', dataIndex: 'status', render: ok },
                { title: 'Rows', dataIndex: 'rows' },
                { title: 'Set aside', dataIndex: 'rejected', render: (n?: number | null) => n ?? '' },
                { title: 'Why', dataIndex: 'error', render: (e: string | null) => e ?? '' },
                { title: 'Loaded', dataIndex: 'loaded_ms', render: when },
            ]}
        />
    );
}

/** The rows a pipe set aside (bad_rows: quarantine): fix them and land them as a new file. */
function PipeRejects({ id }: { id: string }) {
    const q = useDheQuery<PipeReject[]>(`/v1/pipes/${id}/rejects`, (c, signal) => c.pipeRejects(id, { signal }));
    return (
        <Table<PipeReject>
            size="small"
            style={{ marginTop: 12 }}
            title={() => 'Rows set aside'}
            rowKey={(r) => `${r.key}@${r.version}#${r.row}`}
            loading={q.loading}
            dataSource={q.data ?? []}
            pagination={{ pageSize: 10 }}
            columns={[
                { title: 'File', dataIndex: 'key', render: (k: string) => <code>{k}</code> },
                { title: 'Row', dataIndex: 'row' },
                { title: 'Why', dataIndex: 'reason' },
                { title: 'Values', dataIndex: 'value', render: (v: string | null) => <code>{v ?? ''}</code> },
                { title: 'When', dataIndex: 'rejected_ms', render: when },
            ]}
        />
    );
}

/** Kafka topics (Debezium change events included) written into lakehouse tables, exactly once. */
export function StreamsTab() {
    const { client } = useConsumerApi();
    const q = useDheQuery<Stream[]>('/v1/streams', (c, signal) => c.streams({ signal }), { pollMs: 15_000 });
    const [creating, setCreating] = useState(false);
    return (
        <>
            <Space style={{ marginBottom: 8 }}>
                <Button type="primary" onClick={() => setCreating(true)}>New stream</Button>
            </Space>
            {creating && (
                <NewStream
                    onClose={() => setCreating(false)}
                    onCreate={async (body) => {
                        await client.createStream(body);
                        message.success(`${String(body.name)}: created; the stream workers start it within a minute`);
                        setCreating(false);
                        await q.refresh();
                    }}
                />
            )}
            <Muted>
                Each batch is one table commit that also records its Kafka offsets, so nothing is written twice. Messages
                that cannot be rows are set aside with their offset and the reason.
            </Muted>
            {q.error && <Alert type="error" message={q.error.message} showIcon style={{ marginTop: 12 }} />}
            <Surface style={{ marginTop: 12 }}>
                <Table<Stream>
                    rowKey="id"
                    loading={q.loading}
                    dataSource={q.data ?? []}
                    pagination={false}
                    expandable={{ expandedRowRender: (s) => <StreamRejects id={s.id} /> }}
                    columns={[
                        { title: 'Stream', dataIndex: 'name' },
                        { title: 'Topic', dataIndex: 'topic', render: (t: string) => <code>{t}</code> },
                        { title: 'As', render: (_, s) => `${s.format}, ${s.mode}${s.key.length ? ` on ${s.key.join(', ')}` : ''}` },
                        { title: 'Into', dataIndex: 'target', render: (t: string) => <code>{t}</code> },
                        {
                            title: 'State',
                            render: (_, s) => (s.enabled ? (
                                <Tooltip title={s.status.error ?? ''}>{ok(s.status.status)}</Tooltip>
                            ) : <Tag>paused</Tag>),
                        },
                        { title: 'Rows', render: (_, s) => (s.status.rows ?? 0).toLocaleString() },
                        { title: 'Lag', render: (_, s) => (s.status.lag == null ? '—' : s.status.lag.toLocaleString()) },
                        { title: 'Last commit', render: (_, s) => when(s.status.last_commit_ms) },
                        {
                            title: '',
                            render: (_, s) => (
                                <Button size="small" onClick={async () => {
                                    await client.setStreamEnabled(s.id, !s.enabled);
                                    await q.refresh();
                                }}>
                                    {s.enabled ? 'Pause' : 'Resume'}
                                </Button>
                            ),
                        },
                    ]}
                />
            </Surface>
        </>
    );
}

function StreamRejects({ id }: { id: string }) {
    const q = useDheQuery<StreamReject[]>(`/v1/streams/${id}/rejects`, (c, signal) => c.streamRejects(id, { signal }));
    if (!q.loading && !(q.data ?? []).length) return <Muted>No messages set aside.</Muted>;
    return (
        <Table<StreamReject>
            size="small"
            rowKey={(r) => `${r.partition}:${r.offset}`}
            loading={q.loading}
            dataSource={q.data ?? []}
            pagination={{ pageSize: 10 }}
            columns={[
                { title: 'Partition', dataIndex: 'partition' },
                { title: 'Offset', dataIndex: 'offset' },
                { title: 'Why', dataIndex: 'reason' },
                { title: 'Message', dataIndex: 'value', render: (v: string | null) => <code>{(v ?? '').slice(0, 120)}</code> },
                { title: 'When', dataIndex: 'rejected_ms', render: when },
            ]}
        />
    );
}

/** SELECTs kept as lakehouse tables, refreshed on Trino as their owner. */
export function DerivedTab() {
    const { client } = useConsumerApi();
    const q = useDheQuery<DerivedTable[]>('/v1/derived', (c, signal) => c.derivedTables({ signal }));
    const [busy, setBusy] = useState<string | null>(null);
    const refresh = async (d: DerivedTable, full: boolean) => {
        setBusy(d.id);
        try {
            const run = await client.refreshDerived(d.id, full);
            if (run.status === 'ok') message.success(`${d.name}: ${run.kind} done`);
            else message.error(`${d.name}: ${run.error}`);
            await q.refresh();
        } finally {
            setBusy(null);
        }
    };
    return (
        <>
            <Muted>
                Refreshed fully, by watermark or by merge, on a schedule or when a table they read changes (upstream ones
                first). Each refresh runs as its owner, so their masks and row filters apply.
            </Muted>
            {q.error && <Alert type="error" message={q.error.message} showIcon style={{ marginTop: 12 }} />}
            <Surface style={{ marginTop: 12 }}>
                <Table<DerivedTable>
                    rowKey="id"
                    loading={q.loading}
                    dataSource={q.data ?? []}
                    pagination={false}
                    expandable={{
                        expandedRowRender: (d) => (
                            <>
                                <pre style={{ whiteSpace: 'pre-wrap', margin: '0 0 8px' }}>{d.sql}</pre>
                                <DerivedRuns id={d.id} />
                            </>
                        ),
                    }}
                    columns={[
                        { title: 'Table', dataIndex: 'target', render: (t: string) => <code>{t}</code> },
                        { title: 'Name', dataIndex: 'name' },
                        {
                            title: 'Refresh',
                            render: (_, d) => [d.mode + (d.watermark ? ` on ${d.watermark}` : '') + (d.key.length ? ` by ${d.key.join(', ')}` : ''),
                                d.every_minutes ? `every ${d.every_minutes} min` : null, d.on_change ? 'when upstream changes' : null]
                                .filter(Boolean).join(' · '),
                        },
                        { title: 'Owner', dataIndex: 'owner', render: who },
                        { title: 'Last run', dataIndex: 'last_run_ms', render: when },
                        {
                            title: '',
                            render: (_, d) => (
                                <Space>
                                    <Button size="small" loading={busy === d.id} onClick={() => refresh(d, false)}>Refresh</Button>
                                    <Button size="small" disabled={busy === d.id} onClick={() => refresh(d, true)}>Rebuild</Button>
                                </Space>
                            ),
                        },
                    ]}
                />
            </Surface>
        </>
    );
}

function DerivedRuns({ id }: { id: string }) {
    const q = useDheQuery<DerivedRun[]>(`/v1/derived/${id}/runs`, (c, signal) => c.derivedRuns(id, { signal }));
    return (
        <Table<DerivedRun>
            size="small"
            rowKey="id"
            loading={q.loading}
            dataSource={q.data ?? []}
            pagination={{ pageSize: 10 }}
            columns={[
                { title: 'Kind', dataIndex: 'kind' },
                { title: 'Why', dataIndex: 'trigger' },
                { title: 'Result', dataIndex: 'status', render: ok },
                { title: 'Took', render: (_, r) => `${((r.finished_ms - r.started_ms) / 1000).toFixed(1)}s` },
                { title: 'Started', dataIndex: 'started_ms', render: when },
                { title: 'Error', dataIndex: 'error', render: (e: string | null) => e ?? '' },
            ]}
        />
    );
}

/** Compaction, snapshot expiry and orphan cleanup, by policy. */
export function MaintenanceTab() {
    const p = useDheQuery<MaintenancePolicy[]>('/maintenance/policies', (c, signal) => c.maintenancePolicies({ signal }));
    const r = useDheQuery<MaintenanceRun[]>('/maintenance/runs', (c, signal) => c.maintenanceRuns({ signal }));
    const days = (d: number | null) => (d == null ? 'off' : `${d} days`);
    return (
        <>
            <Muted>The most specific policy applies to each table: its own, its schema&apos;s, or <code>*</code>.</Muted>
            {(p.error || r.error) && <Alert type="error" message={(p.error ?? r.error)?.message} showIcon style={{ marginTop: 12 }} />}
            <Surface style={{ marginTop: 12 }}>
                <h3>Policies</h3>
                <Table<MaintenancePolicy>
                    rowKey="scope"
                    loading={p.loading}
                    dataSource={p.data ?? []}
                    pagination={false}
                    columns={[
                        { title: 'Scope', dataIndex: 'scope', render: (s: string) => <code>{s}</code> },
                        { title: 'Compaction', render: (_, x) => (x.optimize ? `files under ${x.optimize_threshold}` : 'off') },
                        { title: 'Expire snapshots', dataIndex: 'expire_days', render: days },
                        { title: 'Remove orphans', dataIndex: 'orphan_days', render: days },
                        { title: 'Every', dataIndex: 'every_hours', render: (h: number) => `${h} h` },
                        { title: 'By', dataIndex: 'updated_by', render: who },
                    ]}
                />
            </Surface>
            <Surface style={{ marginTop: 12 }}>
                <h3>Recent operations</h3>
                <Table<MaintenanceRun>
                    rowKey="id"
                    loading={r.loading}
                    dataSource={r.data ?? []}
                    pagination={{ pageSize: 15 }}
                    columns={[
                        { title: 'Table', dataIndex: 'table_name', render: (t: string) => <code>{t}</code> },
                        { title: 'Operation', dataIndex: 'operation' },
                        { title: 'Result', dataIndex: 'status', render: ok },
                        { title: 'When', dataIndex: 'started_ms', render: when },
                        { title: 'Detail', dataIndex: 'detail' },
                    ]}
                />
            </Surface>
        </>
    );
}

const WH_COLOR: Record<string, string> = { running: 'green', starting: 'blue', stopped: 'default', unreachable: 'red' };

function idleFor(seconds: number | undefined): string {
    if (seconds == null) return '—';
    if (seconds < 90) return `${seconds}s`;
    if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
    return `${(seconds / 3600).toFixed(1)} h`;
}

/** Each team's Trino: running or stopped (it stops when idle and starts on the next query). */
export function WarehousesTab() {
    const { client } = useConsumerApi();
    const q = useDheQuery<WarehouseState[]>('/v1/warehouses', (c, signal) => c.warehouses({ signal }), { pollMs: 15_000 });
    const start = async (name: string) => {
        try {
            await client.startWarehouse(name);
            message.success(`${name}: starting; it takes queries in a minute or two`);
            await q.refresh();
        } catch (e) {
            message.error(e instanceof Error ? e.message : String(e));
        }
    };
    return (
        <>
            <Muted>
                A warehouse stops after its idle time with no queries and starts again on the next one (that query waits
                for the start), or when you start it. Its workers follow the load. You see the warehouses you may use.
            </Muted>
            {q.error && <Alert type="error" message={q.error.message} showIcon style={{ marginTop: 12 }} />}
            <Surface style={{ marginTop: 12 }}>
                <Table<WarehouseState>
                    rowKey="name"
                    loading={q.loading}
                    dataSource={q.data ?? []}
                    pagination={false}
                    locale={{ emptyText: 'No warehouses you may use.' }}
                    columns={[
                        { title: 'Warehouse', dataIndex: 'name' },
                        { title: 'Port', dataIndex: 'port' },
                        {
                            title: 'State',
                            dataIndex: 'state',
                            render: (s: string, w) => (
                                <Tooltip title={w.error ?? ''}><Tag color={WH_COLOR[s]}>{s}</Tag></Tooltip>
                            ),
                        },
                        {
                            title: 'Queries',
                            render: (_, w) => (w.queries_running == null ? '—'
                                : `${w.queries_running} running${w.queries_queued ? `, ${w.queries_queued} queued` : ''}`),
                        },
                        {
                            title: 'Workers',
                            render: (_, w) => (w.workers ? `${w.workers.ready} of ${w.workers.wanted} (${w.workers.min}–${w.workers.max})` : '—'),
                        },
                        { title: 'Idle for', dataIndex: 'idle_seconds', render: idleFor },
                        {
                            title: 'Stops after',
                            dataIndex: 'idle_minutes',
                            render: (m?: number) => (m == null ? '—' : m === 0 ? 'never' : `${m} min idle`),
                        },
                        { title: 'Starts / stops', render: (_, w) => (w.starts == null ? '—' : `${w.starts} / ${w.stops}`) },
                        {
                            title: '',
                            render: (_, w) => w.state === 'stopped' && (
                                <Button size="small" onClick={() => start(w.name)}>Start now</Button>
                            ),
                        },
                    ]}
                />
            </Surface>
        </>
    );
}

const SHARE_COLOR: Record<string, string> = { active: 'green', expired: 'default', revoked: 'red' };
const day = (s: number | null | undefined) => (s ? new Date(s * 1000).toLocaleDateString() : '—');

/** Tables shared with recipients outside the organisation, read through Iceberg REST until revoked or expired. */
export function SharesTab() {
    const { client } = useConsumerApi();
    const q = useDheQuery<{ shares: LakehouseShare[] }>('/v1/shares', (c, signal) => c.shares({ signal }));
    const [creating, setCreating] = useState(false);
    const [made, setMade] = useState<LakehouseShare | null>(null);
    return (
        <>
            <Space style={{ marginBottom: 8 }}>
                <Button type="primary" onClick={() => setCreating(true)}>New share</Button>
            </Space>
            {creating && (
                <NewShare
                    onClose={() => setCreating(false)}
                    onCreate={async (body) => {
                        setMade(await client.createShare(body));
                        setCreating(false);
                        await q.refresh();
                    }}
                />
            )}
            {made && (
                <Modal open title={`Share ${made.name} created`} onOk={() => setMade(null)} onCancel={() => setMade(null)}
                    cancelButtonProps={{ style: { display: 'none' } }}>
                    <p>Send the recipient these connection settings. The credential is shown only now.</p>
                    <Input.TextArea readOnly autoSize value={JSON.stringify(made.connect ?? { credential: made.credential }, null, 2)}
                        aria-label="Connection settings" />
                </Modal>
            )}
            <Muted>
                A recipient reads the shared tables with any Iceberg REST client, read-only. Revoking a share stops it at
                once. Tables with a column mask or row filter are never shared.
            </Muted>
            {q.error && <Alert type="error" message={q.error.message} showIcon style={{ marginTop: 12 }} />}
            <Surface style={{ marginTop: 12 }}>
                <Table<LakehouseShare>
                    rowKey="id"
                    loading={q.loading}
                    dataSource={q.data?.shares ?? []}
                    pagination={false}
                    locale={{ emptyText: 'No shares yet.' }}
                    columns={[
                        { title: 'Share', dataIndex: 'name' },
                        { title: 'Recipient', dataIndex: 'recipient' },
                        { title: 'Tables', dataIndex: 'tables', render: (t: string[]) => t.join(', ') },
                        { title: 'State', dataIndex: 'state', render: (s: string) => <Tag color={SHARE_COLOR[s]}>{s}</Tag> },
                        { title: 'Expires', dataIndex: 'expires_at', render: day },
                        { title: 'Last used', dataIndex: 'last_used_at', render: (s: number | null) => (s ? new Date(s * 1000).toLocaleString() : 'never') },
                        { title: 'By', dataIndex: 'created_by', render: who },
                        {
                            title: '',
                            render: (_, sh) => sh.state === 'active' && (
                                <Button size="small" danger onClick={async () => {
                                    await client.revokeShare(sh.id);
                                    message.success(`${sh.name}: revoked; the recipient is refused from now on`);
                                    await q.refresh();
                                }}>Revoke</Button>
                            ),
                        },
                    ]}
                />
            </Surface>
        </>
    );
}

function NewShare({ onClose, onCreate }: {
    onClose: () => void;
    onCreate: (body: { name: string; recipient: string; tables: string[]; expires_days: number }) => Promise<void>;
}) {
    const [form] = Form.useForm();
    const [error, setError] = useState<string | null>(null);
    const [saving, setSaving] = useState(false);
    const submit = async () => {
        const v = await form.validateFields();
        setSaving(true);
        setError(null);
        try {
            await onCreate({
                name: v.name, recipient: v.recipient, expires_days: v.expires_days,
                tables: String(v.tables ?? '').split(/[\s,]+/).map((t: string) => t.trim()).filter(Boolean),
            });
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        } finally {
            setSaving(false);
        }
    };
    return (
        <Modal open title="New share" okText="Create" onOk={submit} onCancel={onClose} confirmLoading={saving}>
            {error && <Alert type="error" message={error} showIcon style={{ marginBottom: 12 }} />}
            <Form form={form} layout="vertical" initialValues={{ expires_days: 30 }}>
                <Form.Item name="name" label="Name" rules={[{ required: true }]}><Input placeholder="partner-a" /></Form.Item>
                <Form.Item name="recipient" label="Recipient" rules={[{ required: true }]}>
                    <Input placeholder="Partner A (data@partner.example)" />
                </Form.Item>
                <Form.Item name="tables" label="Tables" rules={[{ required: true }]} extra="namespace.table, separated by commas">
                    <Input placeholder="analytics.products" />
                </Form.Item>
                <Form.Item name="expires_days" label="Expires after (days)"><InputNumber min={1} max={365} /></Form.Item>
            </Form>
        </Modal>
    );
}

/** The form for a new stream (POST /v1/streams); Cairn checks the definition and says what is wrong. */
function NewStream({ onClose, onCreate }: { onClose: () => void; onCreate: (body: Record<string, unknown>) => Promise<void> }) {
    const [form] = Form.useForm();
    const [error, setError] = useState<string | null>(null);
    const [saving, setSaving] = useState(false);
    const format = Form.useWatch('format', form);
    const submit = async () => {
        const v = await form.validateFields();
        const body: Record<string, unknown> = {
            name: v.name, topic: v.topic, format: v.format, encoding: v.encoding, target: v.target,
            mode: v.format === 'debezium' ? 'upsert' : v.mode,
            key: String(v.key ?? '').split(',').map((k: string) => k.trim()).filter(Boolean),
            create_table: !!v.create_table, evolve: !!v.evolve, start: v.start, batch_seconds: v.batch_seconds,
            bad_rows: v.quarantine ? 'quarantine' : 'fail',
        };
        if (v.source) body.source = v.source;
        setSaving(true);
        setError(null);
        try {
            await onCreate(body);
        } catch (e) {
            setError((e as Error).message);
        } finally {
            setSaving(false);
        }
    };
    return (
        <Modal title="New stream" visible onCancel={onClose} onOk={submit} okText="Create" confirmLoading={saving} destroyOnClose>
            {error && <Alert type="error" message={error} showIcon style={{ marginBottom: 12 }} />}
            <Form form={form} layout="vertical" initialValues={{ format: 'json', encoding: 'json', mode: 'append', start: 'earliest',
                create_table: true, batch_seconds: 10 }}>
                <Form.Item name="name" label="Name" rules={[{ required: true }]}><Input aria-label="Name" /></Form.Item>
                <Form.Item name="topic" label="Kafka topic" rules={[{ required: true }]}><Input aria-label="Kafka topic" /></Form.Item>
                <Space wrap>
                    <Form.Item name="format" label="Format">
                        <Select aria-label="Format" style={{ width: 160 }} options={[{ value: 'json', label: 'JSON rows' },
                            { value: 'debezium', label: 'Debezium (CDC)' }]} />
                    </Form.Item>
                    <Form.Item name="encoding" label="Encoding">
                        <Select aria-label="Encoding" style={{ width: 200 }} options={[{ value: 'json', label: 'JSON' },
                            { value: 'avro', label: 'Avro (Schema Registry)' }]} />
                    </Form.Item>
                    {format !== 'debezium' && (
                        <Form.Item name="mode" label="Mode">
                            <Select aria-label="Mode" style={{ width: 140 }} options={[{ value: 'append', label: 'Append' },
                                { value: 'upsert', label: 'Upsert by key' }]} />
                        </Form.Item>
                    )}
                </Space>
                <Form.Item name="key" label="Key columns (comma-separated; needed for upsert and Debezium)">
                    <Input aria-label="Key columns" placeholder="id" />
                </Form.Item>
                <Form.Item name="target" label="Into table (namespace.table)" rules={[{ required: true }]}>
                    <Input aria-label="Into table" placeholder="cdc.customers" />
                </Form.Item>
                <Form.Item name="source" label="Source dataset URN (lineage, optional)"><Input aria-label="Source" /></Form.Item>
                <Space wrap>
                    <Form.Item name="start" label="Start from">
                        <Select aria-label="Start from" style={{ width: 140 }} options={[{ value: 'earliest', label: 'Earliest' },
                            { value: 'latest', label: 'Latest' }]} />
                    </Form.Item>
                    <Form.Item name="batch_seconds" label="Commit every (s)"><InputNumber min={1} max={3600} /></Form.Item>
                </Space>
                <Form.Item name="create_table" valuePropName="checked"><Checkbox>Create the table from the first messages</Checkbox></Form.Item>
                <Form.Item name="evolve" valuePropName="checked"><Checkbox>Add new columns instead of setting messages aside</Checkbox></Form.Item>
                <Form.Item name="quarantine" valuePropName="checked">
                    <Checkbox>Set aside rows the table refuses instead of stopping the stream</Checkbox>
                </Form.Item>
            </Form>
        </Modal>
    );
}
