import { Alert, Button, Checkbox, Collapse, Empty, Input, InputNumber, Modal, Select, Space, Table, Tabs, Tag, message } from 'antd';
import React, { useCallback, useEffect, useMemo, useState } from 'react';

import { BiAssetButtons } from '../components/BiAdmin';
import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { ChartSpec, DataResult, SavedQuery, SchemaTable, SqlRun, SystemTablesInfo } from '../api/types';
import { ResultChart, toCsv } from '../components/ResultChart';
import { CHART_TYPES } from '../api/types';
import { SqlEditor } from '../components/SqlEditor';
import { Muted, Page, Surface } from '../components/tokens';
import styled from 'styled-components';
import { TitleRow } from '../guide/PageHeader';
import { describeError, notifyError } from '../components/ErrorNotice';
import { When } from '../utils/time';
import { StatusBadge } from '../components/StatusBadge';

/** The schema browser beside the editor; on a narrow screen it stacks above it, and the editor keeps to the viewport. */
const SqlGrid = styled.div`
    display: grid;
    grid-template-columns: minmax(220px, 1fr) 3fr;
    gap: 16px;
    margin-top: 16px;
    > * {
        min-width: 0;
    }
    .sql-main textarea {
        max-width: 100%;
    }
    @media (max-width: 900px) {
        grid-template-columns: minmax(0, 1fr);
        .sql-schema {
            max-height: 260px;
            overflow: auto;
        }
    }
`;

const ago = (ms: number) => <When value={ms} />;

/** The SQL workspace: browse what you may query, run read-only SQL as yourself, chart and download the
 * result, keep a history, and save queries for yourself or everyone. */
