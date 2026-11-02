import { Alert, Button, Checkbox, Empty, Input, InputNumber, Modal, Select, Space, Table, Tabs, Tag, message } from 'antd';
import React, { useState } from 'react';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { Dashboard, RenderedDashboard, SavedQuery, SqlAlert } from '../api/types';
import { ResultChart } from '../components/ResultChart';
import { Muted, Page, Surface } from '../components/tokens';

const STATE: Record<SqlAlert['state'], string> = { ok: 'green', triggered: 'red', error: 'orange', unknown: 'default' };

/** One dashboard, every tile run as the viewer. */
function Board({ id }: { id: string }) {
    const q = useDheQuery<RenderedDashboard>(`/sql/dashboards/${id}/render`, (c, signal) => c.renderDashboard(id, { signal }));
    if (q.error) return <Alert type="error" message={q.error.message} />;
    if (!q.data) return <Muted>Loading…</Muted>;
    return (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: 12 }}>
            {q.data.tiles.map((t, i) => (
                <Surface key={i} style={{ gridColumn: t.width === 2 ? 'span 2' : undefined }}>
                    <strong>{t.title}</strong>
                    {t.error ? (
                        <Alert type="warning" message={t.error} style={{ marginTop: 8 }} />
                    ) : t.result && t.chart && t.chart.type !== 'table' ? (
                        <ResultChart columns={t.result.columns} rows={t.result.rows} chart={t.chart} height={220} />
                    ) : t.result ? (
                        <Table
                            size="small"
                            rowKey="__row"
                            pagination={{ pageSize: 10, hideOnSinglePage: true }}
                            dataSource={t.result.rows.map((r, n) => ({ __row: n, ...Object.fromEntries(t.result!.columns.map((c, j) => [c, r[j]])) }))}
                            columns={t.result.columns.map((c) => ({ title: c, dataIndex: c, render: (v: unknown) => String(v ?? '') }))}
                        />
                    ) : null}
                </Surface>
            ))}
        </div>
    );
}

function Dashboards({ saved }: { saved: SavedQuery[] }) {
    const { client } = useConsumerApi();
    const [tick, setTick] = useState(0);
    const list = useDheQuery<Dashboard[]>(`/sql/dashboards#${tick}`, (c, signal) => c.dashboards({ signal }));
    const [open, setOpen] = useState<string | null>(null);
    const [editing, setEditing] = useState(false);
    const [form, setForm] = useState({ name: '', shared: false, queries: [] as string[] });

    const create = async () => {
        try {
            const d = await client.saveDashboard({ name: form.name, shared: form.shared,
                tiles: form.queries.map((q) => ({ query: q, title: '', width: 1 as const })) });
            setEditing(false);
            setTick((t) => t + 1);
            setOpen(d.id);
        } catch (e) {
            message.error((e as Error).message);
        }
    };

    return (
        <>
            <Space style={{ marginBottom: 12 }} wrap>
                <Select aria-label="Dashboard" placeholder="Open a dashboard" style={{ minWidth: 260 }} value={open ?? undefined}
                    onChange={setOpen} options={(list.data ?? []).map((d) => ({ value: d.id, label: d.shared ? `${d.name} (shared)` : d.name }))} />
                <Button onClick={() => setEditing(true)} disabled={!saved.length}>New dashboard</Button>
                {!saved.length && <Muted>Save queries in SQL first; a dashboard shows them.</Muted>}
            </Space>
            {open ? <Board key={`${open}-${tick}`} id={open} /> : <Empty description="Pick or create a dashboard" />}
            <Modal title="New dashboard" open={editing} onOk={() => void create()} onCancel={() => setEditing(false)}
                okText="Create" okButtonProps={{ disabled: !form.name.trim() || !form.queries.length }}>
                <Space direction="vertical" style={{ width: '100%' }}>
                    <Input aria-label="Dashboard name" placeholder="Name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
                    <Select aria-label="Queries" mode="multiple" placeholder="Saved queries to show" style={{ width: '100%' }}
                        value={form.queries} onChange={(queries) => setForm({ ...form, queries })}
                        options={saved.map((q) => ({ value: q.id, label: q.name }))} />
                    <Checkbox checked={form.shared} onChange={(e) => setForm({ ...form, shared: e.target.checked })}>
                        Share (each viewer sees each tile as themselves)
                    </Checkbox>
                </Space>
            </Modal>
        </>
    );
}

