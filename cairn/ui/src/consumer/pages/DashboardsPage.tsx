import { Alert, Button, Checkbox, Empty, Input, InputNumber, Modal, Select, Space, Table, Tabs, Tag, message } from 'antd';
import React, { useEffect, useState } from 'react';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import { CHART_TYPES } from '../api/types';
import type { BuiltDashboard, ChartSpec, Dashboard, Metric, MetricQuery, DashboardControl, DashboardState, DashboardTile, RenderedDashboard, SavedQuery, SqlAlert, Workbook } from '../api/types';
import { ResultChart } from '../components/ResultChart';
import { Muted, Page, Surface } from '../components/tokens';

const STATE: Record<SqlAlert['state'], string> = { ok: 'green', triggered: 'red', error: 'orange', unknown: 'default' };

/** One control: a list of values to pick from, a range, a date range or text. */
function Control({ board, control, value, onChange }: {
    board: string; control: DashboardControl; value: unknown; onChange: (v: unknown) => void;
}) {
    const options = useDheQuery<unknown[]>(control.kind === 'values' ? `/sql/dashboards/${board}/controls/${control.id}/values` : null,
        (c, signal) => c.dashboardControlValues(board, control.id, { signal }));
    const pair = Array.isArray(value) && value.length === 2 ? value : [null, null];
    const label = control.label || control.column;
    if (control.kind === 'values') {
        return (
            <Select aria-label={label} mode="multiple" allowClear placeholder={label} style={{ minWidth: 200 }}
                value={Array.isArray(value) ? (value as (string | number)[]) : []} loading={options.loading}
                onChange={(v) => onChange(v.length ? v : null)}
                options={(options.data ?? []).map((o) => ({ value: o as string | number, label: o === null ? '(empty)' : String(o) }))} />
        );
    }
    if (control.kind === 'range') {
        return (
            <Space size={4}>
                <InputNumber aria-label={`${label} from`} placeholder={`${label} from`} value={pair[0] as number | null}
                    onChange={(v) => onChange([v ?? null, pair[1]])} />
                <InputNumber aria-label={`${label} to`} placeholder="to" value={pair[1] as number | null}
                    onChange={(v) => onChange([pair[0], v ?? null])} />
            </Space>
        );
    }
    if (control.kind === 'date_range') {
        return (
            <Space size={4}>
                <Input type="date" aria-label={`${label} from`} value={(pair[0] as string) ?? ''} style={{ width: 160 }}
                    onChange={(e) => onChange([e.target.value || null, pair[1]])} />
                <Input type="date" aria-label={`${label} to`} value={(pair[1] as string) ?? ''} style={{ width: 160 }}
                    onChange={(e) => onChange([pair[0], e.target.value || null])} />
            </Space>
        );
    }
    return <Input aria-label={label} placeholder={`${label} contains`} allowClear style={{ width: 200 }} value={(value as string) ?? ''}
        onChange={(e) => onChange(e.target.value || null)} />;
}