export function SqlWorkspacePage() {
    const { client } = useConsumerApi();
    const conns = useDheQuery('/data/sources', (c, signal) => c.dataConnections({ signal }));
    const [connection, setConnection] = useState<string | undefined>();
    const [sql, setSql] = useState('');
    const [limit, setLimit] = useState(200);
    const [result, setResult] = useState<DataResult | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [running, setRunning] = useState(false);
    const [chart, setChart] = useState<ChartSpec>({ type: 'table' });
    const [current, setCurrent] = useState<SavedQuery | null>(null);
    const [saving, setSaving] = useState(false);
    const [form, setForm] = useState({ name: '', description: '', shared: false });
    const [tick, setTick] = useState(0);

    useEffect(() => {
        if (!connection && conns.data?.length) setConnection(conns.data[0].connection);
    }, [conns.data, connection]);

    const schema = useDheQuery<SchemaTable[]>(`/sql/schema?${connection ?? ''}`, (c, signal) =>
        connection ? c.sqlSchema(connection, { signal }) : Promise.resolve([]),
    );
    // Cairn's system tables (cairn_system.*): listed when they are set up; read like any table.
    const system = useDheQuery<SystemTablesInfo | null>('/v1/system-tables', (c, signal) =>
        c.systemTables({ signal }).catch(() => null),
    );
    const history = useDheQuery<SqlRun[]>(`/sql/history#${tick}`, (c, signal) => c.sqlHistory({ signal }));
    const saved = useDheQuery<SavedQuery[]>(`/sql/saved#${tick}`, (c, signal) => c.savedQueries({ signal }));

    const run = useCallback(async () => {
        if (!connection || !sql.trim()) return;
        setRunning(true);
        setError(null);
        try {
            setResult(await client.runSql(sql, connection, limit));
        } catch (e) {
            setResult(null);
            setError(describeError(e));
        } finally {
            setRunning(false);
            setTick((t) => t + 1);
        }
    }, [client, connection, sql, limit]);

    const download = () => {
        if (!result) return;
        const blob = new Blob([toCsv(result.columns, result.rows)], { type: 'text/csv' });
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = `${current?.name || 'query'}.csv`;
        a.click();
        URL.revokeObjectURL(a.href);
    };

    const save = async () => {
        if (!connection) return;
        try {
            const q = await client.saveQuery({ ...form, sql, connection, chart }, current?.id);
            setCurrent(q);
            setSaving(false);
            setTick((t) => t + 1);
            message.success(form.shared ? 'Saved and shared' : 'Saved');
        } catch (e) {
            notifyError(e);
        }
    };

    const open = (q: { sql: string; connection: string }, s?: SavedQuery) => {
        setSql(q.sql);
        setConnection(q.connection);
        setCurrent(s ?? null);
        if (s) {
            setChart(s.chart?.type ? s.chart : { type: 'table' });
            setForm({ name: s.name, description: s.description, shared: s.shared });
        }
    };

    const numeric = useMemo(
        () => (result ? result.columns.filter((_, i) => result.rows.some((r) => typeof r[i] === 'number')) : []),
        [result],
    );

    return (
        <Page>
            <TitleRow title="SQL" />
            <Muted>Read-only SQL as you: every table is checked and masked for you, as on its page.</Muted>
            <SqlGrid>
                <Surface className="sql-schema">
                    <Select
                        style={{ width: '100%', marginBottom: 8 }}
                        aria-label="Connection"
                        value={connection}
                        onChange={setConnection}
                        options={(conns.data ?? []).map((c) => ({ value: c.connection, label: `${c.connection} (${c.platform})` }))}
                    />
                    {schema.data && !schema.data.length && <Muted>No tables you may query here.</Muted>}
                    <Collapse>
                        {(schema.data ?? []).map((t) => (
                            <Collapse.Panel
                                key={t.urn}
                                header={
                                    <span
                                        onDoubleClick={() => setSql((s) => `${s}${s && !s.endsWith(' ') ? ' ' : ''}${t.name}`)}
                                        title="Double-click to insert">
                                        {t.name}
                                    </span>
                                }>
                                <ul style={{ margin: 0, paddingLeft: 16 }}>
                                    {t.columns.map((c) => (
                                        <li key={c.name}>
                                            {c.name} <Muted>{c.type}</Muted>
                                        </li>
                                    ))}
                                </ul>
                            </Collapse.Panel>
                        ))}
                    </Collapse>
                    {system.data && system.data.tables.length > 0 && (
                        <div style={{ marginTop: 12 }} data-testid="system-tables">
                            <strong>System tables</strong>
                            <Muted style={{ display: 'block', fontSize: 12 }}>
                                Cairn&apos;s own data, fresh within {Math.round(system.data.freshness_seconds / 60)} min.
                                Run on a Trino connection.
                            </Muted>
                            <Collapse>
                                {system.data.tables.map((t) => (
                                    <Collapse.Panel
                                        key={t.name}
                                        header={
                                            <span
                                                onDoubleClick={() => setSql((s) => `${s}${s && !s.endsWith(' ') ? ' ' : ''}${t.name}`)}
                                                title={`${t.description} Double-click to insert.`}>
                                                {t.name}
                                            </span>
                                        }>
                                        <Muted style={{ display: 'block', marginBottom: 4 }}>
                                            {t.description}
                                            {t.row_filtered ? ' You see your own rows unless you are an operator.' : ''}
                                        </Muted>
                                        <ul style={{ margin: 0, paddingLeft: 16 }}>
                                            {t.columns.map((c) => (
                                                <li key={c.name} title={c.description}>
                                                    {c.name} <Muted>{c.type}</Muted>
                                                    {t.masked_columns.includes(c.name) && <Tag style={{ marginLeft: 4 }}>masked</Tag>}
                                                </li>
                                            ))}
                                        </ul>
                                    </Collapse.Panel>
                                ))}
                            </Collapse>
                        </div>
                    )}
                </Surface>
                <div className="sql-main">
                    <Surface>
                        {current && (
                            <div style={{ marginBottom: 8 }}>
                                <strong>{current.name}</strong> {current.shared && <Tag color="blue">shared</Tag>}
                            </div>
                        )}
                        <SqlEditor
                            value={sql}
                            onChange={setSql}
                            tables={schema.data ?? []}
                            onRun={() => void run()}
                            placeholder="SELECT region, count(*) AS orders FROM sales.orders GROUP BY region"
                        />
                        <Space style={{ marginTop: 8 }} wrap>
                            <Button type="primary" onClick={() => void run()} loading={running} disabled={!connection || !sql.trim()}>
                                Run
                            </Button>
                            <span>
                                Rows <InputNumber aria-label="Rows" min={1} max={1000} value={limit} onChange={(v) => setLimit(Number(v) || 200)} />
                            </span>
                            <Button onClick={() => setSaving(true)} disabled={!sql.trim()}>
                                {current ? 'Save changes' : 'Save'}
                            </Button>
                            <Button onClick={download} disabled={!result}>
                                Download CSV
                            </Button>
                            <Muted>Ctrl+Enter runs · Ctrl+Space suggests</Muted>
                        </Space>
                    </Surface>
                    {error && <Alert style={{ marginTop: 12 }} type="error" message={error} showIcon />}
                    {result && (
                        <Surface style={{ marginTop: 12 }}>
                            <Muted>
                                {result.rows.length} row(s){result.truncated ? ' (more not shown)' : ''} in {result.elapsed_ms} ms
                                {result.masked_columns.length ? ` · masked for you: ${result.masked_columns.join(', ')}` : ''}
                            </Muted>
                            <Tabs
                                items={[
                                    {
                                        key: 'table',
                                        label: 'Table',
                                        children: (
                                            <Table
                                                size="small"
                                                rowKey="__row"
                                                scroll={{ x: true }}
                                                pagination={{ pageSize: 50, hideOnSinglePage: true }}
                                                dataSource={result.rows.map((r, n) => ({ __row: n, ...Object.fromEntries(result.columns.map((c, i) => [c, r[i]])) }))}
                                                columns={result.columns.map((c) => ({
                                                    title: c,
                                                    dataIndex: c,
                                                    render: (v: unknown) => (v === null ? <Muted>null</Muted> : String(v)),
                                                }))}
                                            />
                                        ),
                                    },
                                    {
                                        key: 'chart',
                                        label: 'Chart',
                                        children: (
                                            <>
                                                <Space wrap style={{ marginBottom: 8 }}>
                                                    <Select aria-label="Chart type" value={chart.type} style={{ width: 100 }}
                                                        onChange={(type) => setChart({ ...chart, type })}
                                                        options={CHART_TYPES.map((v) => ({ value: v, label: v.replace('_', ' ') }))} />
                                                    <Select aria-label="x" placeholder="x" value={chart.x} style={{ width: 160 }}
                                                        onChange={(x) => setChart({ ...chart, x })}
                                                        options={result.columns.map((c) => ({ value: c, label: c }))} />
                                                    <Select aria-label="y" placeholder={chart.type === 'map' ? 'size by' : 'y'} mode="multiple"
                                                        value={chart.y} style={{ minWidth: 200 }} onChange={(y) => setChart({ ...chart, y })}
                                                        options={numeric.map((c) => ({ value: c, label: c }))} />
                                                    {chart.type === 'heatmap' && (
                                                        <Select aria-label="series" placeholder="down the side" value={chart.series} style={{ width: 160 }}
                                                            onChange={(series) => setChart({ ...chart, series })}
                                                            options={result.columns.map((c) => ({ value: c, label: c }))} />
                                                    )}
                                                    {chart.type === 'map' && (['lat', 'lon'] as const).map((k) => (
                                                        <Select key={k} aria-label={k === 'lat' ? 'latitude' : 'longitude'} style={{ width: 140 }}
                                                            placeholder={k === 'lat' ? 'latitude' : 'longitude'} value={chart[k]}
                                                            onChange={(v) => setChart({ ...chart, [k]: v })}
                                                            options={numeric.map((c) => ({ value: c, label: c }))} />
                                                    ))}
                                                </Space>
                                                <ResultChart columns={result.columns} rows={result.rows} chart={chart} />
                                            </>
                                        ),
                                    },
                                ]}
                            />
                        </Surface>
                    )}
                    <Surface style={{ marginTop: 12 }}>
                        <Tabs
                            items={[
                                {
                                    key: 'saved',
                                    label: 'Saved',
                                    children: saved.data?.length ? (
                                        <Table<SavedQuery>
                                            size="small"
                                            rowKey="id"
                                            pagination={{ pageSize: 10, hideOnSinglePage: true }}
                                            dataSource={saved.data}
                                            columns={[
                                                {
                                                    title: 'Name',
                                                    render: (_, q) => (
                                                        <a onClick={() => open(q, q)}>{q.name}</a>
                                                    ),
                                                },
                                                { title: 'Owner', render: (_, q) => q.owner.replace('urn:li:corpuser:', '') },
                                                { title: '', render: (_, q) => (q.shared ? <Tag color="blue">shared</Tag> : null) },
                                                { title: 'Changed', render: (_, q) => ago(q.updated_ms) },
                                                { title: '', render: (_, q) => (
                                                    <Space size={4}>
                                                        <BiAssetButtons kind="query" id={q.id} name={q.name} role={q.role}
                                                            onRestored={() => setTick((x) => x + 1)} />
                                                    </Space>
                                                ) },
                                            ]}
                                        />
                                    ) : (
                                        <Empty description="No saved queries yet" />
                                    ),
                                },
                                {
                                    key: 'history',
                                    label: 'History',
                                    children: (
                                        <Table<SqlRun>
                                            size="small"
                                            rowKey="id"
                                            pagination={{ pageSize: 10, hideOnSinglePage: true }}
                                            dataSource={history.data ?? []}
                                            columns={[
                                                {
                                                    title: 'Query',
                                                    render: (_, h) => (
                                                        <a onClick={() => open(h)} style={{ fontFamily: 'monospace' }}>
                                                            {h.sql.slice(0, 80)}
                                                        </a>
                                                    ),
                                                },
                                                {
                                                    title: 'Result',
                                                    render: (_, h) =>
                                                        h.status === 'ok' ? <StatusBadge state="succeeded" label={`${h.rows} row(s)`} plain />
                                                            : <StatusBadge state="failed" title={h.error ?? ''} plain />,
                                                },
                                                { title: 'When', render: (_, h) => ago(h.at_ms) },
                                            ]}
                                        />
                                    ),
                                },
                            ]}
                        />
                    </Surface>
                </div>
            </SqlGrid>
            <Modal title={current ? 'Save changes' : 'Save query'} open={saving} onOk={() => void save()} onCancel={() => setSaving(false)}
                okText="Save" okButtonProps={{ disabled: !form.name.trim() }}>
                <Space direction="vertical" style={{ width: '100%' }}>
                    <Input aria-label="Name" placeholder="Name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
                    <Input.TextArea aria-label="Description" placeholder="What it answers" value={form.description}
                        onChange={(e) => setForm({ ...form, description: e.target.value })} />
                    <Checkbox checked={form.shared} onChange={(e) => setForm({ ...form, shared: e.target.checked })}>
                        Share with everyone (each person runs it as themselves)
                    </Checkbox>
                </Space>
            </Modal>
        </Page>
    );
}