function Alerts({ saved }: { saved: SavedQuery[] }) {
    const { client } = useConsumerApi();
    const [tick, setTick] = useState(0);
    const list = useDheQuery<SqlAlert[]>(`/sql/alerts#${tick}`, (c, signal) => c.sqlAlerts({ signal }));
    const [editing, setEditing] = useState(false);
    const [form, setForm] = useState({ query: '', measure: 'rows', op: '>', value: 0, every_minutes: 60 });
    const names = Object.fromEntries(saved.map((q) => [q.id, q.name]));

    const act = async (fn: () => Promise<unknown>) => {
        try {
            await fn();
            setTick((t) => t + 1);
        } catch (e) {
            message.error((e as Error).message);
        }
    };

    return (
        <>
            <Button style={{ marginBottom: 12 }} onClick={() => setEditing(true)} disabled={!saved.length}>New alert</Button>
            <Table<SqlAlert>
                size="small"
                rowKey="id"
                dataSource={list.data ?? []}
                pagination={false}
                columns={[
                    { title: 'Alert', render: (_, a) => a.name },
                    { title: 'Query', render: (_, a) => names[a.query] ?? a.query },
                    { title: 'When', render: (_, a) => `${a.measure} ${a.op} ${a.value}, every ${a.every_minutes} min` },
                    {
                        title: 'State',
                        render: (_, a) => (
                            <Tag color={STATE[a.state]} title={a.last_error ?? ''}>
                                {a.state}
                                {a.last_value !== null ? ` (${a.last_value})` : ''}
                            </Tag>
                        ),
                    },
                    {
                        title: '',
                        render: (_, a) => (
                            <Space>
                                <Button size="small" onClick={() => void act(() => client.checkSqlAlert(a.id))}>Check now</Button>
                                <Button size="small" danger onClick={() => void act(() => client.deleteSqlAlert(a.id))}>Delete</Button>
                            </Space>
                        ),
                    },
                ]}
            />
            <Modal title="New alert" open={editing} okText="Create" okButtonProps={{ disabled: !form.query }}
                onCancel={() => setEditing(false)}
                onOk={() => void act(async () => { await client.saveSqlAlert(form); setEditing(false); })}>
                <Space direction="vertical" style={{ width: '100%' }}>
                    <Select aria-label="Alert query" placeholder="Saved query" style={{ width: '100%' }} value={form.query || undefined}
                        onChange={(query) => setForm({ ...form, query })} options={saved.map((q) => ({ value: q.id, label: q.name }))} />
                    <Space wrap>
                        <Input aria-label="Measure" style={{ width: 140 }} value={form.measure}
                            onChange={(e) => setForm({ ...form, measure: e.target.value })} placeholder="rows or a column" />
                        <Select aria-label="Operator" style={{ width: 80 }} value={form.op} onChange={(op) => setForm({ ...form, op })}
                            options={['>', '>=', '<', '<=', '==', '!='].map((o) => ({ value: o, label: o }))} />
                        <InputNumber aria-label="Value" value={form.value} onChange={(v) => setForm({ ...form, value: Number(v) || 0 })} />
                    </Space>
                    <span>
                        Every <InputNumber aria-label="Minutes" min={5} max={10080} value={form.every_minutes}
                            onChange={(v) => setForm({ ...form, every_minutes: Number(v) || 60 })} /> minutes
                    </span>
                    <Muted>It runs as you; you are told when the condition starts to hold and when it stops.</Muted>
                </Space>
            </Modal>
        </>
    );
}

/** Dashboards of saved queries, and alerts on them. */
export function DashboardsPage() {
    const saved = useDheQuery<SavedQuery[]>('/sql/saved', (c, signal) => c.savedQueries({ signal }));
    return (
        <Page>
            <h1 style={{ marginBottom: 0 }}>Dashboards</h1>
            <Muted>Saved queries with their charts, and alerts on them. Every tile and check runs as a person, under their policies.</Muted>
            <Tabs style={{ marginTop: 12 }} items={[
                { key: 'boards', label: 'Dashboards', children: <Dashboards saved={saved.data ?? []} /> },
                { key: 'alerts', label: 'Alerts', children: <Alerts saved={saved.data ?? []} /> },
            ]} />
        </Page>
    );
}
