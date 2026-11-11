/**
 * Quality & operations (L149): one run list, one incident list and one alert list over the stores that hold them
 * today. Each row says which store it comes from; each store is read through its own existing API, and a store the
 * person may not read (or that is not set up) is named as unavailable rather than failing the whole list.
 *
 * Until the backend merges (one incident model, one run history), these lists are views: they change nothing.
 */
import { Alert, Button, Input, Select, Space, Table, Tag } from 'antd';
import React, { useMemo, useState } from 'react';
import { Link, useHistory, useLocation } from 'react-router-dom';

import type { ConsumerClient } from '../api/client';
import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { SavedQuery } from '../api/types';
import { Alerts as SqlAlerts } from '../pages/DashboardsPage';
import { AlertsTab, IncidentDrawer, SEVERITY_COLOR, useSearchParam } from '../pages/ObservabilityPage';
import { ErrorNotice, describeError, notifyError } from './ErrorNotice';
import { PipelinesTab } from './PipelinesObs';
import { StatusBadge, StatusTone, useStatusLabel } from './StatusBadge';
import { When } from '../utils/time';
import { Muted, Surface } from './tokens';
import { shortUrn } from '../utils/format';
import { isNotConnected } from '../utils/errors';
import { useT } from '../i18n';

/** How many objects of one kind are asked for their runs (the newest few runs each). */
export const FANOUT = 20;

export interface Problem {
    source: string;
    error: string;
    /** The source's service is not set up here (its API is not mounted): shown as not connected, not as a problem. */
    notConnected?: boolean;
}

export interface Gathered<T> {
    rows: T[];
    /** Stores that could not be read, and why (no privilege, not set up). */
    problems: Problem[];
}

type Get = <T>(path: string) => Promise<T>;

const getter = (c: ConsumerClient, signal?: AbortSignal): Get => (path) => c.request('GET', path, undefined, { signal });

/** Reads every source; one that fails is a problem, not a failure of the list. */
async function gather<T>(sources: [string, () => Promise<T[]>][]): Promise<Gathered<T>> {
    const settled = await Promise.allSettled(sources.map(([, f]) => f()));
    const rows: T[] = [];
    const problems: Problem[] = [];
    settled.forEach((s, i) => {
        if (s.status === 'fulfilled') rows.push(...s.value);
        else problems.push({ source: sources[i][0], error: describeError(s.reason), notConnected: isNotConnected(s.reason) });
    });
    return { rows, problems };
}

/** The runs of each object (the first FANOUT objects), asked in parallel; an object whose runs fail is skipped. */
async function perObject<O, R>(objects: O[], runs: (o: O) => Promise<R[]>): Promise<[O, R[]][]> {
    const picked = objects.slice(0, FANOUT);
    const out = await Promise.allSettled(picked.map(runs));
    return picked.map((o, i): [O, R[]] => [o, out[i].status === 'fulfilled' ? (out[i] as PromiseFulfilledResult<R[]>).value : []]);
}

// ---- runs -------------------------------------------------------------------------------------------------------
export type RunSource = 'flow' | 'sql-workflow' | 'job' | 'dbt' | 'derived' | 'pipe' | 'airflow';
export type RunState = 'succeeded' | 'failed' | 'running' | 'other';

export interface RunRow {
    key: string;
    source: RunSource;
    /** The flow, job, dbt project, table, pipe or DAG. */
    name: string;
    state: RunState;
    /** The status as its store says it. */
    raw: string;
    started_ms: number | null;
    finished_ms: number | null;
    trigger?: string | null;
    error?: string | null;
    /** Where the object and its runs are managed. */
    link: string;
}

export const RUN_SOURCE_LABEL: Record<RunSource, string> = {
    flow: 'Flow', 'sql-workflow': 'SQL workflow', job: 'Job', dbt: 'dbt', derived: 'Derived table', pipe: 'Pipe', airflow: 'Airflow',
};

