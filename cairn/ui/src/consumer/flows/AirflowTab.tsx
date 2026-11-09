import React, { useCallback, useEffect, useMemo, useState } from 'react';

import type { ConsumerClient } from '../api/client';
import { FlowCanvas } from './FlowCanvas';
import { positions } from './layout';
import type { FlowStep, Problem } from './model';
import { describeError } from '../components/ErrorNotice';

/** One of Airflow's DAGs as Cairn lists it (GET /v1/airflow/dags): only those that are the person's. */
export interface AirflowDagSummary {
    dag_id: string;
    description: string;
    owners: string[];
    tags: string[];
    is_paused: boolean;
    schedule: string | null;
    schedule_text: string | null;
    has_import_errors: boolean;
    cairn_flow: string | null;
    last_run: { dag_run_id: string; state: string | null; start_date: string | null; end_date: string | null; run_type: string | null } | null;
}

export interface AirflowBlocker {
    task: string | null;
    operator?: string;
    reason: string;
}

/** A DAG with its tasks as canvas steps, recent runs and the chosen run's task states. */
export interface AirflowDagDetail {
    dag_id: string;
    description: string;
    owners: string[];
    tags: string[];
    is_paused: boolean;
    schedule: string | null;
    schedule_text: string | null;
    timezone: string | null;
    file: string | null;
    steps: (FlowStep & { operator: string; trigger_rule: string | null })[];
    runs: { dag_run_id: string; state: string | null; run_type: string | null; start_date: string | null; end_date: string | null }[];
    run: string | null;
    run_state: string | null;
    states: Record<string, string | null>;
    statuses: Record<string, string>;
    conversion: { convertible: boolean; blockers: AirflowBlocker[]; notes: string[]; flow: unknown };
}

const enc = encodeURIComponent;
const LIVE = ['queued', 'running', 'scheduled', 'restarting', 'up_for_retry', 'deferred'];
const NO_PROBLEMS = new Map<string, Problem[]>();

export function airflowApi(client: ConsumerClient) {
    return {
        list: () => client.request<AirflowDagSummary[]>('GET', '/v1/airflow/dags'),
        get: (id: string, run?: string | null) =>
            client.request<AirflowDagDetail>('GET', `/v1/airflow/dags/${enc(id)}`, undefined, run ? { query: { run } } : undefined),
        convert: (id: string, name: string) =>
            client.request<{ flow: string; notes: string[] }>('POST', `/v1/airflow/dags/${enc(id)}/convert`, name ? { name } : {}),
    };
}

function badge(state: string | null | undefined) {
    const map: Record<string, string> = { success: 'succeeded', failed: 'failed', running: 'running', queued: 'queued' };
    return <span className={`fl-badge ${map[state ?? ''] ?? 'pending'}`}>{state ?? 'no runs'}</span>;
}

