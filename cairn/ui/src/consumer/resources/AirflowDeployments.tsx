import { Alert, Button, Empty, Input, Modal, Select, Space, Spin, Table, Typography, message } from 'antd';
import React, { useEffect, useState } from 'react';
import { useHistory, useLocation } from 'react-router-dom';
import styled from 'styled-components';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import { ErrorNotice, notifyError } from '../components/ErrorNotice';
import { StatusBadge, StatusTone } from '../components/StatusBadge';
import { Muted, tokens } from '../components/tokens';
import { useT } from '../i18n';
import { actorName } from '../utils/format';
import { RESOURCE_SPECS } from './catalog';
import { ActionDialog, createAction } from './ResourceView';
import { When } from '../utils/time';

/** A managed Airflow deployment as `GET /v1/airflow/deployments` lists it. */
export interface AirflowDeployment {
    name: string;
    runtime: string;
    state: string;
    environment: string;
    airflow_version: string;
    executor: string | null;
    team: string | null;
    url: string | null;
    image: string | null;
    custom_image: string | null;
    dags_release: string | null;
    pipeline_user: string;
    promotes_to: string | null;
    approval: { required: boolean } | null;
    hibernation: { awake: { days: string[]; from: string; to: string }[]; timezone: string } | null;
    error?: string | null;
}

export interface AirflowList {
    deployments: AirflowDeployment[];
    runtime: string | null;
    versions: string[];
    executors: string[];
}

interface Deploy {
    id: string; seq: number; kind: 'dags' | 'image'; state: string; image: string | null; sha256: string | null;
    files: number | null; git_sha: string | null; note: string | null; source: string | null; created_by: string;
    created_ms: number; checked: { dags?: string[]; import_errors?: Record<string, string> } | null; error: string | null;
}

interface Promotion {
    id: string; deploy: string; source: string; target: string; state: string; requested_by: string;
    requested_ms: number; decided_by: string | null; comment: string | null; error: string | null;
}

export const NEW_DEPLOYMENT = 'New deployment';
const BASE = '/v1/airflow/deployments';
const COMPONENTS = ['scheduler', 'api-server', 'dag-processor', 'triggerer'];
const ENV_ORDER = ['dev', 'test', 'stage', 'staging', 'qa', 'prod', 'production'];

/** The tone of a deployment's, deploy's or promotion's state. */
export function airflowTone(s?: string | null): StatusTone {
    if (s === 'ready' || s === 'healthy' || s === 'live' || s === 'applied') return 'ok';
    if (s === 'failed' || s === 'refused' || s === 'down' || s === 'rejected') return 'error';
    if (s === 'degraded') return 'warn';
    if (s === 'provisioning' || s === 'deploying' || s === 'upgrading') return 'running';
    return 'waiting';
}

/** Who and where a deploy came from, in words. */
export function deploySource(d: Pick<Deploy, 'source' | 'git_sha'>): string {
    const s = d.source ?? '';
    const sha = d.git_sha ? ` (git ${d.git_sha.slice(0, 7)})` : '';
    if (s.startsWith('rollback:')) return `rollback to ${s.slice(9)}${sha}`;
    if (s.startsWith('promoted:')) return `promoted from ${s.slice(9).split('/')[0]}${sha}`;
    if (s.startsWith('git:')) return `git folder${sha}`;
    return `deployed${sha}`;
}

/** When a deployment is awake, in words: "mon–fri 07:00–20:00 Europe/Berlin", or null when always on. */
export function awakeHours(h: AirflowDeployment['hibernation']): string | null {
    if (!h || !h.awake?.length) return null;
    const week = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];
    const days = (d: string[]) => {
        const at = d.map((x) => week.indexOf(x.toLowerCase()));
        const run = d.length > 2 && at[0] >= 0 && at.every((x, i) => x === at[0] + i);
        return run ? `${d[0]}–${d[d.length - 1]}` : d.join(', ');
    };
    return `${h.awake.map((w) => `${days(w.days)} ${w.from}–${w.to}`).join('; ')} ${h.timezone}`;
}

