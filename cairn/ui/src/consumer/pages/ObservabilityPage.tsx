import { Alert, Button, Checkbox, Descriptions, Drawer, Empty, Input, List, Popconfirm, Select, Space, Table, Tabs, Tag, message } from 'antd';
import React, { useState } from 'react';
import { Link, useHistory, useLocation } from 'react-router-dom';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import { ErrorNotice, notifyError } from '../components/ErrorNotice';
import { ExportTab, PipelinesTab } from '../components/PipelinesObs';
import { StatusBadge, StatusTone } from '../components/StatusBadge';
import { EvidenceList, SreIncidentPanel } from '../components/SreAgent';
import type { Evidence } from '../components/SreAgent';
import { Muted, Page, SectionTitle, Surface, tokens } from '../components/tokens';
import { pageHelp } from '../guide/help';
import { PageHeader } from '../guide/PageHeader';
import { MessageKey, useT } from '../i18n';
import { RESOURCE_SPECS } from '../resources/catalog';
import { ActionDialog } from '../resources/ResourceView';
import type { ResourceAction, ResourceSpec } from '../resources/types';
import { errorStatus } from '../utils/errors';
import { actorName } from '../utils/format';

// ---- what the API answers (cairn.observability) ------------------------------------------------------------------
interface Overview {
    backends: { prometheus: boolean; loki: boolean; tempo: boolean; trace_links: boolean; log_links: boolean; vendor: string | null };
    firing: Record<'critical' | 'warning' | 'info', number>;
    open_incidents: number;
    slos: number;
    alert_rules: number;
    silences_active: number;
}
export interface Component {
    name: string;
    state: 'up' | 'degraded' | 'down' | 'unknown';
    rate: number | null;
    error_ratio: number | null;
    p95_ms: number | null;
    up: boolean | null;
    targets: number;
    firing_alerts: number;
}
interface Health { window: string; components: Component[]; edges: { from: string; to: string; rate: number; error_ratio: number }[] }
export interface Span {
    span_id: string;
    parent_id: string;
    service: string;
    name: string;
    kind: string;
    offset_ms: number;
    duration_ms: number;
    status: 'ok' | 'error';
    status_message?: string | null;
    attributes: Record<string, unknown>;
}
export interface Journey {
    trace_id: string;
    source: 'tempo' | 'link';
    links: { trace?: string; logs?: string };
    duration_ms?: number;
    services: string[];
    spans: Span[];
    errors?: number;
}
interface TraceRow { trace_id: string; root_service: string; root_name: string; start_ms: number; duration_ms: number }
interface LogLine { at: string; service: string | null; level: string | null; trace_id: string | null; line: string }
interface Slo { id: string; name: string; service: string; objective: number; window: string; enabled: boolean; last_evaluation?: { error?: string } | null }
interface SloStatus {
    sli: number | null;
    budget_remaining: number | null;
    burn: { long: string; short: string; factor: number; severity: string; long_burn: number | null; short_burn: number | null; burning: boolean }[];
}
interface AlertRow {
    id: string; source: string; name: string; severity: string; state: string; labels: Record<string, string>;
    value: number | null; summary: string; started_at: string; firing_since: string | null; resolved_at: string | null;
    silenced_by: string | null; acknowledged_by: string | null; incident_id: string | null;
}
interface Rule { id: string; name: string; language: string; severity: string; pending_for: string; enabled: boolean; last_evaluation?: { error?: string; matches?: number } | null }
interface Silence { id: string; comment: string; starts_at: string | null; ends_at: string; active: boolean; matchers: { name: string; value: string; regex?: boolean }[] }
interface Incident {
    id: string; title: string; severity: string; status: 'open' | 'acknowledged' | 'resolved'; services: string[];
    summary: string | null; assignee: string | null; opened_at: string; resolved_at: string | null;
}
interface TimelineEvent { at: string; kind: string; actor: string; by_agent: boolean; text: string; detail?: { evidence?: Evidence[] } }
interface IncidentDetail extends Incident { alerts: AlertRow[]; timeline: TimelineEvent[] }

const TABS = ['health', 'journey', 'logs', 'pipelines', 'slos', 'alerts', 'incidents', 'export'] as const;
const STATE_TONE: Record<string, StatusTone> = { up: 'ok', degraded: 'warn', down: 'error', unknown: 'waiting' };
const SEVERITY_COLOR: Record<string, string> = { critical: 'red', warning: 'orange', info: 'blue' };
const SPEC = (k: string) => RESOURCE_SPECS[k] as ResourceSpec;
const action = (k: string, label: string) => SPEC(k).actions!.find((a) => a.label === label) as ResourceAction;
const fmt = (v: number | null | undefined, digits = 2) => (v === null || v === undefined ? '–' : Number(v.toFixed(digits)).toString());
const pct = (v: number | null | undefined) => (v === null || v === undefined ? '–' : `${Number((v * 100).toFixed(2))}`);
const SERVICE_COLORS = ['#2f6feb', '#8a63d2', '#1a7f37', '#bf8700', '#cf222e', '#0a7ea4', '#a40e26', '#57606a'];

/** The colour of a component in a waterfall (stable by name). */
export function serviceColor(service: string, services: string[]): string {
    const i = Math.max(0, services.indexOf(service));
    return SERVICE_COLORS[i % SERVICE_COLORS.length];
}

