import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useHistory, useLocation } from 'react-router-dom';

import { useConsumerApi } from '../api/hooks';
import { Icon } from '../components/Icons';
import { AirflowTab } from '../flows/AirflowTab';
import { FlowApproval, FlowDetail, FlowSummary, flowsApi } from '../flows/api';
import { FlowCanvas } from '../flows/FlowCanvas';
import { FlowEditor, withLayout } from '../flows/FlowEditor';
import { positions } from '../flows/layout';
import { FLOW_NAME, FlowDef, Problem } from '../flows/model';
import { FlowsRoot } from '../flows/styles';
import { TEMPLATES } from '../flows/templates';
import { describeError } from '../components/ErrorNotice';
import { Embedded, PageHeader } from '../guide/PageHeader';
import { AirflowDeploymentsView } from '../resources/AirflowDeployments';
import { RESOURCE_SPECS } from '../resources/catalog';
import { ResourceView } from '../resources/ResourceView';
import { SqlWorkflowsPage } from './SqlWorkflowsPage';
import { pageHelp } from '../guide/help';
import { StatusBadge } from '../components/StatusBadge';
import { useTheme } from '../utils/theme';


/** Orchestration (L149): flows, SQL workflows, scheduled jobs and Airflow (managed deployments and DAGs) in one place. */
const FLOW_TABS = ['flows', 'sql-workflows', 'jobs', 'airflow'] as const;
type FlowTab = (typeof FLOW_TABS)[number];
const FLOW_TAB_LABELS: [FlowTab, string][] = [['flows', 'Flows'], ['sql-workflows', 'SQL workflows'], ['jobs', 'Jobs'], ['airflow', 'Airflow']];

