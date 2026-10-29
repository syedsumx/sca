import {
    Alert,
    Button,
    Card,
    Drawer,
    Form,
    Input,
    InputNumber,
    Modal,
    Popconfirm,
    Select,
    Space,
    Switch,
    Table,
    Tag,
    Typography,
    message,
} from 'antd';
import React, { useState } from 'react';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import type {
    IngestionSource,
    SourceConnector,
    SourceHealthState,
    SourceInput,
    SourceRun,
    SourceTest,
} from '../api/types';
import { GitRepositories } from './GitDocs';
import { Muted, SectionTitle } from './tokens';

const HEALTH_COLOR: Record<SourceHealthState, string> = {
    healthy: 'green',
    running: 'blue',
    failing: 'red',
    stale: 'orange',
    never_run: 'default',
};
const STATUS_COLOR: Record<string, string> = {
    SUCCESS: 'green',
    FAILURE: 'red',
    RUNNING: 'blue',
    CANCELLED: 'default',
};
const CATEGORY: Record<SourceConnector['category'], string> = {
    warehouse: 'Warehouses',
    database: 'Databases',
    lake: 'Lakes and storage',
    bi: 'BI tools',
    transformation: 'Transformation',
    streaming: 'Streaming',
    other: 'Other',
};
const when = (ms: number | null | undefined) => (ms ? new Date(ms).toLocaleString() : '—');
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function TestResult({ test }: { test: SourceTest }) {
    if (test.status === 'PENDING' || test.status === 'RUNNING')
        return <Alert type="info" showIcon message="Testing the connection…" />;
    if (test.connected)
        return (
            <Alert
                type="success"
                showIcon
                message="Connected"
                description={Object.entries(test.report?.capability_report ?? {})
                    .filter(([, c]) => !c.capable)
                    .map(([name, c]) => (
                        <div key={name}>
                            {name}: {c.failure_reason}
                        </div>
                    ))}
            />
        );
    return (
        <Alert
            type="error"
            showIcon
            message="Could not connect"
            description={
                test.report?.basic_connectivity?.failure_reason ?? test.error ?? 'The executor did not report a reason.'
            }
        />
    );
}