/** The spans of a trace as rows of a waterfall: depth from parent links, bar from offset and duration (in %). */
export function waterfall(spans: Span[]): (Span & { depth: number; left: number; width: number })[] {
    const total = Math.max(1, ...spans.map((s) => s.offset_ms + s.duration_ms));
    const byId = new Map(spans.map((s) => [s.span_id, s]));
    const depth = (s: Span, seen = 0): number => {
        const p = s.parent_id ? byId.get(s.parent_id) : undefined;
        return p && seen < 64 ? 1 + depth(p, seen + 1) : 0;
    };
    return spans.map((s) => ({ ...s, depth: depth(s), left: (100 * s.offset_ms) / total,
        width: Math.max(0.4, (100 * s.duration_ms) / total) }));
}

function useSearchParam(name: string): [string, (v: string | null, extra?: Record<string, string | null>) => void] {
    const location = useLocation();
    const history = useHistory();
    const params = new URLSearchParams(location.search);
    const set = (v: string | null, extra: Record<string, string | null> = {}) => {
        const next = new URLSearchParams(location.search);
        for (const [k, val] of Object.entries({ [name]: v, ...extra })) {
            if (val === null || val === '') next.delete(k);
            else next.set(k, val);
        }
        history.replace({ pathname: location.pathname, search: `?${next.toString()}` });
    };
    return [params.get(name) ?? '', set];
}

function NotConfigured({ error }: { error: unknown }) {
    const t = useT();
    return (
        <Space direction="vertical" style={{ width: '100%' }}>
            {errorStatus(error) === 503 && <Alert type="info" showIcon message={t('obs.notConfigured')} />}
            <ErrorNotice error={error} area={t('obs.title')} docs={SPEC('observability-slos').docs} />
        </Space>
    );
}

// ---- health ----------------------------------------------------------------------------------------------------
function HealthTab({ openTraces }: { openTraces: (service: string) => void }) {
    const t = useT();
    const q = useDheQuery<Health>('/v1/observability/health', (c, signal) =>
        c.request('GET', '/v1/observability/health?window=5m', undefined, { signal }), { pollMs: 30_000 });
    if (q.error) return <NotConfigured error={q.error} />;
    const h = q.data;
    return (
        <Space direction="vertical" style={{ width: '100%' }} size="large">
            <section aria-label={t('obs.health.components')}>
                <SectionTitle>{t('obs.health.components')}</SectionTitle>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))', gap: 12 }}>
                    {(h?.components ?? []).map((c) => (
                        <Surface key={c.name} role="group" aria-label={`${c.name}: ${t(`obs.state.${c.state}` as MessageKey)}`}>
                            <Space style={{ justifyContent: 'space-between', width: '100%' }}>
                                <strong>{c.name}</strong>
                                <StatusBadge tone={STATE_TONE[c.state] ?? 'waiting'} label={t(`obs.state.${c.state}` as MessageKey)} />
                            </Space>
                            <Muted style={{ display: 'block' }}>
                                {c.rate !== null && t('obs.health.rate', { n: fmt(c.rate) })}
                                {c.error_ratio !== null && ` · ${t('obs.health.errors', { n: pct(c.error_ratio) })}`}
                                {c.p95_ms !== null && ` · ${t('obs.health.p95', { n: fmt(c.p95_ms, 0) })}`}
                            </Muted>
                            {c.targets > 0 && <Muted style={{ display: 'block' }}>{t('obs.health.targets', { n: c.targets })}</Muted>}
                            {c.firing_alerts > 0 && <Tag color="red">{t('obs.health.alerts', { n: c.firing_alerts })}</Tag>}
                            {c.rate !== null && (
                                <Button size="small" type="link" style={{ paddingLeft: 0 }} onClick={() => openTraces(c.name)}>
                                    {t('obs.health.traces', { name: c.name })}
                                </Button>
                            )}
                        </Surface>
                    ))}
                </div>
                {h && h.components.length === 0 && <Empty />}
            </section>
            <section aria-label={t('obs.health.calls')}>
                <SectionTitle>{t('obs.health.calls')}</SectionTitle>
                <Table size="small" rowKey={(e) => `${e.from}>${e.to}`} pagination={false} dataSource={h?.edges ?? []} loading={q.loading}
                    columns={[
                        { title: t('obs.edge.from'), dataIndex: 'from' },
                        { title: t('obs.edge.to'), dataIndex: 'to' },
                        { title: t('obs.edge.rate'), dataIndex: 'rate', render: (v: number) => fmt(v) },
                        { title: t('obs.edge.errors'), dataIndex: 'error_ratio',
                            render: (v: number) => <Tag color={v > 0.05 ? 'red' : 'default'}>{pct(v)}%</Tag> },
                    ]} />
            </section>
        </Space>
    );
}

