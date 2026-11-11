import { Alert, Button, Descriptions, Drawer, Empty, Input, List, Popconfirm, Space, Table, Tag, message } from 'antd';
import React, { useState } from 'react';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import { MessageKey, useT } from '../i18n';
import { RESOURCE_SPECS } from '../resources/catalog';
import { ActionDialog } from '../resources/ResourceView';
import type { ResourceAction, ResourceSpec } from '../resources/types';
import { ErrorNotice, notifyError } from './ErrorNotice';
import { StatusBadge, StatusTone, useStatusLabel } from './StatusBadge';
import { Muted, SectionTitle, Surface, tokens } from './tokens';
import { When } from '../utils/time';
import { RowActions } from './RowActions';

// ---- what the API answers (cairn.observability.pipelines) ---------------------------------------------------------
export interface PipelineTask {
    task: string;
    state: string;
    airflow_state?: string;
    try?: number;
    start_ms?: number | null;
    end_ms?: number | null;
    offset_ms: number | null;
    duration_ms: number | null;
    error?: string | null;
}
export interface PipelineRun {
    id: string;
    pipeline: string;
    source: string;
    run_id: string;
    ol_run_id: string | null;
    state: string;
    start: string | null;
    end: string | null;
    start_ms: number | null;
    duration_ms: number | null;
    trace_id: string | null;
    error: string | null;
    inputs: string[];
    outputs: string[];
    tasks: PipelineTask[];
    anomaly: { duration_ms: number; mean_ms: number } | null;
}
interface TrendPoint { run_id: string; state: string; duration_ms: number | null; start: string | null }
export interface PipelineSummary {
    key: string;
    source: string;
    name: string;
    health: 'healthy' | 'failing' | 'sla_missed' | 'anomalous' | 'unknown';
    runs: number;
    success_rate: number | null;
    p50_ms: number | null;
    p95_ms: number | null;
    trend: TrendPoint[];
    anomalies: number;
    last_run: PipelineRun | null;
    slas: string[];
    data_products: string[];
}
interface RunDetail extends PipelineRun {
    airflow_trace_id: string | null;
    impact: {
        tables: { urn: string; name: string; hops: number }[];
        assets: { kind: string; id: string; name: string }[];
        data_products: { id: string; name: string; tables: string[] }[];
        pipelines: { pipeline: string; tables: string[] }[];
    };
    quality: { table: string; name: string; status: string; message: string | null; at: string }[];
}
interface Diagnosis {
    causes: { kind: string; text: string; likelihood: number }[];
    suggestions: { kind: string; text?: string; action?: string; rationale?: string }[];
    log: { task: string | null; tail: string[] };
}
interface Product { id: string; name: string; tables: string[]; pipelines: string[]; description?: string }
interface ProductStatus extends Product {
    health: string;
    table_status: { urn: string; name: string; last_update: string | null; fresh: boolean | null }[];
}
interface Sla {
    id: string; name: string; pipeline: string; data_product: string; deadline: string; timezone: string;
    max_duration: string; freshness: string; on_failure: boolean; anomalies: boolean; severity: string;
    last_evaluation?: { error?: string; missed?: number } | null;
}
interface Exporter { id: string; name: string; protocol: string; endpoint: string; signals: string[]; enabled: boolean;
    headers: { name: string; scope: string; key: string }[] }

const HEALTH_TONE: Record<string, StatusTone> = { healthy: 'ok', failing: 'error', sla_missed: 'warn', anomalous: 'warn', unknown: 'waiting' };
const STATE_COLOR: Record<string, string> = { success: '#16A34A', failed: '#DC2626', running: '#2563EB', queued: '#64748B', skipped: '#64748B' };
const SPEC = (k: string) => RESOURCE_SPECS[k] as ResourceSpec;
const action = (k: string, label: string) => SPEC(k).actions!.find((a) => a.label === label) as ResourceAction;
const enc = encodeURIComponent;