/** Deployments in promotion order: a team's dev before its stage before its prod, each chain together. */
export function promotionChains(deps: AirflowDeployment[]): { team: string; chain: AirflowDeployment[] }[] {
    const rank = (e: string) => {
        const i = ENV_ORDER.indexOf(e.toLowerCase());
        return i < 0 ? ENV_ORDER.length : i;
    };
    const teams = new Map<string, AirflowDeployment[]>();
    for (const d of deps) teams.set(d.team ?? '', [...(teams.get(d.team ?? '') ?? []), d]);
    return Array.from(teams.entries()).map(([team, list]) => ({
        team,
        chain: list.slice().sort((a, b) => rank(a.environment) - rank(b.environment) || a.name.localeCompare(b.name)),
    }));
}

const Cards = styled.div`
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    gap: 14px;
`;

const Env = styled.article<{ $selected: boolean }>`
    background: ${tokens.color.surface};
    border: ${(p) => (p.$selected ? `2px solid ${tokens.color.primary}` : `1px solid ${tokens.color.border}`)};
    padding: ${(p) => (p.$selected ? '15px' : '16px')};
    border-radius: ${tokens.radius.lg};
    display: flex;
    flex-direction: column;
    gap: 10px;
    > div:first-child {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 8px;
    }
    h3 {
        margin: 0;
        font-size: 16px;
    }
    h3 button {
        min-height: ${tokens.control}px;
        padding: 0;
        border: 0;
        background: transparent;
        font: inherit;
        font-family: ${tokens.fontMono};
        font-weight: 600;
        color: ${tokens.color.text};
        text-align: left;
        cursor: pointer;
        text-underline-offset: 5px;
        text-decoration-thickness: 2px;
    }
    h3 button[aria-pressed='true'] {
        text-decoration: underline;
    }
    h3 button:focus-visible {
        outline: 2px solid ${tokens.color.focus};
        outline-offset: 2px;
    }
    dl {
        margin: 0;
        display: grid;
        grid-template-columns: 96px minmax(0, 1fr);
        gap: 5px 8px;
        font-size: 13px;
    }
    dt {
        color: ${tokens.color.textMuted};
    }
    dd {
        margin: 0;
        overflow-wrap: anywhere;
    }
    .mono {
        font-family: ${tokens.fontMono};
    }
`;

const Panel = styled.section`
    background: ${tokens.color.surface};
    border: 1px solid ${tokens.color.border};
    border-radius: ${tokens.radius.lg};
    padding: 18px;
    display: flex;
    flex-direction: column;
    gap: 12px;
    min-width: 0;
    h2 {
        margin: 0;
        font-size: 16px;
        font-weight: 600;
    }
`;

const KV = styled.ul`
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    gap: 8px;
    li {
        display: flex;
        justify-content: space-between;
        gap: 8px;
        font-size: 13px;
    }
`;

function EnvCard({ dep, selected, onSelect, onChange }: { dep: AirflowDeployment; selected: boolean; onSelect: () => void;
    onChange: () => void }) {
    const t = useT();
    const { client } = useConsumerApi();
    const act = async (path: string) => {
        try {
            await client.request('POST', `${BASE}/${dep.name}/${path}`, {});
            message.success(path === 'wake' ? 'Awake' : 'Hibernating');
            onChange();
        } catch (e) {
            notifyError(e);
        }
    };
    return (
        <Env $selected={selected} aria-label={dep.name}>
            <div>
                <h3>
                    <button type="button" aria-pressed={selected} onClick={onSelect} title={t('airflow.show', { name: dep.name })}>
                        {dep.name}
                    </button>
                </h3>
                <StatusBadge tone={airflowTone(dep.state)} state={dep.state} />
            </div>
            <dl>
                <dt>{t('airflow.version')}</dt>
                <dd>{dep.airflow_version}{dep.executor ? ` · ${dep.executor}` : ''} · {dep.environment}</dd>
                <dt>{t('airflow.liveDeploy')}</dt>
                <dd className="mono">{dep.dags_release ?? '—'}</dd>
                <dt>{t('airflow.runsAs')}</dt>
                <dd className="mono">{actorName(dep.pipeline_user)}</dd>
                <dt>{t('airflow.hours')}</dt>
                <dd>{awakeHours(dep.hibernation) ?? t('airflow.alwaysOn')}</dd>
            </dl>
            {dep.error && <Alert type="error" showIcon message={dep.error} />}
            <Space wrap>
                {dep.url && <Button href={dep.url} target="_blank" rel="noreferrer">{t('airflow.open')}</Button>}
                {dep.environment !== 'prod' && dep.state === 'ready' && <Button onClick={() => act('hibernate')}>Hibernate</Button>}
                {dep.state === 'hibernating' && <Button type="primary" onClick={() => act('wake')}>Wake</Button>}
            </Space>
        </Env>
    );
}

