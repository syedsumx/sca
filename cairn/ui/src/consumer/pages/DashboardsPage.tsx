import { Alert, Button, Checkbox, Empty, Grid, Input, InputNumber, Modal, Select, Space, Table, Tabs, Tag, message } from 'antd';
import React, { useEffect, useState } from 'react';
import { useHistory, useLocation } from 'react-router-dom';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import { CHART_TYPES } from '../api/types';
import type { BuiltDashboard, ChartSpec, Dashboard, DashboardAction, Metric, MetricQuery, DashboardControl, DashboardState, DashboardTile, RenderedDashboard, SavedQuery, SqlAlert, Workbook } from '../api/types';
import { BiAssetButtons } from '../components/BiAdmin';
import { BiFolders, BiUsageOverview } from '../components/BiCollab';
import { OpsDashboardEditor, OpsTileView, RANGES, REFRESHES, sourceOf } from '../components/OpsTiles';
import { ResultChart } from '../components/ResultChart';
import { Muted, Page, Surface, focusRing, tokens } from '../components/tokens';
import styled from 'styled-components';
import { useT } from '../i18n';
import { When } from '../utils/time';
import { ErrorNotice, notifyError } from '../components/ErrorNotice';
import { RESOURCE_SPECS } from '../resources/catalog';
import { ResourceView } from '../resources/ResourceView';
import { StatusBadge } from '../components/StatusBadge';
import { PageHeader } from '../guide/PageHeader';
import { pageHelp } from '../guide/help';

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

/** An ops dashboard's $variable: its choices from Prometheus, Loki, the instances or the SLOs (as the viewer). */
function Variable({ board, control, value, onChange }: {
    board: string; control: DashboardControl; value: unknown; onChange: (v: unknown) => void;
}) {
    const options = useDheQuery<unknown[]>(`/sql/dashboards/${board}/controls/${control.id}/values`,
        (c, signal) => c.dashboardControlValues(board, control.id, { signal }));
    const label = control.label || control.id;
    const multi = !!control.multi;
    const list = Array.isArray(value) ? (value as string[]) : typeof value === 'string' && value ? (multi ? value.split('|') : [value]) : [];
    return (
        <Select aria-label={label} showSearch allowClear mode={multi ? 'multiple' : undefined} placeholder={`$${control.id}`}
            style={{ minWidth: 180 }} loading={options.loading} value={multi ? list : (list[0] ?? undefined)}
            onChange={(v: string | string[] | undefined) => onChange(Array.isArray(v) ? (v.length ? v : null) : v ?? null)}
            notFoundContent={options.error ? 'Could not list the choices' : undefined}
            options={(options.data ?? []).map((o) => ({ value: String(o), label: String(o) }))} />
    );
}

/** The dashboard's actions: buttons (a form when they ask for fields) run as the person clicking. */
function Actions({ board, row, onDone }: { board: string; row: Record<string, unknown> | null; onDone: () => void }) {
    const { client } = useConsumerApi();
    const list = useDheQuery<DashboardAction[]>(`/sql/dashboards/${board}/actions`, (c, signal) => c.dashboardActions(board, { signal }));
    const [open, setOpen] = useState<DashboardAction | null>(null);
    const [inputs, setInputs] = useState<Record<string, unknown>>({});
    const [busy, setBusy] = useState(false);
    const go = async (a: DashboardAction, values: Record<string, unknown>) => {
        setBusy(true);
        try {
            const out = await client.runDashboardAction(board, a.id, { inputs: values, row: a.kind === 'input_update' ? row ?? undefined : undefined });
            const failed = out.steps?.filter((s) => s.status !== 'ok') ?? [];
            if (failed.length) message.warning(`${a.label}: ${failed.map((s) => `${s.name} ${s.status}`).join(', ')}`);
            else message.success(`${a.label}: done`);
            setOpen(null);
            setInputs({});
            onDone();
        } catch (e) {
            notifyError(e, { action: a.label });
        } finally {
            setBusy(false);
        }
    };
    const start = (a: DashboardAction) => {
        if (a.config.fields?.length) {
            setInputs({});
            setOpen(a);
        } else if (a.config.confirm) {
            Modal.confirm({ title: a.config.confirm, okText: a.label, onOk: () => go(a, {}) });
        } else void go(a, {});
    };
    if (!list.data?.length) return null;
    return (
        <>
            {list.data.map((a) => (
                <Button key={a.id} onClick={() => start(a)} disabled={a.kind === 'input_update' && !row} loading={busy && open?.id === a.id}
                    title={a.kind === 'input_update' && !row ? 'Pick a row in a table tile first' : undefined}>
                    {a.label}
                </Button>
            ))}
            <Modal title={open?.label} open={!!open} onCancel={() => setOpen(null)} confirmLoading={busy}
                okText={open?.label} onOk={() => open && void go(open, inputs)}>
                <Space direction="vertical" style={{ width: '100%' }}>
                    {open?.config.confirm && <Muted>{open.config.confirm}</Muted>}
                    {(open?.config.fields ?? []).map((f) => f.type === 'boolean' ? (
                        <Checkbox key={f.name} aria-label={f.label} checked={inputs[f.name] === true}
                            onChange={(e) => setInputs({ ...inputs, [f.name]: e.target.checked })}>{f.label}</Checkbox>
                    ) : f.type === 'number' || f.type === 'integer' ? (
                        <InputNumber key={f.name} aria-label={f.label} placeholder={f.label + (f.required ? ' *' : '')} style={{ width: '100%' }}
                            value={(inputs[f.name] as number | undefined) ?? null} onChange={(v) => setInputs({ ...inputs, [f.name]: v ?? undefined })} />
                    ) : (
                        <Input key={f.name} aria-label={f.label} type={f.type === 'date' ? 'date' : 'text'} placeholder={f.label + (f.required ? ' *' : '')}
                            value={(inputs[f.name] as string | undefined) ?? ''} onChange={(e) => setInputs({ ...inputs, [f.name]: e.target.value })} />
                    ))}
                </Space>
            </Modal>
        </>
    );
}