export function runState(raw: string | null | undefined): RunState {
    const s = String(raw ?? '').toLowerCase();
    if (['ok', 'success', 'succeeded', 'pass', 'passed', 'done', 'healthy'].includes(s)) return 'succeeded';
    if (['error', 'failed', 'fail', 'failure', 'runtime error', 'timeout', 'timed_out', 'upstream_failed'].includes(s)) return 'failed';
    if (['running', 'queued', 'pending', 'waiting', 'starting', 'scheduled', 'retry', 'up_for_retry'].includes(s)) return 'running';
    return 'other';
}

interface Named { id?: string; name: string }
interface FlowRun { id: string; status: string; trigger?: string; created_ms?: number; started_ms: number | null; finished_ms: number | null; error?: string | null }
interface JobRun { id: string; status: string; trigger?: string; started_ms: number; finished_ms: number | null; error?: string | null }
interface DbtRunRow { invocation: string; project: string; command: string; status: string; started_ms: number | null; finished_ms: number }
interface DerivedRunRow { id: string; status: string; trigger: string; started_ms: number; finished_ms: number; error: string | null }
interface PipeFileRow { key: string; version: string; status: string; error: string | null; loaded_ms: number }
interface PipelineItem {
    key: string; name: string; source: string;
    last_run: { run_id: string; state: string; start_ms: number | null; duration_ms: number | null; error: string | null } | null;
}

const enc = encodeURIComponent;

/** Every run Cairn can see, newest first, from the run APIs of flows, SQL workflows, jobs, dbt, derived tables, pipes
 * and Airflow (through pipeline observability). */
export async function loadRuns(c: ConsumerClient, signal?: AbortSignal): Promise<Gathered<RunRow>> {
    const get = getter(c, signal);
    const out = await gather<RunRow>([
        ['Flows', async () => (await perObject(await get<Named[]>('/v1/flows'), (f) => get<FlowRun[]>(`/v1/flows/${enc(f.name)}/runs?limit=10`)))
            .flatMap(([f, runs]) => runs.map((r) => ({
                key: `flow:${r.id}`, source: 'flow' as const, name: f.name, state: runState(r.status), raw: r.status,
                started_ms: r.started_ms ?? r.created_ms ?? null, finished_ms: r.finished_ms, trigger: r.trigger, error: r.error,
                link: `/flows?flow=${enc(f.name)}`,
            })))],
        ['SQL workflows', async () => (await perObject(await get<Required<Named>[]>('/v1/sql-workflows'),
            (w) => get<JobRun[]>(`/v1/sql-workflows/${enc(w.id)}/runs`)))
            .flatMap(([w, runs]) => runs.map((r) => ({
                key: `sqlwf:${r.id}`, source: 'sql-workflow' as const, name: w.name, state: runState(r.status), raw: r.status,
                started_ms: r.started_ms, finished_ms: r.finished_ms, trigger: r.trigger, error: null, link: '/flows?tab=sql-workflows',
            })))],
        ['Jobs', async () => (await perObject(await get<Named[]>('/v1/jobs'), (j) => get<JobRun[]>(`/v1/jobs/${enc(j.name)}/runs`)))
            .flatMap(([j, runs]) => runs.map((r) => ({
                key: `job:${r.id}`, source: 'job' as const, name: j.name, state: runState(r.status), raw: r.status,
                started_ms: r.started_ms, finished_ms: r.finished_ms, trigger: r.trigger, error: r.error, link: '/flows?tab=jobs',
            })))],
        ['dbt', async () => (await get<DbtRunRow[]>('/pipelines/dbt/runs')).map((r) => ({
            key: `dbt:${r.invocation}`, source: 'dbt' as const, name: `${r.project} · ${r.command}`, state: runState(r.status),
            raw: r.status, started_ms: r.started_ms, finished_ms: r.finished_ms, link: '/runs?tab=dbt',
        }))],
        ['Derived tables', async () => (await perObject(await get<Required<Named>[]>('/v1/derived'),
            (d) => get<DerivedRunRow[]>(`/v1/derived/${enc(d.id)}/runs`)))
            .flatMap(([d, runs]) => runs.slice(0, 10).map((r) => ({
                key: `derived:${r.id}`, source: 'derived' as const, name: d.name, state: runState(r.status), raw: r.status,
                started_ms: r.started_ms, finished_ms: r.finished_ms, trigger: r.trigger, error: r.error, link: '/lakehouse?tab=derived',
            })))],
        ['Pipes', async () => (await perObject(await get<Required<Named>[]>('/v1/pipes'),
            (p) => get<PipeFileRow[]>(`/v1/pipes/${enc(p.id)}/files`)))
            .flatMap(([p, files]) => files.slice(0, 10).map((f) => ({
                key: `pipe:${p.id}:${f.key}:${f.version}`, source: 'pipe' as const, name: `${p.name} · ${f.key}`,
                state: runState(f.status), raw: f.status, started_ms: f.loaded_ms, finished_ms: f.loaded_ms, error: f.error,
                link: '/ingestion?tab=pipes',
            })))],
        ['Airflow', async () => (await get<{ items: PipelineItem[] }>('/v1/observability/pipelines?source=airflow')).items
            .filter((p) => p.last_run).map((p) => ({
                key: `airflow:${p.key}:${p.last_run!.run_id}`, source: 'airflow' as const, name: p.name, state: runState(p.last_run!.state),
                raw: p.last_run!.state, started_ms: p.last_run!.start_ms,
                finished_ms: p.last_run!.start_ms != null && p.last_run!.duration_ms != null ? p.last_run!.start_ms + p.last_run!.duration_ms : null,
                error: p.last_run!.error, link: `/runs?tab=pipelines&pipeline=${enc(p.key)}`,
            }))],
    ]);
    out.rows.sort((a, b) => (b.started_ms ?? 0) - (a.started_ms ?? 0));
    return out;
}

