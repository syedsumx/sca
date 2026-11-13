import { Alert, Button, Checkbox, Collapse, Empty, Input, InputNumber, Modal, Select, Space, Table, Tabs, Tag, Tooltip, message } from 'antd';
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { SqlLanguage } from 'sql-formatter';

import { BiAssetButtons } from '../components/BiAdmin';
import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { ChartSpec, DataResult, SavedQuery, SchemaTable, SqlRun, SystemTablesInfo } from '../api/types';
import { ResultChart, toCsv } from '../components/ResultChart';
import { CHART_TYPES } from '../api/types';
import { SqlEditor, type SqlEditorHandle } from '../components/SqlEditor';
import { statementTitle, toRun } from '../sql/statements';
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

/** One statement of a run and how it went (a result tab). */
export interface StatementRun {
    key: string;
    text: string;
    from: number;
    to: number;
    status: 'queued' | 'running' | 'succeeded' | 'failed' | 'stopped' | 'skipped';
    result?: DataResult;
    error?: string;
}

const RUN_STATE: Record<StatementRun['status'], { state: string; label: string }> = {
    queued: { state: 'waiting', label: 'Waiting' },
    running: { state: 'running', label: 'Running' },
    succeeded: { state: 'succeeded', label: 'Done' },
    failed: { state: 'failed', label: 'Failed' },
    stopped: { state: 'stopped', label: 'Stopped' },
    skipped: { state: 'skipped', label: 'Not run' },
};

/** The formatter's dialect for a connection's platform (standard SQL when it has none of its own). */
export function sqlDialect(platform: string | undefined): SqlLanguage {
    const known: Record<string, SqlLanguage> = {
        trino: 'trino', presto: 'trino', postgres: 'postgresql', postgresql: 'postgresql', mysql: 'mysql',
        mariadb: 'mariadb', snowflake: 'snowflake', bigquery: 'bigquery', redshift: 'redshift', sqlite: 'sqlite',
        databricks: 'spark', spark: 'spark', mssql: 'transactsql', duckdb: 'duckdb', clickhouse: 'clickhouse',
    };
    return known[(platform ?? '').toLowerCase()] ?? 'sql';
}

/** The SQL workspace: browse what you may query, run read-only SQL as yourself, chart and download the
 * result, keep a history, and save queries for yourself or everyone. */