function Health({ dep }: { dep: AirflowDeployment }) {
    const t = useT();
    const q = useDheQuery<Record<string, any>>(`airflow:${dep.name}:status`, (c, signal) =>
        c.request('GET', `${BASE}/${dep.name}/status`, undefined, { signal }), { staleMs: 10_000, pollMs: 30_000 });
    const comps = q.data?.components ?? {};
    const s = q.data;
    return (
        <Panel aria-labelledby="af-health">
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                <h2 id="af-health">{t('airflow.healthOf', { name: dep.name })}</h2>
                {s && <StatusBadge tone={airflowTone(s.health)} state={s.health} />}
            </div>
            {q.error ? <ErrorNotice error={q.error} area="The deployment's status" /> : !s ? <Spin /> : (
                <>
                    {(s.problems ?? []).map((p: string) => <Alert key={p} type="warning" showIcon message={p} />)}
                    <KV>
                        {['metadatabase', 'scheduler', 'dag_processor', 'triggerer'].map((k) => (
                            <li key={k}><span style={{ textTransform: 'capitalize' }}>{k.replace('_', ' ')}</span>
                                {comps[k]?.status ? <StatusBadge plain tone={airflowTone(comps[k].status)} state={comps[k].status} /> : '—'}</li>
                        ))}
                        <li><span>Scheduler heartbeat</span>
                            <span>{s.scheduler_heartbeat_age_s != null ? `${s.scheduler_heartbeat_age_s} s ago` : '—'}</span></li>
                        <li><span>Tasks</span><span>{s.tasks ? `${s.tasks.running} running, ${s.tasks.queued} queued` : '—'}</span></li>
                        <li><span>Image</span><span style={{ fontFamily: tokens.fontMono, overflowWrap: 'anywhere', textAlign: 'right' }}>
                            {dep.image ?? '—'}</span></li>
                    </KV>
                    {(s.import_errors ?? []).length > 0 && (
                        <Table size="small" rowKey="file" pagination={false} dataSource={s.import_errors}
                            columns={[{ title: 'DAG file', dataIndex: 'file' }, { title: 'Import error', dataIndex: 'error', ellipsis: true }]} />
                    )}
                </>
            )}
        </Panel>
    );
}