const STATE_TONE: Record<RunState, StatusTone> = { succeeded: 'ok', failed: 'error', running: 'running', other: 'warn' };
const when = (ms: number | null | undefined) => <When value={ms} />;
const took = (a: number | null, b: number | null) => (a && b && b >= a ? `${((b - a) / 1000).toFixed(1)}s` : '');

function Problems({ problems }: { problems: Problem[] }) {
    const t = useT();
    const failing = problems.filter((p) => !p.notConnected);
    const absent = problems.filter((p) => p.notConnected).map((p) => p.source);
    return (
        <>
            {absent.length > 0 && (
                <Muted style={{ display: 'block', marginBottom: 12 }} data-error-kind="notConnected">
                    {t('err.notConnected.sources', { sources: absent.join(', ') })}
                </Muted>
            )}
            {failing.length > 0 && (
                <Alert type="info" showIcon style={{ marginBottom: 12 }} message="Some sources are not shown"
                    description={<ul style={{ margin: 0, paddingLeft: 18 }}>{failing.map((p) => <li key={p.source}><strong>{p.source}</strong>: {p.error}</li>)}</ul>} />
            )}
        </>
    );
}

/** Runs › All runs: one list over every orchestrator and every object that runs on its own schedule. */
export function AllRuns() {
    const q = useDheQuery<Gathered<RunRow>>('ops:runs', (c, signal) => loadRuns(c, signal), { staleMs: 15_000 });
    const [source, setSource] = useState<RunSource | ''>('');
    const [state, setState] = useState<RunState | ''>('');
    const rows = (q.data?.rows ?? []).filter((r) => (!source || r.source === source) && (!state || r.state === state));
    const words = useStatusLabel();
    return (
        <section aria-label="All runs">
            <Muted style={{ display: 'block', marginBottom: 12 }}>
                Every run of flows, SQL workflows, jobs, dbt, derived tables, pipes and Airflow DAGs, newest first. Each row
                opens the place its runs are managed; pipeline SLAs and run details are in the Pipelines tab.
            </Muted>
            <Space wrap style={{ marginBottom: 12 }}>
                <Select aria-label="Kind" allowClear placeholder="Every kind" style={{ minWidth: 180 }} value={source || undefined}
                    onChange={(v) => setSource((v ?? '') as RunSource | '')}
                    options={(Object.keys(RUN_SOURCE_LABEL) as RunSource[]).map((k) => ({ value: k, label: RUN_SOURCE_LABEL[k] }))} />
                <Select aria-label="Result" allowClear placeholder="Any result" style={{ minWidth: 160 }} value={state || undefined}
                    onChange={(v) => setState((v ?? '') as RunState | '')}
                    options={(['succeeded', 'failed', 'running', 'other'] as const).map((v) => ({ value: v, label: words(v) }))} />
                <Button onClick={() => q.refresh()} loading={q.loading}>Refresh</Button>
            </Space>
            {q.error && <ErrorNotice error={q.error} />}
            <Problems problems={q.data?.problems ?? []} />
            <Table<RunRow> size="small" rowKey="key" loading={q.loading && !q.data} dataSource={rows} pagination={{ pageSize: 25, showSizeChanger: false }}
                aria-label="Runs"
                locale={{ emptyText: 'No runs yet' }}
                columns={[
                    { title: 'Kind', dataIndex: 'source', render: (s: RunSource) => <Tag>{RUN_SOURCE_LABEL[s]}</Tag> },
                    { title: 'What', dataIndex: 'name', render: (n: string, r) => <Link to={r.link}>{n}</Link> },
                    { title: 'Result', dataIndex: 'state', render: (s: RunState, r) => <StatusBadge state={s} tone={STATE_TONE[s]} title={r.raw || undefined} plain /> },
                    { title: 'Started', dataIndex: 'started_ms', render: when },
                    { title: 'Took', render: (_, r) => took(r.started_ms, r.finished_ms) },
                    { title: 'Why', dataIndex: 'trigger', render: (v?: string | null) => v ?? '' },
                    { title: 'Error', dataIndex: 'error', render: (v?: string | null) => (v ? <Muted>{v.slice(0, 160)}</Muted> : '') },
                ]} />
        </section>
    );
}