/** Cairn Flows: every flow you may see, a gallery to start one, and the canvas editor with its runs. */
export function FlowsPage() {
    const { client } = useConsumerApi();
    const api = useMemo(() => flowsApi(client), [client]);
    const history = useHistory();
    const location = useLocation();
    const query = new URLSearchParams(location.search);
    const current = query.get('flow');
    const tab = (FLOW_TABS as readonly string[]).includes(query.get('tab') ?? '') ? (query.get('tab') as FlowTab) : 'flows';
    const change = query.get('change');
    const preview = query.get('preview');
    const [flows, setFlows] = useState<FlowSummary[] | null>(null);
    const [approvals, setApprovals] = useState<FlowApproval[]>([]);
    const [detail, setDetail] = useState<FlowDetail | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [gallery, setGallery] = useState(() => query.get('new') === '1');      // ?new=1: the command palette's New flow
    const [me, setMe] = useState('');
    // The theme is chosen in the profile menu (L150); the canvas follows it.
    const [theme] = useTheme();
    const [filter, setFilter] = useState('');

    const load = useCallback(async () => {
        try {
            setFlows(await api.list());
            setApprovals(await api.approvals().catch(() => []));
        } catch (e) {
            setError(describeError(e));
        }
    }, [api]);

    useEffect(() => {
        void load();
        client.request<{ urn: string }>('GET', '/me').then((m) => setMe(m.urn)).catch(() => setMe(''));
    }, [load, client]);

    useEffect(() => {
        if (!current) {
            setDetail(null);
            return;
        }
        api.get(current).then(setDetail).catch((e) => setError(describeError(e)));
    }, [api, current]);

    const open = (name: string | null) => history.push(name ? `/flows?flow=${encodeURIComponent(name)}` : '/flows');
    const showTab = (t: string) => history.push(t === 'flows' ? '/flows' : `/flows?tab=${t}`);
    const downloadDag = async (name: string) => {
        try {
            const text = await client.request<string>('GET', `/v1/flows/${encodeURIComponent(name)}/airflow`);
            const url = URL.createObjectURL(new Blob([text], { type: 'text/x-python' }));
            const a = document.createElement('a');
            a.href = url;
            a.download = `${name}.py`;
            a.click();
            URL.revokeObjectURL(url);
        } catch (e) {
            setError(describeError(e));
        }
    };

    if (change && preview) {
        return (
            <FlowsRoot data-theme={theme}>
                <ProposedFlow client={client} change={change} flow={preview} onBack={() => history.push('/flows')} />
            </FlowsRoot>
        );
    }
    return (
        <FlowsRoot data-theme={theme}>
            {detail ? (
                <>
                    <div className="fl-top" style={{ paddingTop: 8, paddingBottom: 8 }}>
                        <button type="button" className="fl-btn" onClick={() => open(null)}>{'←'} All flows</button>
                        <span className="fl-muted">Owner {detail.owner.split(':').pop()} {'·'} you are {detail.role}</span>
                        <span style={{ flex: 1 }} />
                        <button type="button" className="fl-btn" title="This flow as an Airflow DAG file (no secrets)" onClick={() => void downloadDag(detail.name)}>
                            Airflow DAG
                        </button>
                    </div>
                    <FlowEditor
                        detail={detail}
                        api={api}
                        me={me}
                        theme={theme}
                        onSaved={(d) => {
                            setDetail(d);
                            void load();
                        }}
                        onDeleted={() => {
                            open(null);
                            void load();
                        }}
                    />
                </>
            ) : (
                <>
                    <div style={{ padding: '20px 24px 0' }}>
                        <PageHeader title="Flows" icon="flow" help={pageHelp('/flows')}
                            description="Orchestrate SQL, tables, notebooks, Spark, dbt, agents, approvals and more as one DAG, run as its owner; SQL workflows, scheduled jobs and Airflow are here too. Every run is in Runs."
                            actions={
                                <>
                                    <button type="button" className="fl-btn primary" onClick={() => setGallery(true)}>
                                        <Icon name="plus" size={14} /> New flow
                                    </button>
                                </>
                            } />
                    </div>
                    <div className="fl-tabs" role="tablist" aria-label="Orchestration" style={{ padding: '0 24px' }}>
                        {FLOW_TAB_LABELS.map(([k, label]) => (
                            <button key={k} type="button" role="tab" aria-selected={tab === k} className="fl-tab" onClick={() => showTab(k)}>{label}</button>
                        ))}
                    </div>
                    {tab === 'airflow' ? (
                        <div className="fl-pane" style={{ maxWidth: 1280 }}>
                            <section aria-label="Airflow deployments" style={{ marginBottom: 24 }}>
                                <h2 style={{ fontSize: 16 }}>Managed Airflow deployments</h2>
                                <AirflowDeploymentsView />
                            </section>
                            <section aria-label="Airflow DAGs">
                                <h2 style={{ fontSize: 16 }}>DAGs</h2>
                                <AirflowTab client={client} theme={theme} onOpenFlow={(name) => { void load(); open(name); }} />
                            </section>
                        </div>
                    ) : tab === 'sql-workflows' ? (
                        <div className="fl-pane" style={{ maxWidth: 1280 }}>
                            <p className="fl-muted">A SQL workflow is a schedule of derived-table refreshes, checks and queries; it runs through the same executors as flows (<code>POST /v1/flows/from-sql-workflow/&lt;id&gt;</code> makes one a flow).</p>
                            <Embedded><SqlWorkflowsPage /></Embedded>
                        </div>
                    ) : tab === 'jobs' ? (
                        <div className="fl-pane" style={{ maxWidth: 1280 }}>
                            <ResourceView spec={RESOURCE_SPECS.jobs} />
                        </div>
                    ) : (
                    <div className="fl-pane" style={{ maxWidth: 1280 }}>
                        {error && <div className="fl-err" role="alert">{error}</div>}
                        {approvals.length > 0 && (
                            <section className="fl-card" style={{ marginBottom: 16 }} aria-label="Waiting for your approval">
                                <strong>Waiting for your approval</strong>
                                <ul className="fl-list">
                                    {approvals.map((a) => (
                                        <li key={a.run + a.step}>
                                            <button type="button" className="fl-list-item" onClick={() => open(a.flow)}>
                                                <span className="fl-badge waiting">approval</span> {a.flow} / {a.step}
                                                <span className="fl-muted">{a.message}</span>
                                            </button>
                                        </li>
                                    ))}
                                </ul>
                            </section>
                        )}
                        {gallery && <Gallery me={me} onClose={() => setGallery(false)} onCreate={async (def) => {
                            const d = await api.create(withLayout(def));
                            setGallery(false);
                            await load();
                            open(d.name);
                        }} />}
                        <label htmlFor="fl-filter" className="fl-sr">Filter flows</label>
                        <input id="fl-filter" className="fl-code" style={{ minHeight: 0, maxWidth: 360, marginBottom: 12 }} placeholder="Filter flows"
                            value={filter} onChange={(e) => setFilter(e.target.value)} />
                        {!flows ? <p className="fl-muted">Loading flows{'…'}</p> : flows.length === 0 ? (
                            <div className="fl-card">
                                <p>No flows yet. Start from a template: a nightly refresh with a check, an ingest that tells the team, or an agent that triages with an approval.</p>
                                <button type="button" className="fl-btn primary" onClick={() => setGallery(true)}>New flow</button>
                            </div>
                        ) : (
                            <table className="fl-table fl-card" style={{ padding: 0 }}>
                                <caption className="fl-sr">Flows</caption>
                                <thead>
                                    <tr><th>Flow</th><th>Version</th><th>Last run</th><th>Next run</th><th>Owner</th><th>Your role</th></tr>
                                </thead>
                                <tbody>
                                    {flows.filter((f) => !filter || f.name.includes(filter.toLowerCase()) || f.description.toLowerCase().includes(filter.toLowerCase())).map((f) => (
                                        <tr key={f.id}>
                                            <td>
                                                <button type="button" className="fl-list-item" style={{ padding: 0, fontWeight: 600 }} onClick={() => open(f.name)}>{f.name}</button>
                                                <div className="fl-muted">{f.description || `${f.steps} steps`}{f.draft_errors ? ` · draft has ${f.draft_errors} problem(s)` : ''}</div>
                                            </td>
                                            <td>{f.published_version ? `v${f.published_version}` : 'draft'}{!f.enabled ? ' (off)' : ''}</td>
                                            <td>{f.last_run ? <StatusBadge state={f.last_run.status} /> : <span className="fl-muted">{'—'}</span>}</td>
                                            <td>{f.next_run_ms ? new Date(f.next_run_ms).toLocaleString() : <span className="fl-muted">{'—'}</span>}</td>
                                            <td>{f.owner.split(':').pop()}</td>
                                            <td>{f.role}</td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        )}
                    </div>
                    )}
                </>
            )}
        </FlowsRoot>
    );
}

interface ChangeForPreview {
    id: string;
    title: string;
    status: string;
    sponsor: string;
    agent: string | null;
    plan: { changes: { action: string; kind: string; key: string; fields: { field: string; after: unknown }[] }[] };
}

const NO_PROBLEMS = new Map<string, Problem[]>();

/** A flow a change request proposes (an agent's draft), on the canvas, read-only until the change is approved. */
function ProposedFlow({ client, change, flow, onBack }: { client: ReturnType<typeof useConsumerApi>['client']; change: string; flow: string; onBack: () => void }) {
    const [cr, setCr] = useState<ChangeForPreview | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [selected, setSelected] = useState<string[]>([]);
    useEffect(() => {
        client.request<ChangeForPreview>('GET', `/v1/iac/changes/${encodeURIComponent(change)}`).then(setCr).catch((e) => setError(describeError(e)));
    }, [client, change]);
    const item = cr?.plan.changes.find((c) => c.kind === 'flows' && c.key === flow);
    const spec = item?.fields.find((f) => f.field === 'spec')?.after as FlowDef | undefined;
    const steps = useMemo(() => spec?.steps ?? [], [spec]);
    const pos = useMemo(() => positions(steps, spec?.layout), [steps, spec]);
    return (
        <div className="fl-pane" style={{ maxWidth: 1280 }}>
            <div className="fl-top" style={{ paddingLeft: 0 }}>
                <button type="button" className="fl-btn" onClick={onBack}>{'←'} All flows</button>
                <strong>{flow}</strong>
                <span className="fl-badge waiting">proposed draft</span>
            </div>
            {error && <div className="fl-err" role="alert">{error}</div>}
            {cr && !spec && <div className="fl-err" role="alert">The change {cr.title} does not propose a flow named {flow}.</div>}
            {cr && spec && (
                <>
                    <p className="fl-muted">
                        Proposed in the change request <Link to={`/settings?tab=iac&change=${encodeURIComponent(cr.id)}`}>{cr.title}</Link>
                        {cr.agent ? ` by the agent ${cr.agent}` : ''} for {cr.sponsor.split(':').pop()} ({cr.status}). Once someone approves and
                        applies it, the flow is created and opens here to edit.
                    </p>
                    <div style={{ height: 460, position: 'relative' }}>
                        <FlowCanvas steps={steps} pos={pos} selected={selected} selectedEdge={null} problems={NO_PROBLEMS} readOnly snapOn={false}
                            onSelect={(names) => setSelected(names)} onMove={() => undefined} onConnect={() => undefined} onDropType={() => undefined} />
                    </div>
                </>
            )}
        </div>
    );
}

function Gallery({ me, onClose, onCreate }: { me: string; onClose: () => void; onCreate: (def: ReturnType<(typeof TEMPLATES)[number]['make']>) => Promise<void> }) {
    const [name, setName] = useState('');
    const [picked, setPicked] = useState(TEMPLATES[0].id);
    const [err, setErr] = useState<string | null>(null);
    const valid = FLOW_NAME.test(name);
    return (
        <section className="fl-card" style={{ marginBottom: 16 }} aria-label="New flow">
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <strong style={{ flex: 1 }}>Start from a template</strong>
                <button type="button" className="fl-btn" onClick={onClose}>Close</button>
            </div>
            <div className="fl-gallery" role="radiogroup" aria-label="Templates" style={{ margin: '12px 0' }}>
                {TEMPLATES.map((t) => (
                    <button
                        key={t.id}
                        type="button"
                        role="radio"
                        aria-checked={picked === t.id}
                        className="fl-card fl-pal-item"
                        style={{ display: 'block', cursor: 'pointer', borderColor: picked === t.id ? 'var(--fl-primary)' : undefined }}
                        onClick={() => setPicked(t.id)}
                    >
                        <div style={{ fontWeight: 650, marginBottom: 4 }}>{t.title}</div>
                        <div className="fl-muted">{t.blurb}</div>
                        <div className="fl-muted" style={{ marginTop: 6 }}>{t.make('x', me || 'urn:li:corpuser:me').steps.map((s) => s.name).join(' → ')}</div>
                    </button>
                ))}
            </div>
            <label className="fl-field" htmlFor="fl-new-name">
                <span>Name (lowercase letters, digits, - and _)</span>
                <input id="fl-new-name" value={name} onChange={(e) => setName(e.target.value)} aria-invalid={!!name && !valid} placeholder="nightly-sales" />
            </label>
            {err && <div className="fl-err" role="alert">{err}</div>}
            <button type="button" className="fl-btn primary" disabled={!valid} onClick={async () => {
                try {
                    await onCreate(TEMPLATES.find((t) => t.id === picked)!.make(name, me || 'urn:li:corpuser:me'));
                } catch (e) {
                    setErr(describeError(e));
                }
            }}>Create draft</button>
        </section>
    );
}