// ---- request journey ----------------------------------------------------------------------------------------
function Waterfall({ j }: { j: Journey }) {
    const t = useT();
    const [open, setOpen] = useState<string | null>(null);
    const rows = waterfall(j.spans);
    return (
        <ol aria-label={t('obs.journey.waterfall', { id: j.trace_id })} style={{ listStyle: 'none', padding: 0, margin: 0 }}>
            {rows.map((s) => (
                <li key={s.span_id} style={{ borderBottom: `1px solid ${tokens.color.border}`, padding: '2px 0' }}>
                    <button type="button" onClick={() => setOpen(open === s.span_id ? null : s.span_id)}
                        aria-expanded={open === s.span_id}
                        aria-label={t('obs.journey.span', { service: s.service, name: s.name, ms: fmt(s.duration_ms) })}
                        style={{ display: 'flex', width: '100%', background: 'none', border: 0, padding: 0, cursor: 'pointer',
                            textAlign: 'left', alignItems: 'center', font: 'inherit', color: 'inherit' }}>
                        <span style={{ width: '38%', paddingLeft: s.depth * 12, overflow: 'hidden', whiteSpace: 'nowrap', textOverflow: 'ellipsis' }}>
                            <Tag color={serviceColor(s.service, j.services)} style={{ marginRight: 6 }}>{s.service}</Tag>
                            {s.status === 'error' && <Tag color="red">error</Tag>}{s.name}
                        </span>
                        <span style={{ position: 'relative', flex: 1, height: 14 }} aria-hidden>
                            <span style={{ position: 'absolute', left: `${s.left}%`, width: `${s.width}%`, top: 2, bottom: 2,
                                background: s.status === 'error' ? '#cf222e' : serviceColor(s.service, j.services), borderRadius: 2 }} />
                        </span>
                        <span style={{ width: 90, textAlign: 'right' }}>{fmt(s.duration_ms)} ms</span>
                    </button>
                    {open === s.span_id && (
                        <Descriptions size="small" column={1} bordered title={t('obs.journey.attributes')}>
                            {s.status_message && <Descriptions.Item label="status">{s.status_message}</Descriptions.Item>}
                            {Object.entries(s.attributes).map(([k, v]) => <Descriptions.Item key={k} label={k}>{String(v)}</Descriptions.Item>)}
                        </Descriptions>
                    )}
                </li>
            ))}
        </ol>
    );
}

function JourneyTab({ initial, service, showLogs }: { initial: string; service: string; showLogs: (traceId: string) => void }) {
    const t = useT();
    const [, setTrace] = useSearchParam('trace');
    const [, setService] = useSearchParam('service');
    const [input, setInput] = useState(initial);
    const [errorsOnly, setErrorsOnly] = useState(false);
    const traceId = initial.trim().toLowerCase();
    const recent = useDheQuery<{ items: TraceRow[] }>(`/v1/observability/traces?${service}&${errorsOnly}`, (c, signal) =>
        c.request('GET', `/v1/observability/traces?limit=20&errors=${errorsOnly}${service ? `&service=${encodeURIComponent(service)}` : ''}`,
            undefined, { signal }));
    const j = useDheQuery<Journey>(traceId ? `/v1/observability/journey/${traceId}` : null, (c, signal) =>
        c.request('GET', `/v1/observability/journey/${encodeURIComponent(traceId)}`, undefined, { signal }));
    return (
        <Space direction="vertical" style={{ width: '100%' }} size="large">
            <Space wrap>
                <Input aria-label={t('obs.journey.traceId')} placeholder={t('obs.journey.traceId')} value={input} style={{ width: 340 }}
                    onChange={(e) => setInput(e.target.value)} onPressEnter={() => setTrace(input.trim())} />
                <Button type="primary" onClick={() => setTrace(input.trim())}>{t('obs.journey.open')}</Button>
            </Space>
            {j.error && <NotConfigured error={j.error} />}
            {j.data && (
                <Surface>
                    <Space wrap style={{ marginBottom: 8 }}>
                        <strong>{j.data.trace_id}</strong>
                        {j.data.source === 'tempo' && (
                            <Muted>{t('obs.journey.summary', { spans: j.data.spans.length, services: j.data.services.length, ms: fmt(j.data.duration_ms) })}</Muted>
                        )}
                        {j.data.links.trace && <a href={j.data.links.trace} target="_blank" rel="noreferrer">{t('obs.journey.linkOut', { vendor: 'your backend' })}</a>}
                        <Button size="small" onClick={() => showLogs(j.data!.trace_id)}>{t('obs.journey.logs')}</Button>
                    </Space>
                    {j.data.spans.length > 0 && <Waterfall j={j.data} />}
                </Surface>
            )}
            <section aria-label={t('obs.journey.recent')}>
                <SectionTitle>{t('obs.journey.recent')}</SectionTitle>
                <Space wrap style={{ marginBottom: 8 }}>
                    <Input aria-label={t('obs.journey.service')} placeholder={t('obs.journey.service')} defaultValue={service} style={{ width: 220 }}
                        onPressEnter={(e) => setService((e.target as HTMLInputElement).value.trim())} />
                    <Checkbox checked={errorsOnly} onChange={(e) => setErrorsOnly(e.target.checked)}>{t('obs.journey.errorsOnly')}</Checkbox>
                </Space>
                {recent.error ? <NotConfigured error={recent.error} /> : (
                    <Table size="small" rowKey="trace_id" pagination={false} loading={recent.loading} dataSource={recent.data?.items ?? []}
                        onRow={(r) => ({ onClick: () => { setInput(r.trace_id); setTrace(r.trace_id); } })}
                        columns={[
                            { title: t('obs.journey.traceId'), dataIndex: 'trace_id',
                                render: (v: string) => <Button type="link" size="small" onClick={() => setTrace(v)}>{v}</Button> },
                            { title: t('obs.journey.service'), dataIndex: 'root_service' },
                            { title: 'Operation', dataIndex: 'root_name' },
                            { title: 'ms', dataIndex: 'duration_ms' },
                            { title: 'Started', dataIndex: 'start_ms', render: (v: number) => new Date(v).toLocaleString() },
                        ]} />
                )}
            </section>
        </Space>
    );
}