/** Runs › Pipelines: pipeline observability (runs, SLAs, data products of pipelines), with its links kept in the URL. */
export function PipelineRuns() {
    const history = useHistory();
    const [pipeline] = useSearchParam('pipeline');
    const [run] = useSearchParam('run');
    const [, set] = useSearchParam('pipeline');
    return (
        <PipelinesTab pipeline={pipeline} run={run} select={(p, r) => set(p, { run: r ?? null })}
            openTrace={(id) => history.push(`/observability?tab=journey&trace=${enc(id)}`)} />
    );
}

// ---- incidents --------------------------------------------------------------------------------------------------
export type IncidentSource = 'quality' | 'platform' | 'status';
export interface IncidentRow {
    key: string;
    source: IncidentSource;
    title: string;
    severity: string;
    /** open, acknowledged, resolved (status page: investigating, …; quality: failing). */
    status: string;
    open: boolean;
    at_ms: number | null;
    about: string;
    /** A platform incident's id (opens the incident and the SRE agent here). */
    id?: string;
    link?: string;
}

export const INCIDENT_SOURCE_LABEL: Record<IncidentSource, string> = {
    quality: 'Data quality', platform: 'Platform', status: 'Status page',
};

interface MonitorRow { id: string; name: string; dataset_urn: string; severity?: string; enabled?: boolean;
    last_evaluation?: { status: string; ts_ms: number; message: string } | null }
interface PlatformIncident { id: string; title: string; severity: string; status: string; services: string[]; opened_at: string }
interface StatusIncident { id: string; title: string; impact: string; components: string[]; starts_ms: number; resolved_ms: number | null;
    updates?: { phase: string }[] }

/** Incidents from their three stores: quality (failing monitors raise Core incidents), platform (observability, with
 * the SRE agent) and the public status page. */