function Deploys({ dep, list, onChange }: { dep: AirflowDeployment; list: AirflowList; onChange: () => void }) {
    const t = useT();
    const { client } = useConsumerApi();
    const q = useDheQuery<{ deploys: Deploy[] }>(`airflow:${dep.name}:deploys`, (c, signal) =>
        c.request('GET', `${BASE}/${dep.name}/deploys`, undefined, { signal }));
    const p = useDheQuery<{ promotions: Promotion[] }>(`airflow:${dep.name}:promotions`, (c, signal) =>
        c.request('GET', `${BASE}/${dep.name}/promotions`, undefined, { signal }));
    const [promoting, setPromoting] = useState(false);
    const [target, setTarget] = useState<string | undefined>(dep.promotes_to ?? undefined);
    const [comment, setComment] = useState('');
    const run = async (fn: () => Promise<unknown>, ok: string) => {
        try {
            await fn();
            message.success(ok);
            await Promise.all([q.refresh(), p.refresh()]);
            onChange();
        } catch (e) {
            notifyError(e);
        }
    };
    const others = list.deployments.filter((d) => d.name !== dep.name && d.environment !== dep.environment);
    // The latest refused deploy, and why: Cairn's error, or the DAG files that did not import with Airflow's message.
    const refused = (q.data?.deploys ?? []).filter((d) => d.state === 'refused').slice(0, 1).map((d) => {
        const errs = Object.entries(d.checked?.import_errors ?? {});
        const why = d.error ?? (errs.length ? `${errs.map(([f]) => f).join(', ')} does not import (${String(errs[0][1])
            .split('\n').filter(Boolean).slice(-1)[0] ?? ''}). Nothing reached the deployment.` : 'refused');
        return { id: d.id, seq: d.seq, why };
    });
    const promotions = (p.data?.promotions ?? []).slice().sort((a, b) => Number(b.state === 'pending') - Number(a.state === 'pending'));
    return (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 20 }}>
            <Panel aria-labelledby="af-deploys" style={{ flex: '999 1 560px' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                    <h2 id="af-deploys">{t('airflow.deploysTo', { name: dep.name })}</h2>
                    <Button type="primary" disabled={!dep.dags_release} onClick={() => setPromoting(true)}>Promote</Button>
                </div>
                <Muted>Deploy from a project with <code>cairn airflow deploy {dep.name} --dags-only</code>.</Muted>
                {q.error ? <ErrorNotice error={q.error} area="Deploys" /> : (
                    <Table<Deploy> size="small" rowKey="id" loading={q.loading} dataSource={q.data?.deploys ?? []} pagination={false}
                        scroll={{ x: true }} locale={{ emptyText: 'Nothing deployed yet' }}
                        columns={[
                            { title: '#', dataIndex: 'seq' },
                            { title: 'What', render: (_, d) => (d.kind === 'dags' ? `DAGs (${d.files ?? 0} files)` : d.image) },
                            { title: 'Result', dataIndex: 'state', render: (v) => <StatusBadge tone={airflowTone(v)} state={v} /> },
                            { title: 'From', render: (_, d) => deploySource(d) },
                            { title: 'By', dataIndex: 'created_by', render: (v) => actorName(v) },
                            { title: 'When', dataIndex: 'created_ms', render: (v) => <When value={v} /> },
                            { title: 'Note', dataIndex: 'note', render: (v, d) => v ?? (d.checked?.import_errors &&
                                Object.keys(d.checked.import_errors).length ? `does not import: ${Object.keys(d.checked.import_errors).join(', ')}` : '') },
                            { title: <span className="sr-only">Actions</span>, render: (_, d) => (d.state === 'superseded' ? (
                                <Button size="small" onClick={() => run(() => client.request('POST', `${BASE}/${dep.name}/deploys/${d.id}/rollback`, {}),
                                    `Rolled back to #${d.seq}`)}>Roll back</Button>) : null) },
                        ]} />
                )}
                {refused.map((d) => (
                    <p key={d.id} role="note" style={{ margin: 0, fontSize: 13, color: tokens.status.error.fg, background: '#FEF2F2',
                        borderRadius: tokens.radius.sm, padding: '10px 12px', overflowWrap: 'anywhere' }}>#{d.seq} refused: {d.why}</p>
                ))}
            </Panel>
            <div style={{ flex: '1 1 300px', display: 'flex', flexDirection: 'column', gap: 16, minWidth: 0 }}>
                <Panel aria-labelledby="af-promotions">
                    <h2 id="af-promotions">{t('airflow.promotionsWaiting')}</h2>
                    {p.error ? <ErrorNotice error={p.error} area="Promotions" /> : promotions.length === 0 ? <Muted>No promotions</Muted> : (
                        <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', gap: 12 }}>
                            {promotions.map((x) => (
                                <li key={x.id} style={{ display: 'grid', gap: 6 }}>
                                    <span style={{ display: 'flex', justifyContent: 'space-between', gap: 8, flexWrap: 'wrap' }}>
                                        <span>{x.source} → <strong>{x.target}</strong></span>
                                        <StatusBadge tone={airflowTone(x.state)} state={x.state} />
                                    </span>
                                    <Muted style={{ fontSize: 13 }}>Asked by {actorName(x.requested_by)}
                                        {x.decided_by ? ` · decided by ${actorName(x.decided_by)}` : ''}{x.error ? ` · ${x.error}` : ''}</Muted>
                                    {x.state === 'pending' && (
                                        <Space wrap>
                                            <Button type="primary" onClick={() => run(() => client.request('POST',
                                                `/v1/airflow/promotions/${x.id}/approve`, { comment: '' }), 'Approved and deployed')}>Approve</Button>
                                            <Button danger onClick={() => run(() => client.request('POST',
                                                `/v1/airflow/promotions/${x.id}/reject`, { comment: '' }), 'Rejected')}>Reject</Button>
                                        </Space>
                                    )}
                                </li>
                            ))}
                        </ul>
                    )}
                </Panel>
                <Health dep={dep} />
            </div>
            <Modal title={`Promote ${dep.name}'s live DAGs`} open={promoting} okText="Promote" onCancel={() => setPromoting(false)}
                onOk={async () => {
                    await run(() => client.request('POST', `${BASE}/${dep.name}/promote`, { to: target, comment }),
                        'Promoted (or waiting for approval)');
                    setPromoting(false);
                }}>
                <Typography.Paragraph>To</Typography.Paragraph>
                <Select style={{ width: '100%' }} value={target} onChange={setTarget} aria-label="Promote to"
                    options={others.map((d) => ({ value: d.name, label: `${d.name} (${d.environment}${d.approval?.required ? ', needs approval' : ''})` }))} />
                <Input.TextArea style={{ marginTop: 12 }} placeholder="Why (optional)" value={comment} aria-label="Comment"
                    onChange={(e) => setComment(e.target.value)} />
            </Modal>
        </div>
    );
}