// ---- logs -------------------------------------------------------------------------------------------------------
function LogsTab({ traceId, service }: { traceId: string; service: string }) {
    const t = useT();
    const { client } = useConsumerApi();
    const [form, setForm] = useState({ trace_id: traceId, service, q: '', level: '' });
    const [rows, setRows] = useState<LogLine[] | null>(null);
    const [error, setError] = useState<unknown>(null);
    const [busy, setBusy] = useState(false);
    const search = async (f = form) => {
        if (!f.trace_id && !f.service) {
            setError(null);
            message.info(t('obs.logs.scope'));
            return;
        }
        setBusy(true);
        try {
            const qs = new URLSearchParams(Object.entries(f).filter(([, v]) => v) as [string, string][]);
            setRows((await client.request<{ items: LogLine[] }>('GET', `/v1/observability/logs?${qs}`)).items);
            setError(null);
        } catch (e) {
            setError(e);
        } finally {
            setBusy(false);
        }
    };
    React.useEffect(() => {
        if (traceId || service) void search({ ...form, trace_id: traceId, service });
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [traceId, service]);
    return (
        <Space direction="vertical" style={{ width: '100%' }}>
            <Space wrap>
                <Input aria-label={t('obs.journey.traceId')} placeholder={t('obs.journey.traceId')} value={form.trace_id} style={{ width: 300 }}
                    onChange={(e) => setForm({ ...form, trace_id: e.target.value.trim() })} />
                <Input aria-label={t('obs.journey.service')} placeholder={t('obs.journey.service')} value={form.service} style={{ width: 180 }}
                    onChange={(e) => setForm({ ...form, service: e.target.value.trim() })} />
                <Input aria-label={t('obs.logs.text')} placeholder={t('obs.logs.text')} value={form.q} style={{ width: 200 }}
                    onChange={(e) => setForm({ ...form, q: e.target.value })} onPressEnter={() => search()} />
                <Select aria-label={t('obs.logs.level')} placeholder={t('obs.logs.level')} allowClear style={{ width: 130 }}
                    value={form.level || undefined} onChange={(v) => setForm({ ...form, level: v ?? '' })}
                    options={['ERROR', 'WARNING', 'INFO', 'DEBUG'].map((v) => ({ value: v, label: v }))} />
                <Button type="primary" loading={busy} onClick={() => search()}>{t('obs.logs.search')}</Button>
            </Space>
            {error !== null && <NotConfigured error={error} />}
            {rows !== null && (
                <section aria-label={t('obs.tab.logs')}><List size="small" bordered locale={{ emptyText: t('obs.logs.none') }} dataSource={rows}
                    renderItem={(r) => (
                        <List.Item>
                            <code style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                                <Muted>{new Date(r.at).toLocaleTimeString()}</Muted>{' '}
                                {r.level && <Tag color={/err|crit/i.test(r.level) ? 'red' : /warn/i.test(r.level) ? 'orange' : 'default'}>{r.level}</Tag>}
                                <Tag>{r.service}</Tag>{r.line}
                            </code>
                        </List.Item>
                    )} /></section>
            )}
        </Space>
    );
}

// ---- SLOs -------------------------------------------------------------------------------------------------------
function BurnChart({ id }: { id: string }) {
    const t = useT();
    const q = useDheQuery<{ burn_1h: { at: string; value: number | null }[] }>(`/v1/observability/slos/${id}/history`, (c, signal) =>
        c.request('GET', `/v1/observability/slos/${encodeURIComponent(id)}/history?hours=24`, undefined, { signal }));
    const pts = (q.data?.burn_1h ?? []).filter((p) => p.value !== null) as { at: string; value: number }[];
    if (q.error || pts.length < 2) return null;
    const max = Math.max(1.5, ...pts.map((p) => p.value));
    const d = pts.map((p, i) => `${(i / (pts.length - 1)) * 300},${60 - (p.value / max) * 56}`).join(' ');
    return (
        <figure style={{ margin: 0 }}>
            <figcaption><Muted>{t('obs.slos.history')}</Muted></figcaption>
            <svg width={300} height={62} role="img" aria-label={`${t('obs.slos.history')}: ${fmt(pts[pts.length - 1].value)}x`}>
                <line x1={0} x2={300} y1={60 - 56 / max} y2={60 - 56 / max} stroke="#57606a" strokeDasharray="3 3" />
                <polyline points={d} fill="none" stroke="#2f6feb" strokeWidth={2} />
            </svg>
        </figure>
    );
}

function SloDetail({ id }: { id: string }) {
    const t = useT();
    const q = useDheQuery<SloStatus>(`/v1/observability/slos/${id}/status`, (c, signal) =>
        c.request('GET', `/v1/observability/slos/${encodeURIComponent(id)}/status`, undefined, { signal }));
    if (q.error) return <NotConfigured error={q.error} />;
    const s = q.data;
    if (!s) return null;
    return (
        <Space direction="vertical">
            <Space wrap>
                <span>{t('obs.slos.sli')}: <strong>{s.sli === null ? '–' : `${s.sli}%`}</strong></span>
                <span>{t('obs.slos.budget')}: <strong>{s.budget_remaining === null ? '–' : `${pct(s.budget_remaining)}%`}</strong></span>
            </Space>
            {s.burn.map((b) => (
                <div key={b.long}>
                    {b.burning && <Tag color={SEVERITY_COLOR[b.severity]}>{t('obs.slos.burning')}</Tag>}
                    {t('obs.slos.burn', { long: b.long, short: b.short, a: fmt(b.long_burn), b: fmt(b.short_burn), factor: b.factor })}
                </div>
            ))}
            <BurnChart id={id} />
        </Space>
    );
}

function SlosTab({ startNew, onNewClosed }: { startNew: boolean; onNewClosed: () => void }) {
    const t = useT();
    const { client } = useConsumerApi();
    const q = useDheQuery<{ items: Slo[] }>('/v1/observability/slos', (c, signal) => c.request('GET', '/v1/observability/slos', undefined, { signal }));
    const [dialog, setDialog] = useState<{ kind: 'new' } | { kind: 'edit'; row: Slo } | null>(startNew ? { kind: 'new' } : null);
    const close = () => { setDialog(null); onNewClosed(); };
    return (
        <Space direction="vertical" style={{ width: '100%' }}>
            <Button type="primary" onClick={() => setDialog({ kind: 'new' })}>{t('obs.slos.new')}</Button>
            {q.error && <ErrorNotice error={q.error} area={t('obs.tab.slos')} docs={SPEC('observability-slos').docs} />}
            <Table size="small" rowKey="id" loading={q.loading} dataSource={q.data?.items ?? []} pagination={false}
                expandable={{ expandedRowRender: (r) => <SloDetail id={r.id} /> }}
                columns={[
                    { title: 'SLO', dataIndex: 'name' },
                    { title: t('obs.journey.service'), dataIndex: 'service' },
                    { title: 'Objective', dataIndex: 'objective', render: (v: number, r) => `${v}% / ${r.window}` },
                    { title: '', key: 'state', render: (_: unknown, r) => (r.last_evaluation?.error
                        ? <Tag color="orange" title={r.last_evaluation.error}>{t('obs.alerts.lastError', { error: '' }).trim()}</Tag>
                        : r.enabled ? null : <Tag>off</Tag>) },
                    { title: '', key: 'act', render: (_: unknown, r) => (
                        <Space>
                            <Button size="small" onClick={() => setDialog({ kind: 'edit', row: r })}>{t('obs.slos.edit')}</Button>
                            <Popconfirm title={`${t('obs.slos.delete')} ${r.name}?`} onConfirm={async () => {
                                try { await client.request('DELETE', `/v1/observability/slos/${encodeURIComponent(r.id)}`); q.refresh(); }
                                catch (e) { notifyError(e); }
                            }}><Button size="small" danger>{t('obs.slos.delete')}</Button></Popconfirm>
                        </Space>) },
                ]} />
            {dialog && (
                <ActionDialog spec={SPEC('observability-slos')} action={action('observability-slos', dialog.kind === 'new' ? 'New SLO' : 'Edit SLO')}
                    row={dialog.kind === 'edit' ? (dialog.row as unknown as Record<string, unknown>) : null}
                    onDone={() => { close(); q.refresh(); }} onClose={close} />
            )}
        </Space>
    );
}

// ---- alerts, rules, silences ----------------------------------------------------------------------------------
function AlertsTab({ startNew, onNewClosed, openIncident }: { startNew: string; onNewClosed: () => void; openIncident: (id: string) => void }) {
    const t = useT();
    const { client } = useConsumerApi();
    const [state, setState] = useState('open');
    const alerts = useDheQuery<{ items: AlertRow[] }>(`/v1/observability/alerts?${state}`, (c, signal) =>
        c.request('GET', `/v1/observability/alerts?state=${state}`, undefined, { signal }), { pollMs: 30_000 });
    const rules = useDheQuery<{ items: Rule[] }>('/v1/observability/alert-rules', (c, signal) => c.request('GET', '/v1/observability/alert-rules', undefined, { signal }));
    const silences = useDheQuery<{ items: Silence[] }>('/v1/observability/silences', (c, signal) => c.request('GET', '/v1/observability/silences', undefined, { signal }));
    const [dialog, setDialog] = useState<{ spec: string; label: string; row?: Record<string, unknown> } | null>(
        startNew === 'rule' ? { spec: 'observability-alert-rules', label: 'New alert rule' }
            : startNew === 'silence' ? { spec: 'observability-silences', label: 'New silence' } : null);
    const close = () => { setDialog(null); onNewClosed(); };
    const ack = async (id: string) => {
        try { await client.request('POST', `/v1/observability/alerts/${encodeURIComponent(id)}/acknowledge`); alerts.refresh(); }
        catch (e) { notifyError(e); }
    };
    const evaluate = async () => {
        try { await client.request('POST', '/v1/observability/evaluate'); alerts.refresh(); rules.refresh(); }
        catch (e) { notifyError(e); }
    };
    const remove = async (path: string, reload: () => void) => {
        try { await client.request('DELETE', path); reload(); } catch (e) { notifyError(e); }
    };
    return (
        <Space direction="vertical" style={{ width: '100%' }} size="large">
            <Space wrap>
                <Select aria-label={t('obs.alerts.state')} value={state} onChange={setState} style={{ width: 150 }}
                    options={['open', 'firing', 'pending', 'resolved'].map((v) => ({ value: v, label: v }))} />
                <Button onClick={evaluate}>{t('obs.alerts.evaluate')}</Button>
            </Space>
            {alerts.error && <ErrorNotice error={alerts.error} area={t('obs.tab.alerts')} />}
            <Table size="small" rowKey="id" loading={alerts.loading} dataSource={alerts.data?.items ?? []} pagination={{ pageSize: 20 }}
                aria-label={t('obs.tab.alerts')}
                columns={[
                    { title: '', dataIndex: 'severity', render: (v: string) => <Tag color={SEVERITY_COLOR[v]}>{v}</Tag> },
                    { title: 'Alert', dataIndex: 'name' },
                    { title: t('obs.alerts.state'), dataIndex: 'state', render: (v: string, r) => (
                        <Space size={4} wrap><Tag>{v}</Tag>
                            {r.silenced_by && <Tag>{t('obs.alerts.silenced', { id: r.silenced_by })}</Tag>}
                            {r.acknowledged_by && <Muted>{t('obs.alerts.acknowledged', { who: actorName(r.acknowledged_by) })}</Muted>}
                        </Space>) },
                    { title: 'Summary', dataIndex: 'summary' },
                    { title: '', key: 'act', render: (_: unknown, r) => (
                        <Space>
                            {r.state === 'firing' && !r.acknowledged_by && <Button size="small" onClick={() => ack(r.id)}>{t('obs.alerts.acknowledge')}</Button>}
                            {r.incident_id && <Button size="small" type="link" onClick={() => openIncident(r.incident_id!)}>{t('obs.tab.incidents')}</Button>}
                        </Space>) },
                ]} />
            <section aria-label={t('obs.alerts.rules')}>
                <Space style={{ justifyContent: 'space-between', width: '100%' }}>
                    <SectionTitle>{t('obs.alerts.rules')}</SectionTitle>
                    <Button onClick={() => setDialog({ spec: 'observability-alert-rules', label: 'New alert rule' })}>{t('obs.alerts.newRule')}</Button>
                </Space>
                <Table size="small" rowKey="id" loading={rules.loading} dataSource={rules.data?.items ?? []} pagination={false}
                    columns={[
                        { title: 'Rule', dataIndex: 'name' },
                        { title: 'Language', dataIndex: 'language' },
                        { title: '', dataIndex: 'severity', render: (v: string) => <Tag color={SEVERITY_COLOR[v]}>{v}</Tag> },
                        { title: 'For', dataIndex: 'pending_for' },
                        { title: '', key: 'ev', render: (_: unknown, r) => (r.last_evaluation?.error
                            ? <Tag color="orange">{t('obs.alerts.lastError', { error: r.last_evaluation.error })}</Tag> : null) },
                        { title: '', key: 'act', render: (_: unknown, r) => (
                            <Space>
                                <Button size="small" onClick={() => setDialog({ spec: 'observability-alert-rules', label: 'Edit alert rule', row: r as unknown as Record<string, unknown> })}>{t('obs.slos.edit')}</Button>
                                <Popconfirm title={`${t('obs.slos.delete')} ${r.name}?`} onConfirm={() => remove(`/v1/observability/alert-rules/${encodeURIComponent(r.id)}`, rules.refresh)}>
                                    <Button size="small" danger>{t('obs.slos.delete')}</Button>
                                </Popconfirm>
                            </Space>) },
                    ]} />
            </section>
            <section aria-label={t('obs.alerts.silences')}>
                <Space style={{ justifyContent: 'space-between', width: '100%' }}>
                    <SectionTitle>{t('obs.alerts.silences')}</SectionTitle>
                    <Button onClick={() => setDialog({ spec: 'observability-silences', label: 'New silence' })}>{t('obs.alerts.newSilence')}</Button>
                </Space>
                <Table size="small" rowKey="id" loading={silences.loading} dataSource={silences.data?.items ?? []} pagination={false}
                    columns={[
                        { title: 'Silence', dataIndex: 'id' },
                        { title: 'Matches', key: 'm', render: (_: unknown, r) => r.matchers.map((m) => `${m.name}${m.regex ? '=~' : '='}${m.value}`).join(', ') },
                        { title: 'Comment', dataIndex: 'comment' },
                        { title: 'Until', dataIndex: 'ends_at', render: (v: string, r) => <Space>{new Date(v).toLocaleString()}{r.active && <Tag color="purple">active</Tag>}</Space> },
                        { title: '', key: 'act', render: (_: unknown, r) => (
                            <Popconfirm title={`${t('obs.slos.delete')} ${r.id}?`} onConfirm={() => remove(`/v1/observability/silences/${encodeURIComponent(r.id)}`, silences.refresh)}>
                                <Button size="small" danger>{t('obs.slos.delete')}</Button>
                            </Popconfirm>) },
                    ]} />
            </section>
            {dialog && (
                <ActionDialog spec={SPEC(dialog.spec)} action={action(dialog.spec, dialog.label)} row={dialog.row ?? null}
                    onDone={() => { close(); rules.refresh(); silences.refresh(); }} onClose={close} />
            )}
        </Space>
    );
}

// ---- incidents ----------------------------------------------------------------------------------------------
function IncidentDrawer({ id, onClose }: { id: string; onClose: () => void }) {
    const t = useT();
    const { client } = useConsumerApi();
    const q = useDheQuery<IncidentDetail>(`/v1/observability/incidents/${id}`, (c, signal) =>
        c.request('GET', `/v1/observability/incidents/${encodeURIComponent(id)}`, undefined, { signal }));
    const [note, setNote] = useState({ kind: 'note', text: '' });
    const [summary, setSummary] = useState('');
    const act = async (what: string, body?: unknown) => {
        try { await client.request('POST', `/v1/observability/incidents/${encodeURIComponent(id)}/${what}`, body); q.refresh(); }
        catch (e) { notifyError(e); }
    };
    const inc = q.data;
    return (
        <Drawer open width={780} onClose={onClose} title={inc ? t('obs.incidents.drawer', { title: inc.title }) : id}>
            {q.error && <ErrorNotice error={q.error} />}
            {inc && (
                <section aria-label={t('obs.incidents.drawer', { title: inc.title })}>
                    <Space wrap style={{ marginBottom: 12 }}>
                        <Tag color={SEVERITY_COLOR[inc.severity]}>{inc.severity}</Tag>
                        <Tag>{t(`obs.incidents.status.${inc.status}` as MessageKey)}</Tag>
                        {inc.services.map((s) => <Tag key={s}>{s}</Tag>)}
                        {inc.assignee && <Muted>{actorName(inc.assignee)}</Muted>}
                    </Space>
                    {inc.summary && <p>{inc.summary}</p>}
                    <Space wrap style={{ marginBottom: 16 }}>
                        {inc.status === 'open' && <Button onClick={() => act('acknowledge')}>{t('obs.incidents.acknowledge')}</Button>}
                        {inc.status !== 'resolved' && (
                            <>
                                <Input aria-label={t('obs.incidents.summary')} placeholder={t('obs.incidents.summary')} value={summary}
                                    onChange={(e) => setSummary(e.target.value)} style={{ width: 280 }} />
                                <Button type="primary" onClick={() => act('resolve', { summary })}>{t('obs.incidents.resolve')}</Button>
                            </>
                        )}
                        {inc.status === 'resolved' && <Button onClick={() => act('reopen')}>{t('obs.incidents.reopen')}</Button>}
                    </Space>
                    <SreIncidentPanel id={id} resolved={inc.status === 'resolved'} onChange={() => void q.refresh()} />
                    <SectionTitle>{t('obs.incidents.timeline')}</SectionTitle>
                    <section aria-label={t('obs.incidents.timeline')}>
                    <List size="small" dataSource={inc.timeline}
                        renderItem={(e) => (
                            <List.Item>
                                <Space align="start">
                                    <Muted style={{ minWidth: 150 }}>{new Date(e.at).toLocaleString()}</Muted>
                                    <Tag>{e.kind}</Tag>
                                    <span><span style={{ whiteSpace: 'pre-wrap' }}>{e.text}</span>
                                        {e.by_agent && <Tag color="purple" style={{ marginLeft: 6 }}>{t('obs.incidents.agent')}</Tag>}
                                        <Muted> · {actorName(e.actor)}</Muted>
                                        {e.detail?.evidence?.length ? <EvidenceList items={e.detail.evidence} /> : null}</span>
                                </Space>
                            </List.Item>
                        )} />
                    </section>
                    <SectionTitle>{t('obs.incidents.note')}</SectionTitle>
                    <Space.Compact style={{ width: '100%' }}>
                        <Select aria-label={t('obs.incidents.noteKind')} value={note.kind} style={{ width: 130 }}
                            onChange={(v) => setNote({ ...note, kind: v })}
                            options={['note', 'finding', 'hypothesis', 'action'].map((v) => ({ value: v, label: v }))} />
                        <Input aria-label={t('obs.incidents.noteText')} placeholder={t('obs.incidents.noteText')} value={note.text}
                            onChange={(e) => setNote({ ...note, text: e.target.value })} />
                        <Button disabled={!note.text.trim()} onClick={async () => { await act('notes', note); setNote({ kind: 'note', text: '' }); }}>
                            {t('obs.incidents.add')}
                        </Button>
                    </Space.Compact>
                </section>
            )}
        </Drawer>
    );
}

function IncidentsTab({ open }: { open: (id: string) => void }) {
    const t = useT();
    const { client } = useConsumerApi();
    const [status, setStatus] = useState('active');
    const q = useDheQuery<{ items: Incident[] }>(`/v1/observability/incidents?${status}`, (c, signal) =>
        c.request('GET', `/v1/observability/incidents?status=${status}`, undefined, { signal }), { pollMs: 30_000 });
    const [form, setForm] = useState<{ title: string; severity: string; services: string } | null>(null);
    const create = async () => {
        if (!form) return;
        try {
            const inc = await client.request<Incident>('POST', '/v1/observability/incidents', {
                title: form.title, severity: form.severity, services: form.services.split(',').map((s) => s.trim()).filter(Boolean) });
            setForm(null);
            q.refresh();
            open(inc.id);
        } catch (e) {
            notifyError(e);
        }
    };
    return (
        <Space direction="vertical" style={{ width: '100%' }}>
            <Space wrap>
                <Select aria-label="Status" value={status} onChange={setStatus} style={{ width: 160 }}
                    options={['active', 'open', 'acknowledged', 'resolved'].map((v) => ({ value: v, label: v }))} />
                <Button onClick={() => setForm({ title: '', severity: 'warning', services: '' })}>{t('obs.incidents.open')}</Button>
            </Space>
            {form && (
                <Surface>
                    <Space wrap>
                        <Input aria-label={t('obs.incidents.title')} placeholder={t('obs.incidents.title')} value={form.title} style={{ width: 280 }}
                            onChange={(e) => setForm({ ...form, title: e.target.value })} />
                        <Select aria-label={t('obs.incidents.severity')} value={form.severity} style={{ width: 120 }}
                            onChange={(v) => setForm({ ...form, severity: v })}
                            options={['critical', 'warning', 'info'].map((v) => ({ value: v, label: v }))} />
                        <Input aria-label={t('obs.incidents.services')} placeholder={t('obs.incidents.services')} value={form.services} style={{ width: 200 }}
                            onChange={(e) => setForm({ ...form, services: e.target.value })} />
                        <Button type="primary" disabled={!form.title.trim()} onClick={create}>{t('obs.incidents.open')}</Button>
                    </Space>
                </Surface>
            )}
            {q.error && <ErrorNotice error={q.error} area={t('obs.tab.incidents')} />}
            <Table size="small" rowKey="id" loading={q.loading} dataSource={q.data?.items ?? []} pagination={{ pageSize: 20 }}
                columns={[
                    { title: '', dataIndex: 'severity', render: (v: string) => <Tag color={SEVERITY_COLOR[v]}>{v}</Tag> },
                    { title: t('obs.incidents.title'), dataIndex: 'title',
                        render: (v: string, r) => <Button type="link" size="small" aria-label={`Open ${v}`} onClick={() => open(r.id)}>{v}</Button> },
                    { title: 'Status', dataIndex: 'status', render: (v: string) => <Tag>{t(`obs.incidents.status.${v}` as MessageKey)}</Tag> },
                    { title: t('obs.incidents.services'), dataIndex: 'services', render: (v: string[]) => v.join(', ') },
                    { title: 'Opened', dataIndex: 'opened_at', render: (v: string) => new Date(v).toLocaleString() },
                ]} />
        </Space>
    );
}

// ---- the page --------------------------------------------------------------------------------------------------
export function ObservabilityPage() {
    const t = useT();
    const [tab, setTab] = useSearchParam('tab');
    const [trace] = useSearchParam('trace');
    const [service] = useSearchParam('service');
    const [incident, setIncident] = useSearchParam('incident');
    const [isNew, setNew] = useSearchParam('new');
    const [pipeline] = useSearchParam('pipeline');
    const [pipeRun] = useSearchParam('run');
    const active = (TABS as readonly string[]).includes(tab) ? tab : 'health';
    const ov = useDheQuery<Overview>('/v1/observability/overview', (c, signal) =>
        c.request('GET', '/v1/observability/overview', undefined, { signal }), { pollMs: 30_000 });
    if (ov.error && errorStatus(ov.error) === 403) {
        return <Page><PageHeader title={t('obs.title')} icon="pulse" help={pageHelp('/observability')} /><Alert type="warning" showIcon message={t('obs.forbidden')} /></Page>;
    }
    const o = ov.data;
    const firing = o ? o.firing.critical + o.firing.warning + o.firing.info : 0;
    const backends = o ? (['prometheus', 'loki', 'tempo'] as const).filter((b) => o.backends[b]) : [];
    return (
        <Page>
            <PageHeader title={t('obs.title')} description={t('obs.description')} icon="pulse" help={pageHelp('/observability')}>
                {o && (
                    <Space wrap>
                        <Tag color={firing ? 'red' : 'green'}>{t('obs.firing', { n: firing })}</Tag>
                        <Tag color={o.open_incidents ? 'orange' : 'default'}>{t('obs.openIncidents', { n: o.open_incidents })}</Tag>
                        <Muted>{backends.length ? t('obs.backends', { list: backends.join(', ') }) : t('obs.backends.none')}</Muted>
                        <Link to="/dashboards?open=ops-platform-overview">{t('obs.dashboards')}</Link>
                    </Space>
                )}
            </PageHeader>
            <Tabs
                activeKey={active}
                onChange={(k) => setTab(k, { new: null })}
                items={[
                    { key: 'health', label: t('obs.tab.health'),
                        children: active === 'health' ? <HealthTab openTraces={(s) => setTab('journey', { service: s, trace: null })} /> : null },
                    { key: 'journey', label: t('obs.tab.journey'),
                        children: active === 'journey' ? <JourneyTab key={trace} initial={trace} service={service}
                            showLogs={(id) => setTab('logs', { trace: id })} /> : null },
                    { key: 'logs', label: t('obs.tab.logs'), children: active === 'logs' ? <LogsTab traceId={trace} service={service} /> : null },
                    { key: 'pipelines', label: t('obs.tab.pipelines'),
                        children: active === 'pipelines' ? <PipelinesTab pipeline={pipeline} run={pipeRun}
                            select={(p, r) => setTab('pipelines', { pipeline: p, run: r ?? null })}
                            openTrace={(id) => setTab('journey', { trace: id, pipeline: null, run: null })} /> : null },
                    { key: 'slos', label: t('obs.tab.slos'),
                        children: active === 'slos' ? <SlosTab startNew={isNew === 'slo'} onNewClosed={() => setNew(null)} /> : null },
                    { key: 'alerts', label: t('obs.tab.alerts'),
                        children: active === 'alerts' ? <AlertsTab startNew={isNew} onNewClosed={() => setNew(null)}
                            openIncident={(id) => setIncident(id)} /> : null },
                    { key: 'incidents', label: t('obs.tab.incidents'),
                        children: active === 'incidents' ? <IncidentsTab open={(id) => setIncident(id)} /> : null },
                    { key: 'export', label: t('obs.tab.export'), children: active === 'export' ? <ExportTab /> : null },
                ]}
            />
            {incident && <IncidentDrawer id={incident} onClose={() => setIncident(null)} />}
        </Page>
    );
}
