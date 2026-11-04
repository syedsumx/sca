import { Alert, Button, Empty, Input, Modal, Select, Space, Table, Tag, message } from 'antd';
import React, { useEffect, useState } from 'react';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { DataResult, Metric, MetricQuery } from '../api/types';
import { ResultChart } from '../components/ResultChart';
import { Muted, Page, Surface } from '../components/tokens';

const GRAINS: NonNullable<MetricQuery['grain']>[] = ['day', 'week', 'month', 'quarter', 'year'];

type Draft = { name: string; label: string; description: string; connection?: string; table: string; measure: string;
    time_column: string; dimensions: string[]; filters: string };

const EMPTY: Draft = { name: '', label: '', description: '', table: '', measure: '', time_column: '', dimensions: [], filters: '' };

/** Metrics: measures defined once ("Revenue" is Sum([amount]) over paid orders), the same number wherever they are
 * used, and run as whoever looks, under their policies. */
export function MetricsPage() {
    const { client } = useConsumerApi();
    const [tick, setTick] = useState(0);
    const list = useDheQuery<Metric[]>(`/sql/metrics#${tick}`, (c, signal) => c.metrics({ signal }));
    const conns = useDheQuery('/data/sources', (c, signal) => c.dataConnections({ signal }));
    const [picked, setPicked] = useState<string[]>([]);
    const [dims, setDims] = useState<string[]>([]);
    const [grain, setGrain] = useState<MetricQuery['grain']>();
    const [result, setResult] = useState<DataResult | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [running, setRunning] = useState(false);
    const [editing, setEditing] = useState<{ draft: Draft; name?: string } | null>(null);

    useEffect(() => {
        if (editing && !editing.draft.connection && conns.data?.length)
            setEditing({ ...editing, draft: { ...editing.draft, connection: conns.data[0].connection } });
    }, [conns.data, editing]);

    const metrics = list.data ?? [];
    const chosen = metrics.filter((m) => picked.includes(m.name));
    // The dimensions every chosen metric allows (all its table's columns when it lists none: typed freely).
    const allowed = chosen.length ? chosen.map((m) => m.dimensions).reduce((a, b) => (a.length && b.length ? a.filter((x) => b.includes(x)) : a.length ? a : b)) : [];
    const canGrain = chosen.length > 0 && chosen.every((m) => m.time_column && m.time_column === chosen[0].time_column);

    const run = async () => {
        setRunning(true);
        setError(null);
        try {
            setResult(await client.queryMetrics({ metrics: picked, dimensions: dims, grain: canGrain ? grain : undefined }));
        } catch (e) {
            setError((e as Error).message);
            setResult(null);
        } finally {
            setRunning(false);
        }
    };

    const save = async () => {
        if (!editing) return;
        const d = editing.draft;
        let filters: unknown = [];
        try {
            filters = d.filters.trim() ? JSON.parse(d.filters) : [];
        } catch {
            message.error('Filters: a JSON list, such as [{"column": "status", "op": "=", "value": "paid"}]');
            return;
        }
        try {
            await client.saveMetric({ name: d.name, label: d.label || d.name, description: d.description, connection: d.connection,
                table: d.table, measure: d.measure, time_column: d.time_column || null, dimensions: d.dimensions,
                filters: filters as Metric['filters'] }, editing.name);
            message.success('Metric saved');
            setEditing(null);
            setTick((t) => t + 1);
        } catch (e) {
            message.error((e as Error).message);
        }
    };
    const act = async (fn: () => Promise<unknown>) => {
        try {
            await fn();
            setTick((t) => t + 1);
        } catch (e) {
            message.error((e as Error).message);
        }
    };
    const set = (patch: Partial<Draft>) => editing && setEditing({ ...editing, draft: { ...editing.draft, ...patch } });
    const x = grain && canGrain ? 'period' : dims[0];

    return (
        <Page>
            <h1 style={{ marginBottom: 0 }}>Metrics</h1>
            <Muted>Measures defined once and used in dashboards and workbooks. They run as you: your masks and row filters apply.</Muted>
            <Surface style={{ marginTop: 16 }}>
                <Space style={{ marginBottom: 12 }}>
                    <Button type="primary" onClick={() => setEditing({ draft: { ...EMPTY } })}>New metric</Button>
                </Space>
                <Table<Metric> size="small" rowKey="name" pagination={false} dataSource={metrics} loading={list.loading}
                    locale={{ emptyText: <Empty description="No metrics yet" /> }}
                    columns={[
                        { title: 'Metric', render: (_, m) => <span><strong>{m.label}</strong> <Muted>{m.name}</Muted>{m.certified && <Tag color="green" style={{ marginLeft: 6 }}>certified</Tag>}</span> },
                        { title: 'Definition', render: (_, m) => <code>{m.measure}{m.filters.length ? ` where ${m.filters.map((f) => `${f.column} ${f.op} ${JSON.stringify(f.value ?? f.values)}`).join(' and ')}` : ''}</code> },
                        { title: 'Table', render: (_, m) => m.table },
                        { title: 'Version', render: (_, m) => m.version },
                        {
                            title: '', render: (_, m) => (
                                <Space>
                                    <Button size="small" onClick={() => setEditing({ name: m.name, draft: { name: m.name, label: m.label, description: m.description,
                                        connection: m.connection, table: m.table, measure: m.measure, time_column: m.time_column ?? '',
                                        dimensions: m.dimensions, filters: m.filters.length ? JSON.stringify(m.filters) : '' } })}>Edit</Button>
                                    <Button size="small" onClick={() => void act(() => client.certifyMetric(m.name, !m.certified))}>
                                        {m.certified ? 'Uncertify' : 'Certify'}
                                    </Button>
                                    <Button size="small" danger onClick={() => void act(() => client.deleteMetric(m.name))}>Delete</Button>
                                </Space>
                            ),
                        },
                    ]} />
            </Surface>
            <Surface style={{ marginTop: 16 }}>
                <strong>Explore</strong>
                <Space wrap style={{ display: 'flex', marginTop: 8 }}>
                    <Select aria-label="Metrics to show" mode="multiple" placeholder="Metrics" style={{ minWidth: 260 }} value={picked}
                        onChange={setPicked} options={metrics.map((m) => ({ value: m.name, label: m.label }))} />
                    <Select aria-label="Break down by" mode={allowed.length ? 'multiple' : 'tags'} placeholder="Break down by" style={{ minWidth: 220 }}
                        value={dims} onChange={setDims} options={allowed.map((d) => ({ value: d, label: d }))} />
                    <Select aria-label="Time grain" allowClear placeholder="Over time" style={{ width: 140 }} value={grain} disabled={!canGrain}
                        onChange={setGrain} options={GRAINS.map((g) => ({ value: g, label: g }))} />
                    <Button type="primary" disabled={!picked.length} loading={running} onClick={() => void run()}>Run</Button>
                </Space>
                {error && <Alert type="error" showIcon message={error} style={{ marginTop: 12 }} />}
                {result && (
                    <div style={{ marginTop: 12 }}>
                        {x && <ResultChart columns={result.columns} rows={result.rows}
                            chart={{ type: grain && canGrain ? 'line' : 'bar', x, y: picked }} />}
                        <Table size="small" rowKey="__row" pagination={{ pageSize: 20, hideOnSinglePage: true }}
                            dataSource={result.rows.map((r, n) => ({ __row: n, ...Object.fromEntries(result.columns.map((c, j) => [c, r[j]])) }))}
                            columns={result.columns.map((c) => ({ title: c, dataIndex: c, render: (v: unknown) => String(v ?? '') }))} />
                    </div>
                )}
            </Surface>
            <Modal title={editing?.name ? `Edit ${editing.name}` : 'New metric'} open={!!editing} onOk={() => void save()} onCancel={() => setEditing(null)}
                okText="Save" width={640} okButtonProps={{ disabled: !editing?.draft.name || !editing?.draft.table || !editing?.draft.measure }}>
                {editing && (
                    <Space direction="vertical" style={{ width: '100%' }}>
                        <Input aria-label="Metric name" placeholder="name, such as revenue" value={editing.draft.name} disabled={!!editing.name}
                            onChange={(e) => set({ name: e.target.value })} />
                        <Input aria-label="Metric label" placeholder="Label, such as Revenue" value={editing.draft.label} onChange={(e) => set({ label: e.target.value })} />
                        <Select aria-label="Metric connection" value={editing.draft.connection} style={{ width: '100%' }}
                            options={(conns.data ?? []).map((c) => ({ value: c.connection, label: c.connection }))} onChange={(v) => set({ connection: v })} />
                        <Input aria-label="Metric table" placeholder="schema.table" value={editing.draft.table} onChange={(e) => set({ table: e.target.value })} />
                        <Input aria-label="Measure" placeholder="Sum([amount])" value={editing.draft.measure} style={{ fontFamily: 'ui-monospace, Menlo, monospace' }}
                            onChange={(e) => set({ measure: e.target.value })} />
                        <Input aria-label="Metric filters" placeholder='Filters (JSON): [{"column": "status", "op": "=", "value": "paid"}]' value={editing.draft.filters}
                            onChange={(e) => set({ filters: e.target.value })} />
                        <Input aria-label="Time column" placeholder="Time column, such as ordered_at" value={editing.draft.time_column}
                            onChange={(e) => set({ time_column: e.target.value })} />
                        <Select aria-label="Dimensions" mode="tags" placeholder="Dimensions it may be broken down by (empty: any column)" style={{ width: '100%' }}
                            value={editing.draft.dimensions} onChange={(v) => set({ dimensions: v })} />
                        <Input.TextArea aria-label="Metric description" placeholder="What it means" value={editing.draft.description} rows={2}
                            onChange={(e) => set({ description: e.target.value })} />
                        <Muted>Changing a certified metric withdraws its certification until an operator certifies it again.</Muted>
                    </Space>
                )}
            </Modal>
        </Page>
    );
}