export async function loadIncidents(c: ConsumerClient, signal?: AbortSignal): Promise<Gathered<IncidentRow>> {
    const get = getter(c, signal);
    const out = await gather<IncidentRow>([
        ['Data quality', async () => (await get<MonitorRow[]>('/quality/monitors'))
            .filter((m) => m.enabled !== false && m.last_evaluation && ['FAIL', 'ERROR'].includes(m.last_evaluation.status))
            .map((m) => ({
                key: `quality:${m.id}`, source: 'quality' as const, title: `${m.name} failing`, severity: m.severity ?? 'warning',
                status: m.last_evaluation!.status.toLowerCase(), open: true, at_ms: m.last_evaluation!.ts_ms,
                about: shortUrn(m.dataset_urn), link: `/asset/${enc(m.dataset_urn)}`,
            }))],
        ['Platform', async () => (await get<{ items: PlatformIncident[] }>('/v1/observability/incidents?status=active')).items.map((i) => ({
            key: `platform:${i.id}`, source: 'platform' as const, title: i.title, severity: i.severity, status: i.status,
            open: i.status !== 'resolved', at_ms: Date.parse(i.opened_at) || null, about: i.services.join(', '), id: i.id,
        }))],
        ['Status page', async () => {
            const s = await get<{ incidents: StatusIncident[]; history?: StatusIncident[] }>('/status.json');
            return [...(s.incidents ?? []), ...(s.history ?? []).slice(0, 5)].map((i) => ({
                key: `status:${i.id}`, source: 'status' as const, title: i.title, severity: i.impact,
                status: i.resolved_ms ? 'resolved' : i.updates?.[i.updates.length - 1]?.phase ?? 'investigating',
                open: !i.resolved_ms, at_ms: i.starts_ms, about: i.components.join(', '), link: '/alerts?tab=status-announcements',
            }));
        }],
    ]);
    out.rows.sort((a, b) => Number(b.open) - Number(a.open) || (b.at_ms ?? 0) - (a.at_ms ?? 0));
    return out;
}