export function SqlWorkspacePage() {
    const { client } = useConsumerApi();
    const conns = useDheQuery('/data/sources', (c, signal) => c.dataConnections({ signal }));
    const [connection, setConnection] = useState<string | undefined>();
    const [sql, setSql] = useState('');
    const [limit, setLimit] = useState(200);
    const [runs, setRuns] = useState<StatementRun[]>([]);
    const [activeRun, setActiveRun] = useState<string | undefined>();
    const [running, setRunning] = useState(false);
    const stopper = useRef<AbortController | null>(null);
    const editor = useRef<SqlEditorHandle>(null);
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

    // The statement at the cursor, the selection's statements, or all; one after another, each its own result, as
    // the person; the first that fails stops the rest (and is selected in the editor).
    const run = useCallback(async (mode: 'current' | 'all' = 'current') => {
        if (!connection || running) return;
        const picked = toRun(sql, editor.current?.selection() ?? { from: 0, to: 0 }, mode);
        if (!picked.length) return;
        const stamp = Date.now();
        const list: StatementRun[] = picked.map((st, i) => ({ key: `${stamp}-${i}`, ...st, status: 'queued' }));
        const put = (i: number, patch: Partial<StatementRun>) => {
            list[i] = { ...list[i], ...patch };
            setRuns([...list]);
        };
        setRuns(list);
        setActiveRun(list[0].key);
        setRunning(true);
        const ctl = new AbortController();
        stopper.current = ctl;
        try {
            for (let i = 0; i < list.length; i += 1) {
                if (ctl.signal.aborted) {
                    for (let j = i; j < list.length; j += 1) put(j, { status: 'stopped' });
                    break;
                }
                put(i, { status: 'running' });
                setActiveRun(list[i].key);
                try {
                    put(i, { status: 'succeeded', result: await client.runSql(list[i].text, connection, limit, { signal: ctl.signal }) });
                } catch (e) {
                    if (ctl.signal.aborted) {
                        for (let j = i; j < list.length; j += 1) put(j, { status: 'stopped' });
                        break;
                    }
                    put(i, { status: 'failed', error: describeError(e) });
                    for (let j = i + 1; j < list.length; j += 1) put(j, { status: 'skipped' });
                    if (list.length > 1 || mode === 'all') editor.current?.select(list[i].from, list[i].to);
                    break;
                }
            }
        } finally {
            stopper.current = null;
            setRunning(false);
            setTick((t) => t + 1);
        }
    }, [client, connection, sql, limit, running]);

    const stop = () => stopper.current?.abort();

    const platform = conns.data?.find((c) => c.connection === connection)?.platform;
    // The formatter (every dialect's grammar) is loaded the first time someone formats, not with the page.
    const tidy = async () => {
        try {
            const { format } = await import('sql-formatter');
            setSql(format(sql, { language: sqlDialect(platform), keywordCase: 'upper', tabWidth: 2 }));
        } catch (e) {
            message.warning(`Could not format it: ${describeError(e)}`);
        }
    };

    const shown = runs.find((r) => r.key === activeRun) ?? runs[runs.length - 1];
    const result = shown?.result ?? null;

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
                                        onDoubleClick={() => editor.current?.insert(t.name)}
                                        title="Double-click to insert at the cursor">
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
                                                onDoubleClick={() => editor.current?.insert(t.name)}
                                                title={`${t.description} Double-click to insert at the cursor.`}>
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
                            ref={editor}
                            value={sql}
                            onChange={setSql}
                            tables={schema.data ?? []}
                            onRun={(mode) => void run(mode)}
                            minLines={10}
                            maxLines={28}
                            placeholder="SELECT region, count(*) AS orders FROM sales.orders GROUP BY region"
                        />
                        <Space style={{ marginTop: 8 }} wrap>
                            <Tooltip title="The statement at the cursor, or the selection (Ctrl/⌘+Enter)">
                                <Button type="primary" onClick={() => void run('current')} loading={running}
                                    disabled={!connection || !sql.trim()}>
                                    Run
                                </Button>
                            </Tooltip>
                            <Tooltip title="Every statement, one after another (Ctrl/⌘+Shift+Enter)">
                                <Button onClick={() => void run('all')} disabled={!connection || !sql.trim() || running}>
                                    Run all
                                </Button>
                            </Tooltip>
                            {running && <Button danger onClick={stop}>Stop</Button>}
                            <span>
                                Rows <InputNumber aria-label="Rows" min={1} max={1000} value={limit} onChange={(v) => setLimit(Number(v) || 200)} />
                            </span>
                            <Button onClick={() => void tidy()} disabled={!sql.trim()}>Format</Button>
                            <Button onClick={() => setSaving(true)} disabled={!sql.trim()}>
                                {current ? 'Save changes' : 'Save'}
                            </Button>
                            <Button onClick={download} disabled={!result}>
                                Download CSV
                            </Button>
                        </Space>
                        <Muted style={{ display: 'block', marginTop: 6 }}>
                            Ctrl/⌘+Enter runs the statement at the cursor or the selection · Ctrl/⌘+Shift+Enter runs all ·
                            Ctrl+Space suggests · Ctrl/⌘+F finds and replaces
                        </Muted>
                    </Surface>
                    {runs.length > 1 && (
                        <div role="group" aria-label="Statements">
                        <Tabs
                            style={{ marginTop: 12 }}
                            activeKey={shown?.key}
                            onChange={setActiveRun}
                            items={runs.map((r, i) => ({
                                key: r.key,
                                label: (
                                    <span title={r.text}>
                                        <StatusBadge state={RUN_STATE[r.status].state} label={`${i + 1}`} plain />{' '}
                                        {statementTitle(r.text, 32)}
                                    </span>
                                ),
                            }))}
                        />
                        </div>
                    )}
                    {shown?.error && <Alert style={{ marginTop: 12 }} type="error" message={shown.error} showIcon />}
                    {shown && !shown.result && !shown.error && shown.status !== 'running' && (
                        <Muted style={{ display: 'block', marginTop: 12 }}>
                            {shown.status === 'stopped' ? 'Stopped before it finished.' : shown.status === 'skipped'
                                ? 'Not run: a statement before it failed.' : 'Waiting for the statements before it.'}
                        </Muted>
                    )}
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
                                                    <BiAssetButtons compact kind="query" id={q.id} name={q.name} role={q.role}
                                                        onRestored={() => setTick((x) => x + 1)} />
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