function Logs({ dep }: { dep: AirflowDeployment }) {
    const t = useT();
    const [component, setComponent] = useState('scheduler');
    const q = useDheQuery<{ log: string }>(`airflow:${dep.name}:logs:${component}`, (c, signal) =>
        c.request('GET', `${BASE}/${dep.name}/logs`, undefined, { signal, query: { component, tail: '300' } }));
    return (
        <Panel aria-labelledby="af-logs">
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                <h2 id="af-logs">{t('airflow.logsOf', { name: dep.name })}</h2>
                <Select style={{ width: 200 }} value={component} onChange={setComponent} aria-label="Component"
                    options={COMPONENTS.map((c) => ({ value: c }))} />
            </div>
            {q.error ? <ErrorNotice error={q.error} area="Logs" /> : q.loading ? <Spin /> : (
                <pre tabIndex={0} style={{ maxHeight: 420, overflow: 'auto', fontSize: 12, margin: 0, background: tokens.color.surfaceSunken,
                    padding: 8, borderRadius: tokens.radius.sm }}>{q.data?.log || '(empty)'}</pre>
            )}
        </Panel>
    );
}

/** Managed Airflow: each team's environments as cards in promotion order; the one picked scopes the deploys, the
 * promotions waiting, its health and its logs. */
export function AirflowDeploymentsView() {
    const t = useT();
    const q = useDheQuery<AirflowList>(`resource:${BASE}`, (c, signal) => c.request<AirflowList>('GET', BASE, undefined, { signal }));
    const [creating, setCreating] = useState(false);
    const [selected, setSelected] = useState<string | null>(null);
    const location = useLocation();
    const history = useHistory();
    const spec = RESOURCE_SPECS['airflow-deployments'];
    const create = createAction(spec);
    useEffect(() => {                                     // `?action=New deployment` (the command palette)
        const params = new URLSearchParams(location.search);
        if (params.get('action') !== NEW_DEPLOYMENT) return;
        setCreating(true);
        params.delete('action');
        history.replace({ pathname: location.pathname, search: params.toString() ? `?${params}` : '' });
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [location.search]);
    if (q.error) return <ErrorNotice error={q.error} area="Managed Airflow" />;
    const list = q.data;
    if (!list) return <Spin />;
    const chains = promotionChains(list.deployments);
    const ordered = chains.flatMap((c) => c.chain);
    const dep = ordered.find((d) => d.name === selected) ?? ordered[0] ?? null;
    const refresh = () => void q.refresh();
    return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
            <Space wrap>
                <Button type="primary" onClick={() => setCreating(true)} disabled={!list.runtime}>{NEW_DEPLOYMENT}</Button>
                {!list.runtime && <Muted>Managed Airflow is not enabled on this Cairn (CAIRN_AIRFLOW_RUNTIME).</Muted>}
            </Space>
            {list.deployments.length === 0 ? <Empty description="No deployments yet" /> : (
                <section aria-label={t('airflow.environments')} style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
                    {chains.map((c) => (
                        <div key={c.team} style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                            <h2 style={{ margin: 0, fontFamily: tokens.fontHeading, fontSize: 22 }}>
                                {c.team ? `${c.team}: ` : ''}{c.chain.map((d) => d.environment).join(' → ')}
                            </h2>
                            <Cards>
                                {c.chain.map((d) => (
                                    <EnvCard key={d.name} dep={d} selected={dep?.name === d.name} onSelect={() => setSelected(d.name)}
                                        onChange={refresh} />
                                ))}
                            </Cards>
                        </div>
                    ))}
                </section>
            )}
            {creating && create && (
                <ActionDialog spec={spec} action={create} row={null} onClose={() => setCreating(false)}
                    onDone={() => { setCreating(false); refresh(); }} />
            )}
            {dep && (
                <>
                    <Deploys key={dep.name} dep={dep} list={list} onChange={refresh} />
                    <Logs key={`logs:${dep.name}`} dep={dep} />
                </>
            )}
        </div>
    );
}