/** Incidents: one list, each row labelled with where it comes from; platform incidents open here with the SRE agent. */
export function AllIncidents() {
    const { client } = useConsumerApi();
    const q = useDheQuery<Gathered<IncidentRow>>('ops:incidents', (c, signal) => loadIncidents(c, signal), { pollMs: 30_000 });
    const [incident, setIncident] = useSearchParam('incident');
    const [source, setSource] = useState<IncidentSource | ''>('');
    const [openOnly, setOpenOnly] = useState(true);
    const [form, setForm] = useState<{ title: string; severity: string; services: string } | null>(null);
    const rows = (q.data?.rows ?? []).filter((r) => (!source || r.source === source) && (!openOnly || r.open));
    const create = async () => {
        if (!form) return;
        try {
            const inc = await client.request<{ id: string }>('POST', '/v1/observability/incidents', {
                title: form.title, severity: form.severity, services: form.services.split(',').map((s) => s.trim()).filter(Boolean) });
            setForm(null);
            q.refresh();
            setIncident(inc.id);
        } catch (e) {
            notifyError(e);
        }
    };
    return (
        <section aria-label="Incidents">
            <Muted style={{ display: 'block', marginBottom: 12 }}>
                Data quality incidents (failing monitors, raised in Cairn Core), platform incidents (with the SRE agent) and
                status page announcements, in one list. Each row names its source.
            </Muted>
            <Space wrap style={{ marginBottom: 12 }}>
                <Select aria-label="Source" allowClear placeholder="Every source" style={{ minWidth: 170 }} value={source || undefined}
                    onChange={(v) => setSource((v ?? '') as IncidentSource | '')}
                    options={(Object.keys(INCIDENT_SOURCE_LABEL) as IncidentSource[]).map((k) => ({ value: k, label: INCIDENT_SOURCE_LABEL[k] }))} />
                <Select aria-label="Status" value={openOnly ? 'open' : 'all'} style={{ width: 140 }} onChange={(v) => setOpenOnly(v === 'open')}
                    options={[{ value: 'open', label: 'Open' }, { value: 'all', label: 'Open and recent' }]} />
                <Button onClick={() => setForm({ title: '', severity: 'warning', services: '' })}>Open incident</Button>
            </Space>
            {form && (
                <Surface style={{ marginBottom: 12, padding: 12 }}>
                    <Space wrap>
                        <Input aria-label="Title" placeholder="Title" value={form.title} style={{ width: 280 }}
                            onChange={(e) => setForm({ ...form, title: e.target.value })} />
                        <Select aria-label="Severity" value={form.severity} style={{ width: 120 }} onChange={(v) => setForm({ ...form, severity: v })}
                            options={['critical', 'warning', 'info'].map((v) => ({ value: v, label: v }))} />
                        <Input aria-label="Services" placeholder="Services" value={form.services} style={{ width: 200 }}
                            onChange={(e) => setForm({ ...form, services: e.target.value })} />
                        <Button type="primary" disabled={!form.title.trim()} onClick={create}>Open incident</Button>
                    </Space>
                </Surface>
            )}
            {q.error && <ErrorNotice error={q.error} />}
            <Problems problems={q.data?.problems ?? []} />
            <Table<IncidentRow> size="small" rowKey="key" loading={q.loading && !q.data} dataSource={rows} pagination={{ pageSize: 25, showSizeChanger: false }}
                locale={{ emptyText: 'No open incidents' }}
                columns={[
                    { title: 'Source', dataIndex: 'source', render: (s: IncidentSource) => <Tag>{INCIDENT_SOURCE_LABEL[s]}</Tag> },
                    { title: '', dataIndex: 'severity', render: (v: string) => <Tag color={SEVERITY_COLOR[v] ?? 'default'}>{v}</Tag> },
                    { title: 'Incident', dataIndex: 'title', render: (v: string, r) => (r.id
                        ? <Button type="link" size="small" style={{ padding: 0 }} aria-label={`Open ${v}`} onClick={() => setIncident(r.id!)}>{v}</Button>
                        : r.link ? <Link to={r.link}>{v}</Link> : v) },
                    { title: 'Status', dataIndex: 'status', render: (v: string) => <StatusBadge state={v} plain /> },
                    { title: 'About', dataIndex: 'about' },
                    { title: 'Since', dataIndex: 'at_ms', render: when },
                ]} />
            {incident && <IncidentDrawer id={incident} onClose={() => setIncident(null)} />}
        </section>
    );
}

// ---- alerts -----------------------------------------------------------------------------------------------------
export type AlertKind = 'sql' | 'metric' | 'platform' | 'budget';
export interface AlertDefRow {
    key: string;
    kind: AlertKind;
    name: string;
    /** ok, triggered, firing, error, unknown … as its store says. */
    state: string;
    firing: boolean;
    detail: string;
    link: string;
}

export const ALERT_KIND_LABEL: Record<AlertKind, string> = {
    sql: 'Query (SQL)', metric: 'Metric', platform: 'Platform rule', budget: 'Budget',
};

interface SqlAlertRow { id: string; name: string; op: string; value: number; measure: string; state: string; enabled: boolean }
interface MetricAlertRow { id: string; name: string; metric: string; state: string; rule: { kind: string } }
interface RuleRow { id: string; name: string; severity: string; enabled: boolean; last_evaluation?: { error?: string; matches?: number } | null }
interface BudgetRow { id: string; name: string; amount_usd: number; period: string; thresholds: number[] }

