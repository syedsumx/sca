import { Alert, Button, Empty, Space, Spin, Table, message } from 'antd';
import React, { useEffect, useState } from 'react';
import { useHistory, useLocation } from 'react-router-dom';
import styled from 'styled-components';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import { ErrorNotice, notifyError } from '../components/ErrorNotice';
import { RowActions } from '../components/RowActions';
import { StatusBadge } from '../components/StatusBadge';
import { Muted, tokens } from '../components/tokens';
import { useT } from '../i18n';
import { actorName } from '../utils/format';
import { When } from '../utils/time';
import { RESOURCE_SPECS } from './catalog';
import { ActionDialog, createAction } from './ResourceView';

/** A managed NiFi deployment as `GET /v1/nifi/deployments` lists it. */
export interface NiFiDeployment {
    name: string;
    state: string;
    environment: string;
    nifi_version: string;
    team: string | null;
    canvas_url: string | null;
    flows: string[];
    resources: { cpu: number; memory_mb: number; heap_mb: number; storage_gb: number } | null;
    error?: string | null;
}

export interface NiFiList {
    deployments: NiFiDeployment[];
    runtime: string | null;
    versions: string[];
}

/** A flow Cairn deploys to a NiFi deployment, as `GET /v1/nifi/flows` lists it. */
export interface NiFiFlow {
    name: string;
    deployment: string;
    owner: string;
    state: string;
    version: number;
    deployed_version: number | null;
    error: string | null;
    synced_ms: number | null;
    sink: { type: 'lakehouse'; namespace: string; table: string } | null;
    secrets: { parameter: string; scope: string; key: string }[];
    running: boolean;
}

const BASE = '/v1/nifi';

/** Where a flow lands, in words: "crm.contacts" for a lakehouse table, else nothing. */
export function landsIn(f: Pick<NiFiFlow, 'sink'>): string {
    return f.sink ? `${f.sink.namespace}.${f.sink.table}` : '—';
}

/** A deployment's size in words: "2 CPU · 8 GB (heap 4 GB) · 50 GB". */
export function sizeText(r: NiFiDeployment['resources']): string {
    if (!r) return '—';
    const gb = (mb: number) => `${Math.round((mb / 1024) * 10) / 10} GB`;
    return `${r.cpu} CPU · ${gb(r.memory_mb)} (heap ${gb(r.heap_mb)}) · ${r.storage_gb} GB`;
}

const Cards = styled.div`
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 14px;
`;

const Card = styled.article`
    background: ${tokens.color.surface};
    border: 1px solid ${tokens.color.border};
    border-radius: ${tokens.radius.lg};
    padding: 16px;
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
        font-family: ${tokens.fontMono};
        overflow-wrap: anywhere;
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
`;

function DeploymentCard({ dep, onChange }: { dep: NiFiDeployment; onChange: () => void }) {
    const t = useT();
    const { client } = useConsumerApi();
    return (
        <Card aria-label={dep.name}>
            <div>
                <h3>{dep.name}</h3>
                <StatusBadge state={dep.state} />
            </div>
            <dl>
                <dt>{t('nifi.version')}</dt>
                <dd>{dep.nifi_version} · {dep.environment}{dep.team ? ` · ${dep.team}` : ''}</dd>
                <dt>{t('nifi.size')}</dt>
                <dd>{sizeText(dep.resources)}</dd>
                <dt>{t('nifi.flows')}</dt>
                <dd>{t('nifi.flowCount', { n: dep.flows.length })}</dd>
            </dl>
            {dep.error && <Alert type="error" showIcon message={dep.error} />}
            <Space wrap>
                {dep.canvas_url && (
                    <Button type="primary" href={dep.canvas_url} target="_blank" rel="noreferrer" title={t('nifi.canvasHint')}>
                        {t('nifi.openCanvas')}
                    </Button>
                )}
                <RowActions name={dep.name} size="middle" actions={[{
                    key: 'delete', label: t('nifi.delete'), danger: true, confirm: t('nifi.deleteDeployment', { name: dep.name }),
                    onSelect: async () => {
                        try {
                            await client.request('DELETE', `${BASE}/deployments/${encodeURIComponent(dep.name)}`);
                            message.success(t('nifi.deleted'));
                            onChange();
                        } catch (e) {
                            notifyError(e);
                        }
                    },
                }]} />
            </Space>
        </Card>
    );
}