/** The setup form of one connector: its fields, where it runs, its schedule, and a connection test. */
function SourceForm({
    connector,
    source,
    onDone,
}: {
    connector: SourceConnector | null;
    source: IngestionSource | null;
    onDone: () => void;
}) {
    const { client } = useConsumerApi();
    const executors = useDheQuery('/sources/executors', (c, signal) => c.sourceExecutors({ signal }));
    const [form] = Form.useForm();
    const [test, setTest] = useState<SourceTest | null>(null);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const fields = connector?.fields ?? [];
    const initial = {
        name: source?.name ?? '',
        executor: source?.executor ?? 'default',
        scheduled: !!source?.schedule || !source,
        interval: source?.schedule?.interval ?? '0 2 * * *',
        timezone: source?.schedule?.timezone ?? 'UTC',
        owners: (source?.owners ?? []).join(', '),
        advanced: source && Object.keys(source.advanced).length ? JSON.stringify(source.advanced, null, 2) : '',
        recipe: connector ? '' : JSON.stringify(source?.recipe ?? { source: { type: '', config: {} } }, null, 2),
        ...Object.fromEntries(
            fields.map((f) => [
                `f_${f.key}`,
                source?.values[f.key] !== undefined
                    ? f.kind === 'list'
                        ? (source.values[f.key] as string[]).join(', ')
                        : source.values[f.key]
                    : f.kind === 'list'
                      ? ''
                      : f.default,
            ]),
        ),
    };

    const input = (): SourceInput => {
        const v = form.getFieldsValue(true);
        const parse = (text: string, what: string) => {
            if (!text?.trim()) return {};
            try {
                return JSON.parse(text);
            } catch {
                throw new Error(`${what} is not valid JSON`);
            }
        };
        return {
            name: v.name,
            connector: connector?.id ?? 'custom',
            values: Object.fromEntries(
                fields
                    .filter((f) => v[`f_${f.key}`] !== undefined && v[`f_${f.key}`] !== null && v[`f_${f.key}`] !== '')
                    .map((f) => [f.key, v[`f_${f.key}`]]),
            ),
            advanced: parse(v.advanced, 'Advanced options'),
            recipe: connector ? null : parse(v.recipe, 'The recipe'),
            schedule: v.scheduled ? { interval: v.interval, timezone: v.timezone || 'UTC' } : null,
            executor: v.executor,
            owners: String(v.owners || '')
                .split(',')
                .map((o: string) => o.trim())
                .filter(Boolean)
                .map((o: string) => (o.startsWith('urn:li:') ? o : `urn:li:corpuser:${o}`)),
        };
    };

    const runTest = async () => {
        setError(null);
        try {
            const body = input();
            let t = await client.testSource({
                connector: body.connector,
                values: body.values,
                advanced: body.advanced,
                recipe: body.recipe,
                executor: body.executor,
            });
            setTest(t);
            for (let i = 0; i < 60 && (t.status === 'PENDING' || t.status === 'RUNNING'); i += 1) {
                await sleep(i < 5 ? 1000 : 3000);
                t = await client.sourceTest(t.id);
                setTest(t);
            }
        } catch (e) {
            setError((e as Error & { body?: { detail?: string } }).body?.detail ?? (e as Error).message);
        }
    };

    const save = async () => {
        setError(null);
        setBusy(true);
        try {
            await form.validateFields();
            const body = input();
            if (source) await client.updateSource(source.urn, body);
            else await client.createSource(body);
            message.success(source ? 'Source saved' : 'Source added');
            onDone();
        } catch (e) {
            const detail = (e as Error & { body?: { detail?: string } }).body?.detail;
            if (detail || !(e as { errorFields?: unknown }).errorFields) setError(detail ?? (e as Error).message);
        } finally {
            setBusy(false);
        }
    };

    return (
        <Form form={form} layout="vertical" initialValues={initial} requiredMark="optional">
            {connector && <Muted>{connector.description}</Muted>}
            <Form.Item name="name" label="Name" rules={[{ required: true, message: 'Give the source a name' }]}>
                <Input aria-label="Source name" />
            </Form.Item>
            {fields.map((f) => (
                <Form.Item
                    key={f.key}
                    name={`f_${f.key}`}
                    label={f.label}
                    valuePropName={f.kind === 'bool' ? 'checked' : 'value'}
                    rules={f.required ? [{ required: true, message: `${f.label} is required` }] : []}
                    extra={
                        f.kind === 'secret'
                            ? 'The name of a secret, not its value: stored in Cairn, or in the agent pool’s secrets.'
                            : f.help || undefined
                    }
                >
                    {f.kind === 'bool' ? (
                        <Switch aria-label={f.label} />
                    ) : f.kind === 'number' ? (
                        <InputNumber aria-label={f.label} style={{ width: '100%' }} />
                    ) : f.kind === 'select' ? (
                        <Select aria-label={f.label} options={f.options.map((o) => ({ value: o, label: o }))} />
                    ) : (
                        <Input
                            aria-label={f.label}
                            placeholder={
                                f.kind === 'list' ? 'comma-separated' : f.kind === 'secret' ? 'SECRET_NAME' : ''
                            }
                        />
                    )}
                </Form.Item>
            ))}
            {!connector && (
                <Form.Item name="recipe" label="Recipe (JSON)" extra="Credentials as ${SECRET} references only.">
                    <Input.TextArea aria-label="Recipe" rows={10} style={{ fontFamily: 'monospace' }} />
                </Form.Item>
            )}
            <Form.Item name="executor" label="Runs on">
                <Select
                    aria-label="Runs on"
                    options={(
                        executors.data ?? [{ id: 'default', name: 'Cairn (central executor)', online: null }]
                    ).map((e) => ({
                        value: e.id,
                        label: e.online === null ? e.name : `${e.name} (${e.online} online)`,
                    }))}
                />
            </Form.Item>
            <Space align="start" wrap>
                <Form.Item name="scheduled" label="On a schedule" valuePropName="checked">
                    <Switch aria-label="On a schedule" />
                </Form.Item>
                <Form.Item name="interval" label="Schedule (cron)" extra="0 2 * * * is every day at 02:00">
                    <Input aria-label="Schedule" style={{ width: 160 }} />
                </Form.Item>
                <Form.Item name="timezone" label="Time zone">
                    <Input aria-label="Time zone" style={{ width: 180 }} />
                </Form.Item>
            </Space>
            <Form.Item
                name="owners"
                label="Tell about failures"
                extra="People (user names), comma-separated. Default: you."
            >
                <Input aria-label="Owners" />
            </Form.Item>
            {connector && (
                <Form.Item name="advanced" label="Advanced options (JSON)" extra="Merged into the source's config.">
                    <Input.TextArea aria-label="Advanced options" rows={3} style={{ fontFamily: 'monospace' }} />
                </Form.Item>
            )}
            {test && <TestResult test={test} />}
            {error && <Alert type="error" showIcon message={error} style={{ marginTop: 8 }} />}
            <Space style={{ marginTop: 12 }}>
                <Button onClick={runTest}>Test connection</Button>
                <Button type="primary" loading={busy} onClick={save}>
                    {source ? 'Save' : 'Save source'}
                </Button>
            </Space>
        </Form>
    );
}