/** A duration for people: 850 ms, 12.5 s, 4 min 05 s, 1 h 02 min. */
export function duration(ms: number | null | undefined): string {
    if (ms === null || ms === undefined) return '–';
    if (ms < 1000) return `${Math.round(ms)} ms`;
    const s = ms / 1000;
    if (s < 60) return `${Number(s.toFixed(1))} s`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m} min ${String(Math.round(s % 60)).padStart(2, '0')} s`;
    return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, '0')} min`;
}

/** A run's tasks as Gantt bars (left and width in % of the run). */
export function gantt(run: Pick<PipelineRun, 'tasks' | 'duration_ms'>): (PipelineTask & { left: number; width: number })[] {
    const total = Math.max(1, run.duration_ms ?? 0, ...run.tasks.map((t) => (t.offset_ms ?? 0) + (t.duration_ms ?? 0)));
    return run.tasks.map((t) => ({ ...t, left: (100 * Math.max(0, t.offset_ms ?? 0)) / total,
        width: Math.max(0.6, (100 * (t.duration_ms ?? 0)) / total) }));
}

function Sparkline({ p }: { p: PipelineSummary }) {
    const t = useT();
    const pts = p.trend.slice(-20);
    if (!pts.length) return <Muted>–</Muted>;
    const max = Math.max(1, ...pts.map((x) => x.duration_ms ?? 0));
    return (
        <svg width={pts.length * 7} height={24} role="img" aria-label={t('pipe.trend', { n: pts.length, name: p.key })}>
            {pts.map((x, i) => {
                const h = Math.max(2, (22 * (x.duration_ms ?? 0)) / max);
                return <rect key={x.run_id} x={i * 7} y={23 - h} width={5} height={h} fill={STATE_COLOR[x.state] ?? '#8c959f'} />;
            })}
        </svg>
    );
}

/** A task's bar: the state's colour, and a pattern for failed (stripes) and skipped (hollow), so it reads without colour. */
function barStyle(state: string): React.CSSProperties {
    const color = STATE_COLOR[state] ?? tokens.color.waiting;
    if (state === 'failed') {
        return { background: `repeating-linear-gradient(135deg, ${color} 0 6px, #B91C1C 6px 9px)` };
    }
    if (state === 'skipped' || state === 'upstream_failed') {
        return { background: 'transparent', border: `2px dashed ${tokens.color.waiting}`, boxSizing: 'border-box' };
    }
    return { background: color };
}