const EVERY = [{ value: 1440, label: 'Every day' }, { value: 10080, label: 'Every week' }, { value: 60, label: 'Every hour' }];

/** Schedule this dashboard as CSV or PDF; every recipient gets their own view. */
function ScheduleExport({ board, name }: { board: string; name: string }) {
    const { client } = useConsumerApi();
    const [open, setOpen] = useState(false);
    const [form, setForm] = useState({ format: 'csv' as 'csv' | 'pdf', every: 10080, people: [] as string[] });
    const [busy, setBusy] = useState(false);
    const save = async (now: boolean) => {
        setBusy(true);
        try {
            const r = await client.saveReport({ name, target: { dashboard: board }, format: form.format, every_minutes: form.every,
                recipients: form.people.map((p) => (p.startsWith('urn:li:corpuser:') ? p : `urn:li:corpuser:${p}`)) });
            if (now) {
                const out = await client.runReport(r.id);
                message.success(`Sent to ${out.sent.length}` + (out.skipped.length ? `; ${out.skipped.length} may not open this dashboard` : ''));
            } else message.success('Export scheduled');
            setOpen(false);
        } catch (e) {
            notifyError(e);
        } finally {
            setBusy(false);
        }
    };
    return (
        <>
            <Button onClick={() => setOpen(true)}>Schedule export</Button>
            <Modal title={`Schedule ${name}`} open={open} onCancel={() => setOpen(false)} footer={[
                <Button key="now" loading={busy} onClick={() => void save(true)}>Schedule and send now</Button>,
                <Button key="save" type="primary" loading={busy} onClick={() => void save(false)}>Schedule</Button>]}>
                <Space direction="vertical" style={{ width: '100%' }}>
                    <Space wrap>
                        <Select aria-label="Export format" value={form.format} style={{ width: 110 }} onChange={(format) => setForm({ ...form, format })}
                            options={[{ value: 'csv', label: 'CSV' }, { value: 'pdf', label: 'PDF' }]} />
                        <Select aria-label="How often" value={form.every} style={{ width: 150 }} onChange={(every) => setForm({ ...form, every })}
                            options={EVERY} />
                    </Space>
                    <Select aria-label="Recipients" mode="tags" placeholder="Recipients (user names); you get a copy too" style={{ width: '100%' }}
                        value={form.people} onChange={(people) => setForm({ ...form, people })} />
                    <Muted>Each recipient&apos;s copy is made as them: their masks and row filters apply. Someone who may not open the dashboard gets nothing.</Muted>
                </Space>
            </Modal>
        </>
    );
}