/** Every alert definition, whatever it watches: saved queries, metrics, platform telemetry and budgets. */
export async function loadAlerts(c: ConsumerClient, signal?: AbortSignal): Promise<Gathered<AlertDefRow>> {
    const get = getter(c, signal);
    return gather<AlertDefRow>([
        ['Query alerts', async () => (await get<SqlAlertRow[]>('/sql/alerts')).map((a) => ({
            key: `sql:${a.id}`, kind: 'sql' as const, name: a.name, state: a.enabled ? a.state : 'off', firing: a.state === 'triggered',
            detail: `${a.measure} ${a.op} ${a.value}`, link: '/alerts?tab=sql-alerts',
        }))],
        ['Metric alerts', async () => (await get<MetricAlertRow[]>('/sql/metric-alerts')).map((a) => ({
            key: `metric:${a.id}`, kind: 'metric' as const, name: a.name, state: a.state, firing: a.state === 'triggered',
            detail: `${a.metric}: ${a.rule.kind}`, link: '/alerts?tab=metric-alerts',
        }))],
        ['Platform rules', async () => (await get<{ items: RuleRow[] }>('/v1/observability/alert-rules')).items.map((r) => ({
            key: `platform:${r.id}`, kind: 'platform' as const, name: r.name,
            state: !r.enabled ? 'off' : r.last_evaluation?.error ? 'error' : r.last_evaluation?.matches ? 'firing' : 'ok',
            firing: !!r.enabled && !!r.last_evaluation?.matches, detail: r.severity, link: '/alerts?tab=platform-rules',
        }))],
        ['Budgets', async () => (await get<BudgetRow[]>('/v1/budgets')).map((b) => ({
            key: `budget:${b.id}`, kind: 'budget' as const, name: b.name, state: 'watching', firing: false,
            detail: `${b.amount_usd} USD a ${b.period}, told at ${(b.thresholds ?? []).map((x) => `${Math.round(x * (x <= 1 ? 100 : 1))}%`).join(', ')}`,
            link: '/usage?tab=budgets',
        }))],
    ]);
}

/** Alerts & routing › All alerts. */
export function AllAlerts() {
    const q = useDheQuery<Gathered<AlertDefRow>>('ops:alerts', (c, signal) => loadAlerts(c, signal), { staleMs: 15_000 });
    const [kind, setKind] = useState<AlertKind | ''>('');
    const rows = useMemo(() => (q.data?.rows ?? []).filter((r) => !kind || r.kind === kind)
        .sort((a, b) => Number(b.firing) - Number(a.firing) || a.name.localeCompare(b.name)), [q.data, kind]);
    return (
        <section aria-label="All alerts">
            <Muted style={{ display: 'block', marginBottom: 12 }}>
                Every alert, whatever it watches: saved queries, governed metrics, platform telemetry and budgets. Each
                kind is made and changed in its own tab (budgets in Usage &amp; cost); where alerts are sent is in
                Notification routes.
            </Muted>
            <Space wrap style={{ marginBottom: 12 }}>
                <Select aria-label="Alert kind" allowClear placeholder="Every kind" style={{ minWidth: 180 }} value={kind || undefined}
                    onChange={(v) => setKind((v ?? '') as AlertKind | '')}
                    options={(Object.keys(ALERT_KIND_LABEL) as AlertKind[]).map((k) => ({ value: k, label: ALERT_KIND_LABEL[k] }))} />
                <Button onClick={() => q.refresh()} loading={q.loading}>Refresh</Button>
            </Space>
            {q.error && <ErrorNotice error={q.error} />}
            <Problems problems={q.data?.problems ?? []} />
            <Table<AlertDefRow> size="small" rowKey="key" loading={q.loading && !q.data} dataSource={rows}
                pagination={rows.length > 25 ? { pageSize: 25, showSizeChanger: false } : false} locale={{ emptyText: 'No alerts yet' }}
                columns={[
                    { title: 'Kind', dataIndex: 'kind', render: (k: AlertKind) => <Tag>{ALERT_KIND_LABEL[k]}</Tag> },
                    { title: 'Alert', dataIndex: 'name', render: (v: string, r) => <Link to={r.link}>{v}</Link> },
                    { title: 'State', dataIndex: 'state', render: (v: string, r) => <Tag color={r.firing ? 'red' : v === 'error' ? 'orange' : 'default'}>{v}</Tag> },
                    { title: 'Watches', dataIndex: 'detail' },
                ]} />
        </section>
    );
}