/** A source's recent runs, and one run's log. */
function History({ source, onClose }: { source: IngestionSource; onClose: () => void }) {
    const { client } = useConsumerApi();
    const detail = useDheQuery(`/sources/${source.urn}/detail`, (c, signal) => c.source(source.urn, { signal }));
    const [log, setLog] = useState<(SourceRun & { log: string }) | null>(null);
    return (
        <Drawer open width={760} title={`Runs of ${source.name}`} onClose={onClose}>
            <Table<SourceRun>
                size="small"
                rowKey="urn"
                loading={detail.loading}
                dataSource={detail.data?.runs ?? []}
                pagination={{ pageSize: 10, hideOnSinglePage: true }}
                locale={{ emptyText: 'No runs yet.' }}
                columns={[
                    { title: 'Started', render: (_, r) => when(r.started_ms ?? r.requested_ms) },
                    { title: 'Status', render: (_, r) => <Tag color={STATUS_COLOR[r.status]}>{r.status}</Tag> },
                    {
                        title: 'Time',
                        render: (_, r) => (r.duration_ms ? `${Math.round(r.duration_ms / 1000)} s` : '—'),
                    },
                    { title: 'Changes', dataIndex: 'events' },
                    { title: 'Failures', dataIndex: 'failures' },
                    {
                        title: '',
                        render: (_, r) => (
                            <Button size="small" onClick={async () => setLog(await client.sourceRun(r.urn))}>
                                Log
                            </Button>
                        ),
                    },
                ]}
            />
            {log && (
                <>
                    <SectionTitle as="h3" style={{ fontSize: 15, marginTop: 16 }}>
                        Log of the run started {when(log.started_ms ?? log.requested_ms)}
                    </SectionTitle>
                    <pre
                        aria-label="Run log"
                        style={{ maxHeight: 360, overflow: 'auto', fontSize: 12, background: '#f6f7f9', padding: 12 }}
                    >
                        {log.log || 'No log was recorded.'}
                    </pre>
                </>
            )}
        </Drawer>
    );
}