/** One dashboard, every tile run as the viewer, filtered by the controls, cross-filtered by clicks and drilled. */
function Board({ id, onOpen }: { id: string; onOpen?: (id: string) => void }) {
    // On a phone (narrower than antd's md, 768px) tiles stack in one column.
    const wide = Grid.useBreakpoint().md !== false;
    const { client } = useConsumerApi();
    const [state, setState] = useState<DashboardState>({});
    const [refresh, setRefresh] = useState(0);
    const [row, setRow] = useState<Record<string, unknown> | null>(null);
    const [every, setEvery] = useState<number | null>(null);          // the viewer's auto-refresh (seconds; 0: off)
    const [editing, setEditing] = useState<Dashboard | null>(null);
    const [last, setLast] = useState<RenderedDashboard | null>(null);
    const timeControl = last?.controls?.find((c) => c.kind === 'time');
    const pollMs = (every ?? timeControl?.refresh ?? 0) * 1000 || undefined;
    const key = JSON.stringify(state);
    const q = useDheQuery<RenderedDashboard>(`/sql/dashboards/${id}/render#${key}#${refresh}`, (c, signal) =>
        c.renderDashboard(id, { signal }, key === '{}' ? undefined : state), { pollMs });
    useEffect(() => { if (q.data) setLast(q.data); }, [q.data]);
    const board = q.data ?? last;
    if (q.error) return <ErrorNotice error={q.error} />;
    if (!board) return <Muted>Loading…</Muted>;
    const ops = board.tiles.some((t) => sourceOf(t) !== null) || (board.controls ?? []).some((c) => c.kind === 'time' || c.kind === 'variable');
    const setControl = (cid: string, v: unknown) => setState({ ...state, controls: { ...(state.controls ?? {}), [cid]: v } });
    const copy = async () => {
        try {
            const d = await client.request<Dashboard>('POST', `/sql/dashboards/${encodeURIComponent(id)}/copy`, {});
            message.success(`Copied as ${d.name}`);
            onOpen?.(d.id);
        } catch (e) {
            notifyError(e, { action: 'Copy the dashboard' });
        }
    };
    const exportGrafana = async () => {
        try {
            const doc = await client.request<Record<string, unknown>>('GET', `/sql/dashboards/${encodeURIComponent(id)}/grafana`);
            const blob = new Blob([JSON.stringify(doc, null, 2)], { type: 'application/json' });
            if (typeof URL.createObjectURL === 'function') {
                const a = document.createElement('a');
                a.href = URL.createObjectURL(blob);
                a.download = `${board.name.replace(/[^\w.-]+/g, '-')}.grafana.json`;
                a.click();
                URL.revokeObjectURL(a.href);
            }
            const skipped = ((doc.cairn as { skipped?: unknown[] } | undefined)?.skipped ?? []).length;
            message.success(skipped ? `Exported; ${skipped} tile(s) run only in Cairn and are text panels there` : 'Exported for Grafana');
        } catch (e) {
            notifyError(e, { action: 'Export to Grafana' });
        }
    };
    const edit = async () => {
        try {
            setEditing(await client.request<Dashboard>('GET', `/sql/dashboards/${encodeURIComponent(id)}`));
        } catch (e) {
            notifyError(e);
        }
    };
    const selection = state.selection ?? [];
    const select = (from: number, column: string, value: unknown) => {
        const same = selection.some((x) => x.from === from && x.column === column && x.value === value);
        setState({ ...state, selection: same ? selection.filter((x) => x.from !== from) : [...selection.filter((x) => x.from !== from), { column, value, from }] });
    };
    const drill = (i: number, path: unknown[]) => setState({
        ...state, drill: { ...(state.drill ?? {}), [String(i)]: path }, selection: selection.filter((x) => x.from !== i) });
    return (
        <>
            {(
                <Surface style={{ marginBottom: 12 }}>
                    <Space wrap data-testid="controls">
                        <Actions board={id} row={row} onDone={() => { setRow(null); setRefresh((r) => r + 1); }} />
                        <ScheduleExport board={id} name={board.name} />
                        <BiAssetButtons kind="dashboard" id={id} name={board.name} role={board.role}
                            tiles={board.tiles.map((t, i) => t.title || `Tile ${i + 1}`)}
                            onRestored={() => setRefresh((r) => r + 1)} />
                        <Button onClick={() => void copy()}>{board.builtin ? 'Copy to edit' : 'Copy'}</Button>
                        {ops && <Button onClick={() => void exportGrafana()}>Export to Grafana</Button>}
                        {ops && (board.role === 'OWNER' || board.role === 'EDITOR') && <Button onClick={() => void edit()}>Edit</Button>}
                        {(board.controls ?? []).filter((c) => c.kind !== 'time' && c.kind !== 'variable').map((c) => (
                            <Control key={c.id} board={id} control={c} value={state.controls?.[c.id] ?? c.default}
                                onChange={(v) => setControl(c.id, v)} />
                        ))}
                    </Space>
                    {ops && (
                        <Space wrap style={{ marginTop: 8 }} data-testid="ops-controls">
                            {(board.controls ?? []).filter((c) => c.kind === 'variable').map((c) => (
                                <Variable key={c.id} board={id} control={c} value={state.controls?.[c.id] ?? c.default}
                                    onChange={(v) => setControl(c.id, v)} />
                            ))}
                            <Select aria-label="Time range" style={{ width: 110 }} value={(state.controls?.time as string) ?? (timeControl?.default as string) ?? '1h'}
                                onChange={(v) => setControl(timeControl?.id ?? 'time', v)}
                                options={RANGES.map((r) => ({ value: r, label: `Last ${r}` }))} />
                            <Select aria-label="Auto-refresh" style={{ width: 120 }} value={every ?? timeControl?.refresh ?? 0} onChange={setEvery}
                                options={REFRESHES.map((r) => ({ ...r, label: r.value ? `Every ${r.label}` : 'No refresh' }))} />
                            <Button onClick={() => void q.refresh()}>Refresh now</Button>
                            {board.window && <Muted>{new Date(board.window.start_ms).toLocaleString()} – {new Date(board.window.end_ms).toLocaleTimeString()}</Muted>}
                        </Space>
                    )}
                    <Space wrap>
                        {editing && (
                            <OpsDashboardEditor open board={editing} onCancel={() => setEditing(null)}
                                onSaved={(did) => { setEditing(null); message.success('Saved'); onOpen?.(did); }} />
                        )}
                        {selection.map((x) => (
                            <Tag key={`${x.from}-${x.column}`} closable onClose={() => setState({ ...state, selection: selection.filter((y) => y !== x) })}>
                                {x.column} = {String(x.value)}
                            </Tag>
                        ))}
                        {q.loading && <Muted>Updating…</Muted>}
                    </Space>
                </Surface>
            )}
            <div data-testid="tiles" style={{ display: 'grid', gridTemplateColumns: wide ? 'repeat(2, minmax(0, 1fr))' : 'minmax(0, 1fr)', gap: 12 }}>
                {board.tiles.map((t, i) => {
                    const path = t.level?.path ?? [];
                    const mine = selection.find((x) => x.from === i);
                    const deeper = !!t.drill && t.drill.length > path.length + 1;
                    const x = t.chart?.x ?? '';
                    return (
                        <Surface key={i} style={{ gridColumn: t.width === 2 && wide ? 'span 2' : undefined, minWidth: 0 }}>
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
                            ) : t.ops ? (
                                <OpsTileView tile={{ ...t, ops: t.ops }} />
                            ) : t.result && t.chart && t.chart.type !== 'table' ? (
                                <ResultChart columns={t.result.columns} rows={t.result.rows} chart={t.chart} height={220}
                                    selected={mine?.value} onSelect={x ? (v) => select(i, x, v) : undefined} />
                            ) : t.result ? (
                                <Table
                                    size="small"
                                    rowKey="__row"
                                    rowClassName={(r: Record<string, unknown>) => (row && r._row_id === row._row_id ? 'ant-table-row-selected' : '')}
                                    onRow={(r: Record<string, unknown>) => ({ onClick: () => { if (r._row_id) setRow(r); } })}
                                    pagination={{ pageSize: 10, hideOnSinglePage: true }}
                                    scroll={{ x: 'max-content' }}
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

/** A Grafana dashboard's JSON (pasted or from a file) as a dashboard of yours: PromQL, LogQL and TraceQL panels. */
function GrafanaImport({ open, onClose, onImported }: { open: boolean; onClose: () => void; onImported: (id: string) => void }) {
    const { client } = useConsumerApi();
    const [text, setText] = useState('');
    const [busy, setBusy] = useState(false);
    const [skipped, setSkipped] = useState<{ panel?: string; variable?: string; reason: string }[] | null>(null);
    const [made, setMade] = useState<string | null>(null);
    const go = async () => {
        let doc: unknown;
        try {
            doc = JSON.parse(text);
        } catch {
            message.error('That is not JSON: paste the dashboard’s JSON model (Dashboard settings → JSON Model in Grafana)');
            return;
        }
        setBusy(true);
        try {
            const out = await client.request<{ dashboard: Dashboard; skipped: { panel?: string; variable?: string; reason: string }[] }>(
                'POST', '/sql/dashboards/import/grafana', doc);
            if (out.skipped.length) {
                setSkipped(out.skipped);
                setMade(out.dashboard.id);
            } else {
                setText('');
                onImported(out.dashboard.id);
            }
        } catch (e) {
            notifyError(e, { action: 'Import from Grafana' });
        } finally {
            setBusy(false);
        }
    };
    const file = async (f: File | undefined) => { if (f) setText(await f.text()); };
    const close = () => { setSkipped(null); setMade(null); onClose(); };
    return (
        <Modal title="Import a Grafana dashboard" open={open} onCancel={close} width={720} confirmLoading={busy}
            okText={made ? 'Open it' : 'Import'} okButtonProps={{ disabled: !made && !text.trim() }}
            onOk={() => (made ? (setSkipped(null), setMade(null), setText(''), onImported(made)) : void go())}>
            <Space direction="vertical" style={{ width: '100%' }}>
                <Muted>Panels on Prometheus, Loki and Tempo (TraceQL) become tiles; query and custom variables, the time range and the refresh come too. Queries run through Cairn, as each viewer.</Muted>
                <input type="file" accept=".json,application/json" aria-label="Grafana dashboard file" onChange={(e) => void file(e.target.files?.[0])} />
                <Input.TextArea aria-label="Grafana dashboard JSON" rows={8} value={text} onChange={(e) => setText(e.target.value)}
                    placeholder='{"title": "...", "panels": [...]}' style={{ fontFamily: 'ui-monospace, monospace' }} />
                {skipped && (
                    <Alert type="info" showIcon message={`Imported; ${skipped.length} left out:`}
                        description={<ul style={{ margin: 0 }}>{skipped.map((s, i) => <li key={i}>{s.panel ?? (s.variable ? `$${s.variable}` : 'a panel')}: {s.reason}</li>)}</ul>} />
                )}
            </Space>
        </Modal>
    );
}

function Dashboards({ saved, openId, newOps, onNewOpsClosed }: { saved: SavedQuery[]; openId?: string | null; newOps?: boolean;
    onNewOpsClosed?: () => void }) {
    const { client } = useConsumerApi();
    const t = useT();
    const [tick, setTick] = useState(0);
    const list = useDheQuery<Dashboard[]>(`/sql/dashboards#${tick}`, (c, signal) => c.dashboards({ signal }));
    const books = useDheQuery<Workbook[]>('/sql/workbooks', (c, signal) => c.workbooks({ signal }));
    const metricList = useDheQuery<Metric[]>('/sql/metrics', (c, signal) => c.metrics({ signal }));
    const [open, setOpen] = useState<string | null>(openId ?? null);
    useEffect(() => { if (openId) setOpen(openId); }, [openId]);
    const [opsNew, setOpsNew] = useState(!!newOps);
    useEffect(() => { if (newOps) setOpsNew(true); }, [newOps]);
    const [importing, setImporting] = useState(false);
    const opened = (id: string) => { setTick((t) => t + 1); setOpen(id); };
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
                     chart: x && y.length ? { type: form.chart, x, y, ...(form.chart === 'heatmap' ? { series: el.group_by?.[1] } : {}) }
                         : { type: 'table' as const } };
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
            notifyError(e);
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
            notifyError(e);
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
                    onChange={setOpen} options={(list.data ?? []).map((d) => ({ value: d.id,
                        label: d.builtin ? `${d.name} (built-in)` : d.shared ? `${d.name} (shared)` : d.name }))} />
                <Button onClick={() => { setBuilt(null); setAsking(true); }}>Build from a question</Button>
                <Button onClick={() => setEditing(true)} disabled={!saved.length && !(books.data ?? []).length && !(metricList.data ?? []).length}>New dashboard</Button>
                <Button onClick={() => setOpsNew(true)}>New ops dashboard</Button>
                <Button onClick={() => setImporting(true)}>Import from Grafana</Button>
                {!saved.length && !(books.data ?? []).length && <Muted>Save queries in SQL, or workbooks, first; a dashboard shows them.</Muted>}
            </Space>
            {open ? <Board key={`${open}-${tick}`} id={open} onOpen={opened} /> : (
                // No blank page (L150): the dashboards, most recently changed first, as cards.
                (list.data ?? []).length ? (
                    <section aria-labelledby="dash-recent">
                        <h2 id="dash-recent" style={{ fontSize: 16, margin: '4px 0 12px' }}>{t('page.recentDashboards')}</h2>
                        <DashCards>
                            {[...(list.data ?? [])].sort((a, b) => (b.updated_ms ?? 0) - (a.updated_ms ?? 0)).slice(0, 12).map((d) => (
                                <button key={d.id} type="button" onClick={() => setOpen(d.id)}>
                                    <strong>{d.name}</strong>
                                    {d.description && <span className="desc">{d.description}</span>}
                                    <span className="meta">
                                        {d.tiles.length} tile{d.tiles.length === 1 ? '' : 's'}
                                        {d.builtin ? ' · built-in' : d.shared ? ' · shared' : ''}
                                        {d.updated_ms ? <> · <When value={d.updated_ms} /></> : null}
                                    </span>
                                </button>
                            ))}
                        </DashCards>
                    </section>
                ) : list.loading ? null : <Empty description="No dashboards yet: build one from a question, or from saved queries" />
            )}
            {opsNew && (
                <OpsDashboardEditor open onCancel={() => { setOpsNew(false); onNewOpsClosed?.(); }}
                    onSaved={(id) => { setOpsNew(false); onNewOpsClosed?.(); message.success('Ops dashboard created'); opened(id); }} />
            )}
            <GrafanaImport open={importing} onClose={() => setImporting(false)} onImported={(id) => { setImporting(false); opened(id); }} />
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
                                options={CHART_TYPES.filter((t) => t !== 'map').map((t) => ({ value: t, label: t.replace('_', ' ') }))} />
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

export function Alerts({ saved }: { saved: SavedQuery[] }) {
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
            notifyError(e);
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
                            <span title={a.last_error ?? ''}>
                                <StatusBadge state={a.state} />
                                {a.last_value !== null ? <Muted>{` (${a.last_value})`}</Muted> : ''}
                            </span>
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
const DashCards = styled.div`
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
    gap: 12px;
    button {
        display: flex;
        flex-direction: column;
        align-items: flex-start;
        gap: 4px;
        min-height: 96px;
        padding: 14px 16px;
        text-align: left;
        border: 1px solid ${tokens.color.border};
        border-radius: ${tokens.radius.md};
        background: ${tokens.color.surface};
        box-shadow: ${tokens.shadow.card};
        color: ${tokens.color.text};
        font: inherit;
        cursor: pointer;
        ${focusRing}
        &:hover {
            border-color: ${tokens.color.primary};
        }
        .desc {
            color: ${tokens.color.textMuted};
            font-size: 13px;
            display: -webkit-box;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
            overflow: hidden;
        }
        .meta {
            margin-top: auto;
            color: ${tokens.color.textMuted};
            font-size: 12px;
        }
    }
`;

export function DashboardsPage() {
    const saved = useDheQuery<SavedQuery[]>('/sql/saved', (c, signal) => c.savedQueries({ signal }));
    // A link from a folder or the usage list opens its dashboard (`/dashboards?open=<id>`).
    // `?new=ops` (the command palette's "New ops dashboard") opens the ops dashboard editor.
    const location = useLocation();
    const history = useHistory();
    const params = new URLSearchParams(location.search);
    const openId = params.get('open');
    const newOps = params.get('new') === 'ops';
    const [tab, setTab] = useState(params.get('tab') === 'exports' || params.get('tab') === 'folders' || params.get('tab') === 'usage'
        ? (params.get('tab') as string) : 'boards');
    useEffect(() => { if (openId || newOps) setTab('boards'); }, [openId, newOps]);
    const clearNew = () => {
        params.delete('new');
        history.replace({ pathname: location.pathname, search: params.toString() ? `?${params.toString()}` : '' });
    };
    return (
        <Page>
            <PageHeader title="Dashboards" help={pageHelp('/dashboards')}
                description="Charts of saved queries and workbooks, and ops dashboards of metrics, logs, traces, SLOs and alerts. Every tile and export runs as a person, under their policies; alerts on queries are in Alerts & routing." />
            <Tabs style={{ marginTop: 12 }} activeKey={tab} onChange={setTab} items={[
                { key: 'boards', label: 'Dashboards', children: <Dashboards saved={saved.data ?? []} openId={openId} newOps={newOps} onNewOpsClosed={clearNew} /> },
                { key: 'folders', label: 'Folders', children: <BiFolders /> },
                { key: 'exports', label: 'Scheduled exports', children: <ResourceView spec={RESOURCE_SPECS['scheduled-exports']} /> },
                { key: 'usage', label: 'Usage', children: <BiUsageOverview /> },
            ]} />
        </Page>
    );
}