/** Alerts & routing › Query alerts (saved queries checked on a schedule). */
export function SqlAlertsView() {
    const saved = useDheQuery<SavedQuery[]>('/sql/saved', (c, signal) => c.savedQueries({ signal }));
    return <SqlAlerts saved={saved.data ?? []} />;
}

/** Alerts & routing › Platform rules: alerts on platform telemetry, their rules and silences. */
export function PlatformAlerts() {
    const history = useHistory();
    const [isNew, setNew] = useSearchParam('new');
    return <AlertsTab startNew={isNew} onNewClosed={() => setNew(null)} openIncident={(id) => history.push(`/incidents?incident=${enc(id)}`)} />;
}

/** Alerts & routing › Status announcements: the public status page's incidents and maintenance. */
export function StatusAnnouncements() {
    const { client } = useConsumerApi();
    const location = useLocation();
    const q = useDheQuery<{ state: string; components: { name: string; state: string }[]; incidents: StatusIncident[];
        maintenance: StatusIncident[]; history: StatusIncident[] }>('/status.json', (c, signal) => c.request('GET', '/status.json', undefined, { signal }));
    const [form, setForm] = useState<{ title: string; impact: string; components: string[]; message: string } | null>(null);
    const post = async () => {
        if (!form) return;
        try {
            await client.request('POST', '/v1/status/incidents', form);
            setForm(null);
            q.refresh();
        } catch (e) {
            notifyError(e);
        }
    };
    const rows = q.data ? [...q.data.incidents, ...q.data.maintenance, ...q.data.history] : [];
    return (
        <section aria-label="Status announcements" data-path={location.pathname}>
            <Muted style={{ display: 'block', marginBottom: 12 }}>
                What the public status page (<a href="/status" target="_blank" rel="noreferrer">/status</a>) tells people: incidents
                and maintenance, announced by operators. Platform incidents you work on are in Incidents.
            </Muted>
            {q.error && <ErrorNotice error={q.error} />}
            {q.data && (
                <Space wrap style={{ marginBottom: 12 }}>
                    <Tag>{q.data.state}</Tag>
                    <Button onClick={() => setForm({ title: '', impact: 'degraded', components: [], message: '' })}>New announcement</Button>
                </Space>
            )}
            {form && q.data && (
                <Surface style={{ marginBottom: 12, padding: 12 }}>
                    <Space wrap>
                        <Input aria-label="Announcement title" placeholder="Title" value={form.title} style={{ width: 260 }}
                            onChange={(e) => setForm({ ...form, title: e.target.value })} />
                        <Select aria-label="Impact" value={form.impact} style={{ width: 160 }} onChange={(v) => setForm({ ...form, impact: v })}
                            options={['degraded', 'partial_outage', 'outage', 'maintenance'].map((v) => ({ value: v, label: v }))} />
                        <Select aria-label="Components" mode="multiple" value={form.components} style={{ minWidth: 220 }}
                            onChange={(v) => setForm({ ...form, components: v })}
                            options={q.data.components.map((c) => ({ value: c.name, label: c.name }))} />
                        <Input aria-label="Message" placeholder="What people should know" value={form.message} style={{ width: 320 }}
                            onChange={(e) => setForm({ ...form, message: e.target.value })} />
                        <Button type="primary" disabled={!form.title.trim() || !form.message.trim() || !form.components.length} onClick={post}>
                            Announce
                        </Button>
                    </Space>
                </Surface>
            )}
            <Table<StatusIncident> size="small" rowKey="id" dataSource={rows} loading={q.loading && !q.data} pagination={false}
                locale={{ emptyText: 'Nothing announced' }}
                columns={[
                    { title: 'Announcement', dataIndex: 'title' },
                    { title: 'Impact', dataIndex: 'impact', render: (v: string) => <Tag>{v}</Tag> },
                    { title: 'Components', dataIndex: 'components', render: (v: string[]) => v.join(', ') },
                    { title: 'From', dataIndex: 'starts_ms', render: when },
                    { title: 'Resolved', dataIndex: 'resolved_ms', render: (v: number | null) => (v ? when(v) : '') },
                ]} />
        </section>
    );
}