/** One dashboard, every tile run as the viewer, filtered by the controls, cross-filtered by clicks and drilled. */
function Board({ id }: { id: string }) {
    const [state, setState] = useState<DashboardState>({});
    const key = JSON.stringify(state);
    const q = useDheQuery<RenderedDashboard>(`/sql/dashboards/${id}/render#${key}`, (c, signal) =>
        c.renderDashboard(id, { signal }, key === '{}' ? undefined : state));
    const [last, setLast] = useState<RenderedDashboard | null>(null);
    useEffect(() => { if (q.data) setLast(q.data); }, [q.data]);
    const board = q.data ?? last;
    if (q.error) return <Alert type="error" message={q.error.message} />;
    if (!board) return <Muted>Loading…</Muted>;
    const selection = state.selection ?? [];
    const select = (from: number, column: string, value: unknown) => {
        const same = selection.some((x) => x.from === from && x.column === column && x.value === value);
        setState({ ...state, selection: same ? selection.filter((x) => x.from !== from) : [...selection.filter((x) => x.from !== from), { column, value, from }] });
    };
    const drill = (i: number, path: unknown[]) => setState({
        ...state, drill: { ...(state.drill ?? {}), [String(i)]: path }, selection: selection.filter((x) => x.from !== i) });
    return (
        <>
            {(board.controls?.length || selection.length > 0) && (
                <Surface style={{ marginBottom: 12 }}>
                    <Space wrap data-testid="controls">
                        {(board.controls ?? []).map((c) => (
                            <Control key={c.id} board={id} control={c} value={state.controls?.[c.id] ?? c.default}
                                onChange={(v) => setState({ ...state, controls: { ...(state.controls ?? {}), [c.id]: v } })} />
                        ))}
                        {selection.map((x) => (
                            <Tag key={`${x.from}-${x.column}`} closable onClose={() => setState({ ...state, selection: selection.filter((y) => y !== x) })}>
                                {x.column} = {String(x.value)}
                            </Tag>
                        ))}
                        {q.loading && <Muted>Updating…</Muted>}
                    </Space>
                </Surface>
            )}
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: 12 }}>
                {board.tiles.map((t, i) => {
                    const path = t.level?.path ?? [];
                    const mine = selection.find((x) => x.from === i);
                    const deeper = !!t.drill && t.drill.length > path.length + 1;
                    const x = t.chart?.x ?? '';
                    return (
                        <Surface key={i} style={{ gridColumn: t.width === 2 ? 'span 2' : undefined }}>
                            <Space wrap style={{ justifyContent: 'space-between', width: '100%' }}>
                                <strong>{t.title}</strong>
                                {path.length > 0 && (
                                    <Space size={4}>
                                        <Muted>{t.drill!.slice(0, path.length).map((c, k) => `${c}: ${String(path[k])}`).join(' › ')}</Muted>
                                        <Button size="small" onClick={() => drill(i, path.slice(0, -1))}>Up</Button>
                                    </Space>
                                )}
                                {mine && deeper && (
                                    <Button size="small" type="primary" onClick={() => drill(i, [...path, mine.value])}>
                                        Drill into {String(mine.value)}
                                    </Button>
                                )}
                            </Space>
                            {(t.applied?.length ?? 0) > 0 && (
                                <div><Muted>Filtered by {t.applied!.map((f) => f.column).filter((c, k, a) => a.indexOf(c) === k).join(', ')}</Muted></div>
                            )}
                            {t.error ? (
                                <Alert type="warning" message={t.error} style={{ marginTop: 8 }} />
                            ) : t.result && t.chart && t.chart.type !== 'table' ? (
                                <ResultChart columns={t.result.columns} rows={t.result.rows} chart={t.chart} height={220}
                                    selected={mine?.value} onSelect={x ? (v) => select(i, x, v) : undefined} />
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
                    );
                })}
            </div>
        </>
    );
}

type ControlDraft = { column: string; kind: DashboardControl['kind']; label: string };

