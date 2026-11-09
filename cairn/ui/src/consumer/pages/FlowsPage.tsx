import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useHistory, useLocation } from 'react-router-dom';

import { useConsumerApi } from '../api/hooks';
import { Icon } from '../components/Icons';
import { FlowApproval, FlowDetail, FlowSummary, flowsApi } from '../flows/api';
import { FlowEditor, withLayout } from '../flows/FlowEditor';
import { FLOW_NAME } from '../flows/model';
import { FlowsRoot } from '../flows/styles';
import { TEMPLATES } from '../flows/templates';

const THEME_KEY = 'cairn.flows.theme';

function readTheme(): string {
    try {
        return window.localStorage.getItem(THEME_KEY) || 'light';
    } catch {
        return 'light';
    }
}

/** Cairn Flows: every flow you may see, a gallery to start one, and the canvas editor with its runs. */
export function FlowsPage() {
    const { client } = useConsumerApi();
    const api = useMemo(() => flowsApi(client), [client]);
    const history = useHistory();
    const location = useLocation();
    const query = new URLSearchParams(location.search);
    const current = query.get('flow');
    const [flows, setFlows] = useState<FlowSummary[] | null>(null);
    const [approvals, setApprovals] = useState<FlowApproval[]>([]);
    const [detail, setDetail] = useState<FlowDetail | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [gallery, setGallery] = useState(false);
    const [me, setMe] = useState('');
    const [theme, setTheme] = useState(readTheme);
    const [filter, setFilter] = useState('');

    const load = useCallback(async () => {
        try {
            setFlows(await api.list());
            setApprovals(await api.approvals().catch(() => []));
        } catch (e) {
            setError((e as Error).message);
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
        api.get(current).then(setDetail).catch((e) => setError((e as Error).message));
    }, [api, current]);

    const open = (name: string | null) => history.push(name ? `/flows?flow=${encodeURIComponent(name)}` : '/flows');
    const changeTheme = (t: string) => {
        setTheme(t);
        try {
            window.localStorage.setItem(THEME_KEY, t);
        } catch {
            /* a preference only */
        }
    };

    return (
        <FlowsRoot data-theme={theme}>
            {detail ? (
                <>
                    <div className="fl-top" style={{ paddingTop: 8, paddingBottom: 8 }}>
                        <button type="button" className="fl-btn" onClick={() => open(null)}>{'←'} All flows</button>
                        <span className="fl-muted">Owner {detail.owner.split(':').pop()} {'·'} you are {detail.role}</span>
                    </div>
                    <FlowEditor
                        detail={detail}
                        api={api}
                        me={me}
                        theme={theme}
                        onTheme={changeTheme}
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
                    <div className="fl-top">
                        <Icon name="flow" size={22} />
                        <h1 className="fl-title">Flows</h1>
                        <span className="fl-muted">Orchestrate SQL, tables, notebooks, Spark, dbt, agents, approvals and more as one DAG, run as its owner.</span>
                        <span style={{ flex: 1 }} />
                        <select aria-label="Theme" className="fl-btn" value={theme} onChange={(e) => changeTheme(e.target.value)}>
                            <option value="light">Light</option>
                            <option value="dark">Dark</option>
                            <option value="auto">System</option>
                        </select>
                        <Link to="/sql/workflows" className="fl-btn">SQL workflows</Link>
                        <button type="button" className="fl-btn primary" onClick={() => setGallery(true)}>
                            <Icon name="plus" size={14} /> New flow
                        </button>
                    </div>
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
                                            <td>{f.last_run ? <span className={`fl-badge ${f.last_run.status}`}>{f.last_run.status}</span> : <span className="fl-muted">{'—'}</span>}</td>
                                            <td>{f.next_run_ms ? new Date(f.next_run_ms).toLocaleString() : <span className="fl-muted">{'—'}</span>}</td>
                                            <td>{f.owner.split(':').pop()}</td>
                                            <td>{f.role}</td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        )}
                    </div>
                </>
            )}
        </FlowsRoot>
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
                    setErr((e as Error).message);
                }
            }}>Create draft</button>
        </section>
    );
}