function Flows({ onChange }: { onChange: () => void }) {
    const t = useT();
    const { client } = useConsumerApi();
    const q = useDheQuery<{ flows: NiFiFlow[] }>(`resource:${BASE}/flows`, (c, signal) =>
        c.request('GET', `${BASE}/flows`, undefined, { signal }));
    const run = async (fn: () => Promise<unknown>, ok: string) => {
        try {
            await fn();
            message.success(ok);
            await q.refresh();
            onChange();
        } catch (e) {
            notifyError(e);
        }
    };
    const path = (f: NiFiFlow, what = '') => `${BASE}/flows/${encodeURIComponent(f.name)}${what}`;
    if (q.error) return <ErrorNotice error={q.error} area={t('nifi.flows')} />;
    return (
        <section aria-labelledby="nifi-flows" style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            <h2 id="nifi-flows" style={{ margin: 0, fontSize: 18 }}>{t('nifi.flows')}</h2>
            <Muted>{t('nifi.secretsNote')}</Muted>
            <Table<NiFiFlow> size="small" rowKey="name" loading={q.loading} dataSource={q.data?.flows ?? []} pagination={false}
                scroll={{ x: true }} locale={{ emptyText: t('nifi.noFlows') }}
                expandable={{ rowExpandable: (f) => !!f.error, expandedRowRender: (f) => <Alert type="error" showIcon message={f.error} /> }}
                columns={[
                    { title: t('nifi.flows'), dataIndex: 'name', render: (v) => <code>{v}</code> },
                    { title: t('nifi.deployments'), dataIndex: 'deployment' },
                    { title: t('nifi.owner'), dataIndex: 'owner', render: (v) => actorName(v) },
                    { title: t('nifi.lands'), render: (_, f) => landsIn(f) },
                    { title: t('nifi.state'), dataIndex: 'state', render: (v) => <StatusBadge state={v} /> },
                    { title: t('nifi.synced'), dataIndex: 'synced_ms', render: (v) => <When value={v} /> },
                    {
                        title: <span className="sr-only">Actions</span>, render: (_, f) => (
                            <RowActions name={f.name}
                                primary={f.state === 'running'
                                    ? [{ key: 'stop', label: t('nifi.stop'), onSelect: () => run(() => client.request('POST', path(f, '/stop'), {}), t('nifi.stopped')) }]
                                    : f.state === 'stopped'
                                        ? [{ key: 'start', label: t('nifi.start'), onSelect: () => run(() => client.request('POST', path(f, '/start'), {}), t('nifi.started')) }]
                                        : []}
                                actions={[
                                    { key: 'deploy', label: t('nifi.redeploy'), onSelect: () => run(() => client.request('POST', path(f, '/deploy'), {}), t('nifi.deployed')) },
                                    {
                                        key: 'sync', label: t('nifi.readRuns'), disabled: f.state !== 'running' && f.state !== 'stopped',
                                        onSelect: async () => {
                                            try {
                                                const out = await client.request<{ run: { state: string } | null }>('POST', path(f, '/sync'), {});
                                                message.info(out.run ? t('nifi.runRead', { state: out.run.state }) : t('nifi.noRun'));
                                            } catch (e) {
                                                notifyError(e);
                                            }
                                        },
                                    },
                                    { key: 'delete', label: t('nifi.delete'), danger: true, onSelect: () => run(() => client.request('DELETE', path(f)), t('nifi.deleted')) },
                                ]} />
                        ),
                    },
                ]} />
        </section>
    );
}

/** Ingestion › Connectors (NiFi): managed NiFi deployments as cards (open the canvas through Cairn) and the flows Cairn
 * deploys to them, with generated forms to make either. */
export function NiFiConnectorsView() {
    const t = useT();
    const q = useDheQuery<NiFiList>(`resource:${BASE}/deployments`, (c, signal) =>
        c.request<NiFiList>('GET', `${BASE}/deployments`, undefined, { signal }));
    const [creating, setCreating] = useState<'deployment' | 'flow' | null>(null);
    const location = useLocation();
    const history = useHistory();
    const depSpec = RESOURCE_SPECS['nifi-connectors'];
    const flowSpec = RESOURCE_SPECS['nifi-flows'];
    const newDeployment = createAction(depSpec);
    const newFlow = createAction(flowSpec);
    useEffect(() => {                                     // `?action=New deployment` (the command palette)
        const params = new URLSearchParams(location.search);
        const a = params.get('action');
        if (a !== 'New deployment' && a !== 'New flow') return;
        setCreating(a === 'New flow' ? 'flow' : 'deployment');
        params.delete('action');
        history.replace({ pathname: location.pathname, search: params.toString() ? `?${params}` : '' });
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [location.search]);
    if (q.error) return <ErrorNotice error={q.error} area={t('nifi.title')} />;
    const list = q.data;
    if (!list) return <Spin />;
    const refresh = () => void q.refresh();
    return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
            <Muted style={{ maxWidth: 820 }}>{t('nifi.intro')}</Muted>
            <Space wrap>
                <Button type="primary" onClick={() => setCreating('deployment')} disabled={!list.runtime}>{t('nifi.newDeployment')}</Button>
                <Button onClick={() => setCreating('flow')} disabled={!list.runtime || list.deployments.length === 0}>{t('nifi.newFlow')}</Button>
                {!list.runtime && <Muted>{t('nifi.notEnabled')}</Muted>}
            </Space>
            <section aria-labelledby="nifi-deployments" style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                <h2 id="nifi-deployments" style={{ margin: 0, fontSize: 18 }}>{t('nifi.deployments')}</h2>
                {list.deployments.length === 0 ? <Empty description={t('nifi.noDeployments')} /> : (
                    <Cards>
                        {list.deployments.map((d) => <DeploymentCard key={d.name} dep={d} onChange={refresh} />)}
                    </Cards>
                )}
            </section>
            {list.deployments.length > 0 && <Flows onChange={refresh} />}
            {creating === 'deployment' && newDeployment && (
                <ActionDialog spec={depSpec} action={newDeployment} row={null} onClose={() => setCreating(null)}
                    onDone={() => { setCreating(null); refresh(); }} />
            )}
            {creating === 'flow' && newFlow && (
                <ActionDialog spec={flowSpec} action={newFlow} row={null} onClose={() => setCreating(null)}
                    onDone={() => { setCreating(null); refresh(); }} />
            )}
        </div>
    );
}