function Gantt({ run }: { run: PipelineRun }) {
    const t = useT();
    const words = useStatusLabel();
    return (
        <ol aria-label={t('pipe.gantt', { id: run.run_id })} style={{ listStyle: 'none', padding: 0, margin: 0, display: 'grid', gap: 6 }}>
            {gantt(run).map((x) => {
                const marked = x.state !== 'success';
                return (
                    <li key={x.task} style={{ display: 'grid', gridTemplateColumns: 'minmax(120px, 30%) minmax(0, 1fr) minmax(96px, auto)',
                        gap: 12, alignItems: 'center', fontSize: 13 }}
                        aria-label={t('pipe.task', { task: x.task, state: x.state, ms: duration(x.duration_ms) })}>
                        <span style={{ fontFamily: tokens.fontMono, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                            {x.task}{(x.try ?? 0) > 1 && <Muted> · try {x.try}</Muted>}
                        </span>
                        <span style={{ position: 'relative', height: 18, background: '#F1F5F9', borderRadius: 4 }} aria-hidden>
                            <span style={{ position: 'absolute', left: `${x.left}%`, width: `${x.width}%`, top: 0, bottom: 0,
                                borderRadius: 4, ...barStyle(x.state) }} />
                        </span>
                        <span style={{ textAlign: 'right', color: tokens.color.textMuted, whiteSpace: 'nowrap' }}>
                            {marked ? <StatusBadge state={x.state} plain label={x.duration_ms
                                ? `${words(x.state)} · ${duration(x.duration_ms)}` : words(x.state)} /> : duration(x.duration_ms)}
                        </span>
                    </li>
                );
            })}
        </ol>
    );
}

/** A failed run's most likely cause, above its timeline: what the diagnosis found first, and what it suggests. */
function LikelyCause({ diag }: { diag: Diagnosis | null }) {
    const t = useT();
    if (!diag) return null;
    const [first, ...rest] = diag.causes;
    const hint = diag.suggestions[0];
    return (
        <section aria-labelledby="pipe-likely" style={{ background: tokens.color.primarySoft, border: '1px solid #BFDBFE',
            borderRadius: tokens.radius.lg, padding: 16, display: 'grid', gap: 8 }}>
            <div style={{ display: 'flex', flexWrap: 'wrap', justifyContent: 'space-between', gap: 8, alignItems: 'center' }}>
                <h3 id="pipe-likely" style={{ margin: 0, fontSize: 16 }}>{t('pipe.likelyCause')}</h3>
                <span style={{ fontSize: 12, fontWeight: 600, color: tokens.color.primaryHover }}>{t('pipe.likelyCauseFrom')}</span>
            </div>
            {first ? (
                <p style={{ margin: 0, fontSize: 15, lineHeight: 1.5 }}>
                    <Tag>{first.kind.replace(/_/g, ' ')}</Tag>{first.text}
                    {rest.length > 0 && <Muted> · {rest.map((c) => c.text).join(' · ')}</Muted>}
                </p>
            ) : <p style={{ margin: 0 }}>{t('pipe.likelyCauseNone')}</p>}
            {hint && (hint.text ?? hint.rationale) && (
                <p style={{ margin: 0, fontSize: 13, color: tokens.color.textSubtle }}>→ {hint.text ?? hint.rationale}</p>
            )}
        </section>
    );
}

function TaskLog({ pipeline, run, task }: { pipeline: string; run: string; task: string }) {
    const t = useT();
    const q = useDheQuery<{ lines: string[]; source: string | null }>(`log:${pipeline}:${run}:${task}`, (c, signal) =>
        c.request('GET', `/v1/observability/pipelines/${enc(pipeline)}/runs/${enc(run)}/tasks/${enc(task)}/log`, undefined, { signal }));
    if (q.error) return <ErrorNotice error={q.error} area={t('pipe.log', { task })} />;
    return (
        <section aria-label={t('pipe.log', { task })}>
            <pre tabIndex={0} style={{ maxHeight: 320, overflow: 'auto', fontSize: 12, background: tokens.color.surfaceAlt, padding: 8, whiteSpace: 'pre-wrap' }}>
                {q.data ? (q.data.lines.length ? q.data.lines.join('\n') : t('pipe.logEmpty')) : '…'}
            </pre>
        </section>
    );
}

function RunView({ pipeline, runId, openTrace }: { pipeline: string; runId: string; openTrace: (id: string) => void }) {
    const t = useT();
    const { client } = useConsumerApi();
    const q = useDheQuery<RunDetail>(`run:${pipeline}:${runId}`, (c, signal) =>
        c.request('GET', `/v1/observability/pipelines/${enc(pipeline)}/runs/${enc(runId)}`, undefined, { signal }));
    const [log, setLog] = useState<string | null>(null);
    const [diag, setDiag] = useState<Diagnosis | null>(null);
    const failed = q.data?.state === 'failed';
    // A failed run says its likely cause first, without a click (the button below shows the evidence and the log).
    const auto = useDheQuery<Diagnosis | null>(`diagnose:${pipeline}:${runId}:${failed ? 1 : 0}`, (c, signal) => (failed
        ? c.request<Diagnosis>('GET', `/v1/observability/pipelines/${enc(pipeline)}/runs/${enc(runId)}/diagnose`, undefined, { signal })
        : Promise.resolve(null)));
    if (q.error) return <ErrorNotice error={q.error} area={t('pipe.run', { id: runId })} />;
    const r = q.data;
    if (!r) return null;
    const failedTask = r.tasks.find((x) => x.state === 'failed');
    const diagnose = async () => {
        try {
            setDiag(await client.request<Diagnosis>('GET', `/v1/observability/pipelines/${enc(pipeline)}/runs/${enc(runId)}/diagnose`));
        } catch (e) { notifyError(e); }
    };
    const rerun = async () => {
        try { await client.request('POST', `/v1/observability/pipelines/${enc(pipeline)}/runs/${enc(runId)}/rerun`); message.success(t('pipe.rerunDone')); q.refresh(); }
        catch (e) { notifyError(e); }
    };
    return (
        <Space direction="vertical" style={{ width: '100%' }} size="middle">
            <Descriptions size="small" column={3} bordered>
                <Descriptions.Item label={t('pipe.run.state')}><StatusBadge state={r.state} /></Descriptions.Item>
                <Descriptions.Item label={t('pipe.run.started')}><When value={r.start} empty="–" /></Descriptions.Item>
                <Descriptions.Item label={t('pipe.run.duration')}>{duration(r.duration_ms)}</Descriptions.Item>
            </Descriptions>
            {r.anomaly && <Alert type="warning" showIcon message={t('pipe.run.anomaly', { ms: duration(r.anomaly.duration_ms), mean: duration(r.anomaly.mean_ms) })} />}
            {r.error && <Alert type="error" showIcon message={r.error} />}
            <Space wrap>
                {r.trace_id && <Button onClick={() => openTrace(r.trace_id!)}>{t('pipe.trace')}</Button>}
                {r.airflow_trace_id && <Button onClick={() => openTrace(r.airflow_trace_id!)}>{t('pipe.airflowTrace')}</Button>}
                {r.state === 'failed' && <Button onClick={diagnose}>{t('pipe.diagnose')}</Button>}
                {r.state === 'failed' && (
                    <Popconfirm title={t('pipe.rerunConfirm')} onConfirm={rerun}><Button danger>{t('pipe.rerun')}</Button></Popconfirm>
                )}
            </Space>
            {failed && <LikelyCause diag={diag ?? auto.data ?? null} />}
            <Gantt run={r} />
            <Space wrap>
                {r.tasks.map((x) => (
                    <Button key={x.task} size="small" type={log === x.task ? 'primary' : 'default'} danger={x.state === 'failed'}
                        onClick={() => setLog(log === x.task ? null : x.task)}>{t('pipe.log', { task: x.task })}</Button>
                ))}
            </Space>
            {(log ?? (diag ? failedTask?.task : null)) && <TaskLog pipeline={pipeline} run={runId} task={(log ?? failedTask?.task)!} />}
            {diag && (
                <section aria-label={t('pipe.causes')}>
                    <SectionTitle>{t('pipe.causes')}</SectionTitle>
                    <List size="small" bordered dataSource={diag.causes} locale={{ emptyText: '–' }}
                        renderItem={(c) => <List.Item><Tag>{c.kind}</Tag>{c.text} <Muted>({Math.round(c.likelihood * 100)}%)</Muted></List.Item>} />
                    {diag.suggestions.map((s, i) => <div key={i}><Muted>→ {s.text ?? s.rationale}</Muted></div>)}
                </section>
            )}
            <section aria-label={t('pipe.impact')}>
                <SectionTitle>{t('pipe.impact')}</SectionTitle>
                {r.impact.tables.length === 0 ? <Muted>{t('pipe.impact.none')}</Muted> : (
                    <Descriptions size="small" column={1} bordered>
                        <Descriptions.Item label={t('pipe.impact.tables')}>
                            {r.impact.tables.map((x) => <Tag key={x.urn} color={x.hops ? 'orange' : 'blue'}>{x.name}</Tag>)}
                        </Descriptions.Item>
                        <Descriptions.Item label={t('pipe.impact.assets')}>
                            {r.impact.assets.length ? r.impact.assets.map((a) => <Tag key={`${a.kind}:${a.id}`}>{a.kind}: {a.name}</Tag>) : '–'}
                        </Descriptions.Item>
                        <Descriptions.Item label={t('pipe.impact.products')}>
                            {r.impact.data_products.length ? r.impact.data_products.map((p) => <Tag key={p.id} color="purple">{p.name}</Tag>) : '–'}
                        </Descriptions.Item>
                        <Descriptions.Item label={t('pipe.impact.pipelines')}>
                            {r.impact.pipelines.length ? r.impact.pipelines.map((p) => <Tag key={p.pipeline}>{p.pipeline}</Tag>) : '–'}
                        </Descriptions.Item>
                    </Descriptions>
                )}
            </section>
            <section aria-label={t('pipe.quality')}>
                <SectionTitle>{t('pipe.quality')}</SectionTitle>
                {r.quality.length === 0 ? <Muted>{t('pipe.quality.none')}</Muted> : (
                    <List size="small" bordered dataSource={r.quality} renderItem={(x) => (
                        <List.Item><StatusBadge state={x.status} label={x.status} style={{ marginRight: 8 }} />{x.name}
                            <Muted> · {x.table.replace(/^urn:li:dataset:\([^,]+,([^,]+),.*$/, '$1')}{x.message ? ` · ${x.message}` : ''}</Muted></List.Item>
                    )} />
                )}
            </section>
        </Space>
    );
}

function PipelineView({ pipeline, run, setRun, openTrace }: { pipeline: string; run: string; setRun: (id: string | null) => void;
    openTrace: (id: string) => void }) {
    const t = useT();
    const q = useDheQuery<PipelineSummary & { items: PipelineRun[] }>(`pipeline:${pipeline}`, (c, signal) =>
        c.request('GET', `/v1/observability/pipelines/${enc(pipeline)}?limit=50`, undefined, { signal }), { pollMs: 30_000 });
    if (q.error) return <ErrorNotice error={q.error} area={pipeline} />;
    const p = q.data;
    const chosen = run || p?.items[0]?.run_id || '';
    return (
        <Space direction="vertical" style={{ width: '100%' }} size="large">
            <section aria-label={t('pipe.runs', { name: pipeline })}>
                <SectionTitle>{t('pipe.runs', { name: pipeline })}</SectionTitle>
                <Table size="small" rowKey="run_id" loading={q.loading} dataSource={p?.items ?? []} pagination={{ pageSize: 10 }}
                    rowClassName={(r) => (r.run_id === chosen ? 'ant-table-row-selected' : '')}
                    columns={[
                        { title: t('pipe.run', { id: '' }).trim(), dataIndex: 'run_id',
                            render: (v: string) => <Button type="link" size="small" onClick={() => setRun(v)}>{v}</Button> },
                        { title: t('pipe.run.state'), dataIndex: 'state', render: (v: string, r) => (
                            <Space size={4}><StatusBadge state={v} />
                                {r.anomaly && <Tag color="gold">{t('pipe.health.anomalous')}</Tag>}</Space>) },
                        { title: t('pipe.run.started'), dataIndex: 'start', render: (v: string | null) => <When value={v} empty={'–'} /> },
                        { title: t('pipe.run.duration'), dataIndex: 'duration_ms', render: (v: number | null) => duration(v) },
                    ]} />
            </section>
            {chosen && <Surface><SectionTitle>{t('pipe.run', { id: chosen })}</SectionTitle>
                <RunView key={chosen} pipeline={pipeline} runId={chosen} openTrace={openTrace} /></Surface>}
        </Space>
    );
}

function ProductStatusView({ id, name }: { id: string; name: string }) {
    const t = useT();
    const q = useDheQuery<ProductStatus>(`product:${id}`, (c, signal) =>
        c.request('GET', `/v1/observability/data-products/${enc(id)}/status`, undefined, { signal }));
    if (q.error) return <ErrorNotice error={q.error} area={name} />;
    const s = q.data;
    if (!s) return null;
    return (
        <section aria-label={t('pipe.product.status', { name })}>
            <StatusBadge tone={HEALTH_TONE[s.health] ?? 'waiting'} label={t(`pipe.health.${s.health}` as MessageKey)} />
            <List size="small" dataSource={s.table_status} renderItem={(x) => (
                <List.Item>{x.name}{' '}
                    {x.fresh !== null && <StatusBadge tone={x.fresh ? 'ok' : 'error'} label={t(x.fresh ? 'pipe.product.fresh' : 'pipe.product.stale')} />}
                    <Muted><When value={x.last_update} empty="–" /></Muted></List.Item>
            )} />
        </section>
    );
}

function Definitions() {
    const t = useT();
    const { client } = useConsumerApi();
    const products = useDheQuery<{ items: Product[] }>('/v1/observability/data-products', (c, signal) =>
        c.request('GET', '/v1/observability/data-products', undefined, { signal }));
    const slas = useDheQuery<{ items: Sla[] }>('/v1/observability/pipeline-slas', (c, signal) =>
        c.request('GET', '/v1/observability/pipeline-slas', undefined, { signal }));
    const [dialog, setDialog] = useState<{ spec: string; label: string; row?: Record<string, unknown> } | null>(null);
    const del = async (path: string, done: () => void) => {
        try { await client.request('DELETE', path); done(); } catch (e) { notifyError(e); }
    };
    return (
        <Space direction="vertical" style={{ width: '100%' }} size="large">
            <section aria-label={t('pipe.products')}>
                <SectionTitle>{t('pipe.products')}</SectionTitle>
                <Button onClick={() => setDialog({ spec: 'observability-data-products', label: 'New data product' })}>{t('pipe.product.new')}</Button>
                {products.error && <ErrorNotice error={products.error} area={t('pipe.products')} />}
                <Table size="small" rowKey="id" dataSource={products.data?.items ?? []} pagination={false} loading={products.loading}
                    expandable={{ expandedRowRender: (r) => <ProductStatusView id={r.id} name={r.name} /> }}
                    columns={[
                        { title: t('pipe.products'), dataIndex: 'name' },
                        { title: t('pipe.product.tables'), dataIndex: 'tables', render: (v: string[]) => v.length },
                        { title: t('pipe.list'), dataIndex: 'pipelines', render: (v: string[]) => v.join(', ') },
                        { title: '', key: 'act', render: (_: unknown, r) => (
                            <Space>
                                <Button size="small" onClick={() => setDialog({ spec: 'observability-data-products', label: 'Edit data product',
                                    row: r as unknown as Record<string, unknown> })}>{t('pipe.product.edit')}</Button>
                                <RowActions name={r.name || r.id} actions={[{ key: 'delete', label: t('pipe.product.delete'), danger: true, confirm: `${t('pipe.product.delete')} ${r.name}?`,
    onSelect: () => del(`/v1/observability/data-products/${enc(r.id)}`, products.refresh) }]} />
                            </Space>) },
                    ]} />
            </section>
            <section aria-label={t('pipe.slas')}>
                <SectionTitle>{t('pipe.slas')}</SectionTitle>
                <Button onClick={() => setDialog({ spec: 'observability-pipeline-slas', label: 'New SLA' })}>{t('pipe.sla.new')}</Button>
                {slas.error && <ErrorNotice error={slas.error} area={t('pipe.slas')} />}
                <Table size="small" rowKey="id" dataSource={slas.data?.items ?? []} pagination={false} loading={slas.loading}
                    columns={[
                        { title: 'SLA', dataIndex: 'name' },
                        { title: t('pipe.sla.about'), key: 'about', render: (_: unknown, r) => r.pipeline || r.data_product },
                        { title: t('pipe.sla.checks'), key: 'checks', render: (_: unknown, r) => [
                            r.on_failure && 'failure', r.deadline && `by ${r.deadline} ${r.timezone}`, r.max_duration && `≤ ${r.max_duration}`,
                            r.freshness && `fresh ${r.freshness}`, r.anomalies && 'anomalies'].filter(Boolean).join(' · ') },
                        { title: '', dataIndex: 'severity', render: (v: string) => <Tag color={v === 'critical' ? 'red' : v === 'warning' ? 'orange' : 'blue'}>{v}</Tag> },
                        { title: '', key: 'act', render: (_: unknown, r) => (
                            <Space>
                                <Button size="small" onClick={() => setDialog({ spec: 'observability-pipeline-slas', label: 'Edit SLA',
                                    row: r as unknown as Record<string, unknown> })}>{t('pipe.sla.edit')}</Button>
                                <RowActions name={r.name || r.id} actions={[{ key: 'delete', label: t('pipe.sla.delete'), danger: true, confirm: `${t('pipe.sla.delete')} ${r.name}?`,
    onSelect: () => del(`/v1/observability/pipeline-slas/${enc(r.id)}`, slas.refresh) }]} />
                            </Space>) },
                    ]} />
            </section>
            {dialog && (
                <ActionDialog spec={SPEC(dialog.spec)} action={action(dialog.spec, dialog.label)} row={dialog.row ?? null}
                    onDone={() => { setDialog(null); products.refresh(); slas.refresh(); }} onClose={() => setDialog(null)} />
            )}
        </Space>
    );
}

/** Observability → Pipelines: every pipeline's health, its runs as a timeline, a run's tasks (Gantt), trace, logs,
 * what it reaches and the quality of what it wrote; data products and SLAs. */
export function PipelinesTab({ pipeline, run, select, openTrace }: { pipeline: string; run: string;
    select: (pipeline: string | null, run?: string | null) => void; openTrace: (id: string) => void }) {
    const t = useT();
    const { client } = useConsumerApi();
    const [filter, setFilter] = useState('');
    const q = useDheQuery<{ items: PipelineSummary[] }>('/v1/observability/pipelines', (c, signal) =>
        c.request('GET', '/v1/observability/pipelines', undefined, { signal }), { pollMs: 30_000 });
    const sync = async () => {
        try {
            const r = await client.request<{ runs: number; airflows: number }>('POST', '/v1/observability/pipelines/sync');
            message.success(t('pipe.synced', { runs: r.runs, airflows: r.airflows }));
            q.refresh();
        } catch (e) { notifyError(e); }
    };
    const rows = (q.data?.items ?? []).filter((p) => !filter || p.key.toLowerCase().includes(filter.toLowerCase()));
    return (
        <Space direction="vertical" style={{ width: '100%' }} size="large">
            <section aria-label={t('pipe.list')}>
                <Space wrap style={{ marginBottom: 8 }}>
                    <Input aria-label={t('pipe.filter')} placeholder={t('pipe.filter')} value={filter} style={{ width: 260 }}
                        onChange={(e) => setFilter(e.target.value)} />
                    <Button onClick={sync}>{t('pipe.sync')}</Button>
                </Space>
                {q.error && <ErrorNotice error={q.error} area={t('pipe.list')} />}
                {q.data && rows.length === 0 && !filter ? <Empty description={t('pipe.none')} /> : (
                    <Table size="small" rowKey="key" loading={q.loading} dataSource={rows} pagination={{ pageSize: 20 }}
                        columns={[
                            { title: t('pipe.col.pipeline'), dataIndex: 'key', render: (v: string, p) => (
                                <Button type="link" size="small" onClick={() => select(v, null)} aria-label={`Open ${v}`}>
                                    <Tag>{p.source}</Tag>{p.name}</Button>) },
                            { title: t('pipe.col.health'), dataIndex: 'health',
                                render: (v: string) => <StatusBadge tone={HEALTH_TONE[v] ?? 'waiting'} label={t(`pipe.health.${v}` as MessageKey)} /> },
                            { title: t('pipe.col.success'), dataIndex: 'success_rate',
                                render: (v: number | null) => (v === null ? '–' : `${Number((v * 100).toFixed(1))}%`) },
                            { title: t('pipe.col.p50'), dataIndex: 'p50_ms', render: (v: number | null) => duration(v) },
                            { title: t('pipe.col.p95'), dataIndex: 'p95_ms', render: (v: number | null) => duration(v) },
                            { title: t('pipe.col.trend'), key: 'trend', render: (_: unknown, p) => <Sparkline p={p} /> },
                            { title: t('pipe.col.last'), key: 'last', render: (_: unknown, p) => (p.last_run
                                ? <span><StatusBadge state={p.last_run.state} plain /> <Muted>{p.last_run.start ? new Date(p.last_run.start).toLocaleString() : ''}</Muted></span> : '–') },
                            { title: t('pipe.col.slas'), dataIndex: 'slas', render: (v: string[]) => v.map((s) => <Tag key={s}>{s}</Tag>) },
                        ]} />
                )}
            </section>
            <Definitions />
            <Drawer open={!!pipeline} width={Math.min(960, typeof window !== 'undefined' ? window.innerWidth - 40 : 960)}
                title={pipeline} onClose={() => select(null)} destroyOnClose>
                {pipeline && <PipelineView pipeline={pipeline} run={run} setRun={(r) => select(pipeline, r)} openTrace={openTrace} />}
            </Drawer>
        </Space>
    );
}

/** Observability → Export: the OTLP destinations, the Collector configuration rendered from them, applying it. */
export function ExportTab() {
    const t = useT();
    const { client } = useConsumerApi();
    const q = useDheQuery<{ items: Exporter[] }>('/v1/observability/exporters', (c, signal) =>
        c.request('GET', '/v1/observability/exporters', undefined, { signal }));
    const [dialog, setDialog] = useState<{ label: string; row?: Record<string, unknown> } | null>(null);
    const [config, setConfig] = useState<{ yaml: string; env: string[] } | null>(null);
    const showConfig = async () => {
        try { setConfig(await client.request('GET', '/v1/observability/exporters/collector-config')); } catch (e) { notifyError(e); }
    };
    const apply = async () => {
        try {
            const r = await client.request<{ written: string[] }>('POST', '/v1/observability/exporters/apply');
            message.success(t('export.applied', { files: r.written.join(', ') }));
        } catch (e) { notifyError(e); }
    };
    return (
        <Space direction="vertical" style={{ width: '100%' }} size="large">
            <section aria-label={t('export.title')}>
                <SectionTitle>{t('export.title')}</SectionTitle>
                <Muted style={{ display: 'block', marginBottom: 8 }}>{t('export.description')}</Muted>
                <Space wrap style={{ marginBottom: 8 }}>
                    <Button type="primary" onClick={() => setDialog({ label: 'New destination' })}>{t('export.new')}</Button>
                    <Button onClick={showConfig}>{t('export.config')}</Button>
                    <Button onClick={apply}>{t('export.apply')}</Button>
                </Space>
                {q.error && <ErrorNotice error={q.error} area={t('export.title')} docs={SPEC('observability-exporters').docs} />}
                {q.data && q.data.items.length === 0 ? <Empty description={t('export.none')} /> : (
                    <Table size="small" rowKey="id" loading={q.loading} dataSource={q.data?.items ?? []} pagination={false}
                        columns={[
                            { title: t('export.title'), dataIndex: 'name', render: (v: string, r) => v || r.id },
                            { title: 'Protocol', dataIndex: 'protocol' },
                            { title: 'Endpoint', dataIndex: 'endpoint' },
                            { title: 'Signals', dataIndex: 'signals', render: (v: string[]) => v.map((s) => <Tag key={s}>{s}</Tag>) },
                            { title: 'Headers', dataIndex: 'headers', render: (v: Exporter['headers']) => v.map((h) => `${h.name} ← ${h.scope}/${h.key}`).join(', ') || '–' },
                            { title: '', dataIndex: 'enabled', render: (v: boolean) => (v ? null : <Tag>off</Tag>) },
                            { title: '', key: 'act', render: (_: unknown, r) => (
                                <Space>
                                    <Button size="small" onClick={() => setDialog({ label: 'Edit destination', row: r as unknown as Record<string, unknown> })}>
                                        {t('export.edit')}</Button>
                                    <RowActions name={r.name || r.id} actions={[{ key: 'delete', label: t('export.delete'), danger: true,
                                        confirm: `${t('export.delete')} ${r.name || r.id}?`, onSelect: async () => {
                                            try { await client.request('DELETE', `/v1/observability/exporters/${enc(r.id)}`); q.refresh(); } catch (e) { notifyError(e); }
                                        } }]} />
                                </Space>) },
                        ]} />
                )}
            </section>
            {config && (
                <section aria-label={t('export.config')}>
                    <SectionTitle>{t('export.config')}</SectionTitle>
                    {config.env.length > 0 && <Muted style={{ display: 'block' }}>{t('export.env', { names: config.env.join(', ') })}</Muted>}
                    <pre tabIndex={0} style={{ maxHeight: 360, overflow: 'auto', fontSize: 12, background: tokens.color.surfaceAlt, padding: 8 }}>{config.yaml}</pre>
                </section>
            )}
            {dialog && (
                <ActionDialog spec={SPEC('observability-exporters')} action={action('observability-exporters', dialog.label)} row={dialog.row ?? null}
                    onDone={() => { setDialog(null); q.refresh(); }} onClose={() => setDialog(null)} />
            )}
        </Space>
    );
}