function Dashboards({ saved }: { saved: SavedQuery[] }) {
    const { client } = useConsumerApi();
    const [tick, setTick] = useState(0);
    const list = useDheQuery<Dashboard[]>(`/sql/dashboards#${tick}`, (c, signal) => c.dashboards({ signal }));
    const books = useDheQuery<Workbook[]>('/sql/workbooks', (c, signal) => c.workbooks({ signal }));
    const metricList = useDheQuery<Metric[]>('/sql/metrics', (c, signal) => c.metrics({ signal }));
    const [open, setOpen] = useState<string | null>(null);
    const [editing, setEditing] = useState(false);
    const [asking, setAsking] = useState(false);
    const [question, setQuestion] = useState('');
    const [building, setBuilding] = useState(false);
    const [built, setBuilt] = useState<BuiltDashboard | null>(null);
    const [form, setForm] = useState({ name: '', shared: false, queries: [] as string[], workbooks: [] as string[],
        chart: 'bar' as ChartSpec['type'], drill: [] as string[], controls: [] as ControlDraft[],
        metrics: [] as string[], metricDims: [] as string[], metricGrain: undefined as MetricQuery['grain'] });

    const elementTiles = (): DashboardTile[] => (books.data ?? []).filter((w) => form.workbooks.includes(w.id)).flatMap((w) =>
        w.spec.elements.map((el) => {
            const x = el.group_by?.[0] ?? '';
            const y = (el.aggregates ?? []).map((a) => a.name);
            const drill = form.drill.length && x && form.drill[0] === x ? form.drill : undefined;
            return { element: el, connection: w.connection, title: w.name, width: 1 as const, drill,
                     chart: x && y.length ? { type: form.chart, x, y } : { type: 'table' as const } };
        }));
    const firstTable = () => (books.data ?? []).find((w) => form.workbooks.includes(w.id));

    const create = async () => {
        try {
            const src = firstTable();
            const d = await client.saveDashboard({ name: form.name, shared: form.shared,
                tiles: [...form.queries.map((q) => ({ query: q, title: '', width: 1 as const })), ...elementTiles(),
                    ...(form.metrics.length ? [{ metric: { metrics: form.metrics, dimensions: form.metricDims, grain: form.metricGrain },
                        title: '', width: 2 as const }] : [])],
                controls: form.controls.filter((c) => c.column.trim()).map((c, k) => ({
                    id: `c${k + 1}`, label: c.label || c.column, column: c.column.trim(), kind: c.kind,
                    source: c.kind === 'values' && src ? { connection: src.connection, table: src.spec.elements[0].source.table } : null })) });
            setEditing(false);
            setTick((t) => t + 1);
            setOpen(d.id);
        } catch (e) {
            message.error((e as Error).message);
        }
    };
    const build = async () => {
        setBuilding(true);
        setBuilt(null);
        try {
            const out = await client.buildDashboard({ question, save: true });
            setBuilt(out);
            setTick((t) => t + 1);
            if (out.dashboard) setOpen(out.dashboard.id);
            if (!out.refused.length) setAsking(false);
        } catch (e) {
            message.error((e as Error).message);
        } finally {
            setBuilding(false);
        }
    };
    const setControl = (k: number, patch: Partial<ControlDraft>) =>
        setForm({ ...form, controls: form.controls.map((c, j) => (j === k ? { ...c, ...patch } : c)) });
    const canCreate = !!form.name.trim() && (form.queries.length > 0 || form.workbooks.length > 0 || form.metrics.length > 0);

    return (
        <>
            <Space style={{ marginBottom: 12 }} wrap>
                <Select aria-label="Dashboard" placeholder="Open a dashboard" style={{ minWidth: 260 }} value={open ?? undefined}
                    onChange={setOpen} options={(list.data ?? []).map((d) => ({ value: d.id, label: d.shared ? `${d.name} (shared)` : d.name }))} />
                <Button onClick={() => { setBuilt(null); setAsking(true); }}>Build from a question</Button>
                <Button onClick={() => setEditing(true)} disabled={!saved.length && !(books.data ?? []).length && !(metricList.data ?? []).length}>New dashboard</Button>
                {!saved.length && !(books.data ?? []).length && <Muted>Save queries in SQL, or workbooks, first; a dashboard shows them.</Muted>}
            </Space>
            {open ? <Board key={`${open}-${tick}`} id={open} /> : <Empty description="Pick or create a dashboard" />}
            <Modal title="Build a dashboard from a question" open={asking} onCancel={() => setAsking(false)} onOk={() => void build()}
                okText="Build" confirmLoading={building} okButtonProps={{ disabled: question.trim().length < 3 }}>
                <Space direction="vertical" style={{ width: '100%' }}>
                    <Input.TextArea aria-label="Question" rows={3} value={question} onChange={(e) => setQuestion(e.target.value)}
                        placeholder="Revenue by region this year, and orders by status" />
                    <Muted>Cairn builds it from the metrics and tables you may see, checks every tile as you, and saves it as yours (private).</Muted>
                    {built && built.refused.length > 0 && (
                        <Alert type="info" showIcon message={`Built ${built.tiles.length} tile(s); left out:`}
                            description={<ul style={{ margin: 0 }}>{built.refused.map((r, i) => <li key={i}>{r.error}</li>)}</ul>} />
                    )}
                </Space>
            </Modal>
            <Modal title="New dashboard" open={editing} onOk={() => void create()} onCancel={() => setEditing(false)} width={680}
                okText="Create" okButtonProps={{ disabled: !canCreate }}>
                <Space direction="vertical" style={{ width: '100%' }}>
                    <Input aria-label="Dashboard name" placeholder="Name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
                    <Select aria-label="Queries" mode="multiple" placeholder="Saved queries to show" style={{ width: '100%' }}
                        value={form.queries} onChange={(queries) => setForm({ ...form, queries })}
                        options={saved.map((q) => ({ value: q.id, label: q.name }))} />
                    <Select aria-label="Workbooks" mode="multiple" placeholder="Workbooks to show (one tile per element)" style={{ width: '100%' }}
                        value={form.workbooks} onChange={(workbooks) => setForm({ ...form, workbooks })}
                        options={(books.data ?? []).map((w) => ({ value: w.id, label: w.name }))} />
                    {form.workbooks.length > 0 && (
                        <Space wrap>
                            <Select aria-label="Chart type" style={{ width: 150 }} value={form.chart} onChange={(chart) => setForm({ ...form, chart })}
                                options={CHART_TYPES.map((t) => ({ value: t, label: t.replace('_', ' ') }))} />
                            <Select aria-label="Drill path" mode="tags" placeholder="Drill path: region, country, city" style={{ minWidth: 300 }}
                                value={form.drill} onChange={(drill) => setForm({ ...form, drill })} />
                        </Space>
                    )}
                    <Select aria-label="Dashboard metrics" mode="multiple" placeholder="Metrics to show (one tile)" style={{ width: '100%' }}
                        value={form.metrics} onChange={(metrics) => setForm({ ...form, metrics })}
                        options={(metricList.data ?? []).map((m) => ({ value: m.name, label: m.certified ? `${m.label} (certified)` : m.label }))} />
                    {form.metrics.length > 0 && (
                        <Space wrap>
                            <Select aria-label="Metric breakdown" mode="tags" placeholder="Break down by" style={{ minWidth: 240 }}
                                value={form.metricDims} onChange={(metricDims) => setForm({ ...form, metricDims })} />
                            <Select aria-label="Metric grain" allowClear placeholder="Over time" style={{ width: 140 }} value={form.metricGrain}
                                onChange={(metricGrain) => setForm({ ...form, metricGrain })}
                                options={['day', 'week', 'month', 'quarter', 'year'].map((g) => ({ value: g, label: g }))} />
                        </Space>
                    )}
                    <strong>Controls</strong>
                    {form.controls.map((c, k) => (
                        <Space key={k} wrap>
                            <Input aria-label={`Control column ${k + 1}`} placeholder="column" value={c.column} style={{ width: 160 }}
                                onChange={(e) => setControl(k, { column: e.target.value })} />
                            <Select aria-label={`Control kind ${k + 1}`} value={c.kind} style={{ width: 140 }} onChange={(kind) => setControl(k, { kind })}
                                options={[{ value: 'values', label: 'list of values' }, { value: 'range', label: 'number range' },
                                          { value: 'date_range', label: 'date range' }, { value: 'text', label: 'text contains' }]} />
                            <Input aria-label={`Control label ${k + 1}`} placeholder="label" value={c.label} style={{ width: 160 }}
                                onChange={(e) => setControl(k, { label: e.target.value })} />
                            <Button aria-label={`Remove control ${k + 1}`} onClick={() => setForm({ ...form, controls: form.controls.filter((_, j) => j !== k) })}>×</Button>
                        </Space>
                    ))}
                    <Button size="small" onClick={() => setForm({ ...form, controls: [...form.controls, { column: '', kind: 'values', label: '' }] })}>
                        Add control
                    </Button>
                    <Muted>A control filters every tile that has its column. A list of values takes its choices from the first workbook&apos;s table.</Muted>
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
            <Muted>Saved queries and workbooks with their charts, filters and drill-down, and alerts. Every tile and check runs as a person, under their policies.</Muted>
            <Tabs style={{ marginTop: 12 }} items={[
                { key: 'boards', label: 'Dashboards', children: <Dashboards saved={saved.data ?? []} /> },
                { key: 'alerts', label: 'Alerts', children: <Alerts saved={saved.data ?? []} /> },
            ]} />
        </Page>
    );
}