/** Settings → Sources: the connector gallery, every source with its health, runs and logs. */
export function SourcesSettings() {
    const { client } = useConsumerApi();
    const sources = useDheQuery('/sources', (c, signal) => c.sources({ signal }), { pollMs: 30_000 });
    const connectors = useDheQuery('/sources/connectors', (c, signal) => c.sourceConnectors({ signal }));
    const [gallery, setGallery] = useState(false);
    const [editing, setEditing] = useState<{
        connector: SourceConnector | null;
        source: IngestionSource | null;
    } | null>(null);
    const [history, setHistory] = useState<IngestionSource | null>(null);
    const byId = Object.fromEntries((connectors.data ?? []).map((c) => [c.id, c]));
    const bad = (sources.data ?? []).filter((s) => s.health.state === 'failing' || s.health.state === 'stale');

    return (
        <section>
            <Space style={{ justifyContent: 'space-between', width: '100%' }}>
                <Muted>Where the catalog's metadata comes from, when it runs, and whether it is up to date.</Muted>
                <Button type="primary" onClick={() => setGallery(true)}>
                    Add source
                </Button>
            </Space>
            {bad.length > 0 && (
                <Alert
                    type="warning"
                    showIcon
                    style={{ marginTop: 12 }}
                    message={`${bad.length} source${bad.length > 1 ? 's need' : ' needs'} attention`}
                    description={bad.map((s) => (
                        <div key={s.urn}>
                            {s.name}: {s.health.reason}
                        </div>
                    ))}
                />
            )}
            <Table<IngestionSource>
                style={{ marginTop: 12 }}
                rowKey="urn"
                loading={sources.loading}
                dataSource={sources.data ?? []}
                pagination={false}
                locale={{ emptyText: 'No sources yet: add one from the gallery.' }}
                columns={[
                    {
                        title: 'Source',
                        render: (_, s) => (
                            <span>
                                <Typography.Text strong>{s.name}</Typography.Text>
                                <br />
                                <Muted>{byId[s.connector]?.name ?? s.type}</Muted>
                            </span>
                        ),
                    },
                    {
                        title: 'Health',
                        render: (_, s) => (
                            <span title={s.health.reason}>
                                <Tag color={HEALTH_COLOR[s.health.state]}>{s.health.state.replace('_', ' ')}</Tag>
                            </span>
                        ),
                    },
                    { title: 'Runs on', render: (_, s) => (s.executor === 'default' ? 'Cairn' : `pool ${s.executor}`) },
                    {
                        title: 'Schedule',
                        render: (_, s) =>
                            s.schedule ? (
                                <code>
                                    {s.schedule.interval} {s.schedule.timezone !== 'UTC' && s.schedule.timezone}
                                </code>
                            ) : (
                                <Muted>manual</Muted>
                            ),
                    },
                    {
                        title: 'Last run',
                        render: (_, s) =>
                            s.last_run ? (
                                <span>
                                    <Tag color={STATUS_COLOR[s.last_run.status]}>{s.last_run.status}</Tag>
                                    {when(s.last_run.started_ms ?? s.last_run.requested_ms)}
                                </span>
                            ) : (
                                <Muted>never</Muted>
                            ),
                    },
                    {
                        title: '',
                        render: (_, s) => (
                            <Space>
                                <Button
                                    size="small"
                                    aria-label={`Run ${s.name} now`}
                                    onClick={async () => {
                                        await client.runSource(s.urn);
                                        message.success(`${s.name} started`);
                                        sources.refresh();
                                    }}
                                >
                                    Run now
                                </Button>
                                <Button size="small" onClick={() => setHistory(s)}>
                                    Runs
                                </Button>
                                <Button
                                    size="small"
                                    onClick={() => setEditing({ connector: byId[s.connector] ?? null, source: s })}
                                >
                                    Edit
                                </Button>
                                <Popconfirm
                                    title={`Delete ${s.name}? The metadata it brought in stays.`}
                                    onConfirm={async () => {
                                        await client.deleteSource(s.urn);
                                        sources.refresh();
                                    }}
                                >
                                    <Button size="small" danger>
                                        Delete
                                    </Button>
                                </Popconfirm>
                            </Space>
                        ),
                    },
                ]}
            />
            <Modal open={gallery} title="Add a source" footer={null} width={880} onCancel={() => setGallery(false)}>
                {(Object.keys(CATEGORY) as SourceConnector['category'][]).map((cat) => {
                    const items = (connectors.data ?? []).filter((c) => c.category === cat);
                    if (!items.length) return null;
                    return (
                        <div key={cat} style={{ marginBottom: 12 }}>
                            <SectionTitle as="h3" style={{ fontSize: 14 }}>
                                {CATEGORY[cat]}
                            </SectionTitle>
                            <Space wrap>
                                {items.map((c) => (
                                    <Card
                                        key={c.id}
                                        size="small"
                                        hoverable
                                        style={{ width: 200 }}
                                        onClick={() => {
                                            setGallery(false);
                                            setEditing({ connector: c, source: null });
                                        }}
                                    >
                                        <Typography.Text strong>{c.name}</Typography.Text>
                                        <br />
                                        <Muted style={{ fontSize: 12 }}>{c.description}</Muted>
                                    </Card>
                                ))}
                            </Space>
                        </div>
                    );
                })}
                <Button
                    type="link"
                    onClick={() => {
                        setGallery(false);
                        setEditing({ connector: null, source: null });
                    }}
                >
                    Another connector: write the recipe
                </Button>
            </Modal>
            {editing && (
                <Drawer
                    open
                    width={560}
                    title={
                        editing.source
                            ? `Edit ${editing.source.name}`
                            : `New ${editing.connector?.name ?? 'custom'} source`
                    }
                    onClose={() => setEditing(null)}
                >
                    <SourceForm
                        connector={editing.connector}
                        source={editing.source}
                        onDone={() => {
                            setEditing(null);
                            sources.refresh();
                        }}
                    />
                </Drawer>
            )}
            {history && <History source={history} onClose={() => setHistory(null)} />}
            <div style={{ marginTop: 16 }}>
                <GitRepositories />
            </div>
        </section>
    );
}