/** Airflow's DAGs that are yours, each as a read-only graph on the flow canvas with its live task states. */
export function AirflowTab({ client, onOpenFlow, theme }: { client: ConsumerClient; onOpenFlow: (name: string) => void; theme?: string }) {
    const api = useMemo(() => airflowApi(client), [client]);
    const [dags, setDags] = useState<AirflowDagSummary[] | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [current, setCurrent] = useState<string | null>(null);

    useEffect(() => {
        api.list().then(setDags).catch((e) => setError(describeError(e)));
    }, [api]);

    if (current) {
        return <AirflowDagView api={api} dagId={current} onBack={() => setCurrent(null)} onOpenFlow={onOpenFlow} theme={theme} />;
    }
    return (
        <section aria-label="Airflow DAGs">
            <p className="fl-muted" style={{ maxWidth: 820 }}>
                Airflow's DAGs that are yours: you own them, they are tagged with your team (<code>team:&lt;group&gt;</code>), or they
                run one of your flows. Each opens read-only on the canvas with its runs; a DAG of Cairn's operators converts to a draft flow.
            </p>
            {error && <div className="fl-err" role="alert">{error}</div>}
            {!dags && !error ? <p className="fl-muted">Loading Airflow's DAGs{'…'}</p> : dags && dags.length === 0 ? (
                <div className="fl-card"><p>No Airflow DAGs of yours. A DAG shows here when one of its owners is you or your group.</p></div>
            ) : dags && (
                <table className="fl-table fl-card" style={{ padding: 0 }}>
                    <caption className="fl-sr">Airflow DAGs</caption>
                    <thead>
                        <tr><th>DAG</th><th>Schedule</th><th>Last run</th><th>Owners</th><th>Tags</th></tr>
                    </thead>
                    <tbody>
                        {dags.map((d) => (
                            <tr key={d.dag_id}>
                                <td>
                                    <button type="button" className="fl-list-item" style={{ padding: 0, fontWeight: 600 }} onClick={() => setCurrent(d.dag_id)}>{d.dag_id}</button>
                                    <div className="fl-muted">
                                        {d.description || (d.cairn_flow ? `Runs the flow ${d.cairn_flow}` : '')}
                                        {d.is_paused ? ' · paused' : ''}{d.has_import_errors ? ' · import errors' : ''}
                                    </div>
                                </td>
                                <td>{d.schedule_text || d.schedule || <span className="fl-muted">{'—'}</span>}</td>
                                <td>{badge(d.last_run?.state)}</td>
                                <td>{d.owners.join(', ')}</td>
                                <td>{d.tags.join(', ')}</td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            )}
        </section>
    );
}

function AirflowDagView({ api, dagId, onBack, onOpenFlow, theme }: {
    api: ReturnType<typeof airflowApi>; dagId: string; onBack: () => void; onOpenFlow: (name: string) => void; theme?: string;
}) {
    const [detail, setDetail] = useState<AirflowDagDetail | null>(null);
    const [run, setRun] = useState<string | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [name, setName] = useState('');
    const [converting, setConverting] = useState(false);
    const [selected, setSelected] = useState<string[]>([]);

    const load = useCallback(async () => {
        try {
            setDetail(await api.get(dagId, run));
        } catch (e) {
            setError(describeError(e));
        }
    }, [api, dagId, run]);

    useEffect(() => {
        void load();
    }, [load]);

    const live = !!detail && LIVE.includes(detail.run_state ?? '');
    useEffect(() => {
        if (!live) return undefined;
        const t = window.setInterval(() => void load(), 4000);       // live task states while the run goes on
        return () => window.clearInterval(t);
    }, [live, load]);

    const pos = useMemo(() => positions(detail?.steps ?? []), [detail]);
    if (error) return <div className="fl-pane"><button type="button" className="fl-btn" onClick={onBack}>{'←'} Airflow DAGs</button><div className="fl-err" role="alert">{error}</div></div>;
    if (!detail) return <p className="fl-muted">Loading {dagId}{'…'}</p>;
    const picked = detail.steps.find((s) => s.name === selected[0]);
    const conv = detail.conversion;
    return (
        <section aria-label={`Airflow DAG ${detail.dag_id}`}>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', marginBottom: 8 }}>
                <button type="button" className="fl-btn" onClick={onBack}>{'←'} Airflow DAGs</button>
                <strong style={{ fontSize: 18 }}>{detail.dag_id}</strong>
                <span className="fl-badge pending" title="Shown from Airflow; change it there, or convert it">read-only</span>
                {detail.run && <span>Run {detail.run} {badge(detail.run_state)}</span>}
                <span className="fl-muted">{detail.schedule_text || detail.schedule || 'no schedule'} {'·'} owners {detail.owners.join(', ')}</span>
                <span style={{ flex: 1 }} />
                {detail.runs.length > 0 && (
                    <label className="fl-muted">
                        Run{' '}
                        <select aria-label="Run" className="fl-btn" value={detail.run ?? ''} onChange={(e) => setRun(e.target.value)}>
                            {detail.runs.map((r) => <option key={r.dag_run_id} value={r.dag_run_id}>{r.dag_run_id} ({r.state})</option>)}
                        </select>
                    </label>
                )}
            </div>
            <div className="fl-canvas-wrap" data-theme={theme} style={{ height: 420, position: 'relative' }}>
                <FlowCanvas
                    steps={detail.steps}
                    pos={pos}
                    selected={selected}
                    selectedEdge={null}
                    problems={NO_PROBLEMS}
                    statuses={detail.statuses}
                    readOnly
                    snapOn={false}
                    onSelect={(names) => setSelected(names)}
                    onMove={() => undefined}
                    onConnect={() => undefined}
                    onDropType={() => undefined}
                />
            </div>
            {picked && (
                <div className="fl-card" style={{ marginTop: 8 }} aria-label={`Task ${picked.name}`}>
                    <strong>{picked.name}</strong> <span className="fl-muted">{picked.operator}</span>
                    {' · '}state {detail.states[picked.name] ?? 'not run'}{' · '}retries {picked.retries ?? 0}
                    {picked.trigger_rule && picked.trigger_rule !== 'all_success' ? ` · ${picked.trigger_rule}` : ''}
                    {picked.after?.length ? <div className="fl-muted">after {picked.after.join(', ')}</div> : null}
                </div>
            )}
            <section className="fl-card" style={{ marginTop: 12 }} aria-label="Convert to a Cairn flow">
                <strong>Convert to a Cairn flow</strong>
                {conv.convertible ? (
                    <>
                        <p className="fl-muted">Every task maps to a flow step. The flow starts as a draft of yours: check it, then publish it.</p>
                        {conv.notes.map((n) => <p key={n} className="fl-muted">{n}</p>)}
                        <label className="fl-field" htmlFor="af-flow-name">
                            <span>Flow name (optional)</span>
                            <input id="af-flow-name" value={name} onChange={(e) => setName(e.target.value)} placeholder={detail.dag_id.toLowerCase()} />
                        </label>
                        <button type="button" className="fl-btn primary" disabled={converting} onClick={async () => {
                            setConverting(true);
                            try {
                                const out = await api.convert(detail.dag_id, name.trim());
                                onOpenFlow(out.flow);
                            } catch (e) {
                                setError(describeError(e));
                            } finally {
                                setConverting(false);
                            }
                        }}>Convert to Cairn flow</button>
                    </>
                ) : (
                    <>
                        <p className="fl-muted">It cannot be converted as it is:</p>
                        <ul aria-label="What blocks conversion">
                            {conv.blockers.map((b, i) => (
                                <li key={`${b.task}-${i}`}><strong>{b.task ?? 'the DAG'}</strong>{b.operator ? ` (${b.operator})` : ''}: {b.reason}</li>
                            ))}
                        </ul>
                    </>
                )}
            </section>
        </section>
    );
}
