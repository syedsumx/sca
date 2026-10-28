import { Alert, Button, Drawer, Progress, Space, Table, Tabs, message } from 'antd';
import React, { useState } from 'react';
import { Link } from 'react-router-dom';
import styled from 'styled-components';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { CostTotal, CoverageFacet, CoverageRow, ReportAsset, ReportSnapshot } from '../api/types';
import { assetPath } from '../components/Assets';
import { download } from '../components/BulkEdit';
import { Muted, Page, SectionTitle, Surface } from '../components/tokens';

const FACETS: { key: CoverageFacet; label: string }[] = [
    { key: 'description', label: 'Described' },
    { key: 'owner', label: 'Owned' },
    { key: 'classification', label: 'Classified' },
    { key: 'certified', label: 'Certified' },
    { key: 'monitored', label: 'Monitored' },
];

const Grid = styled.div`
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 16px;
    margin: 12px 0 24px;
`;

const Figure = styled.div`
    font-size: 26px;
    font-weight: 600;
    line-height: 1.2;
`;

interface DrillDown {
    title: string;
    missing: CoverageFacet;
    domain?: string | null;
    platform?: string;
}

function Stat({ label, value, hint }: { label: string; value: React.ReactNode; hint?: string }) {
    return (
        <Surface style={{ padding: 16 }} aria-label={label}>
            <Muted>{label}</Muted>
            <Figure>{value}</Figure>
            {hint && <Muted style={{ fontSize: 12 }}>{hint}</Muted>}
        </Surface>
    );
}

const hours = (h: number | null) => (h === null ? '–' : h < 48 ? `${h.toFixed(1)} h` : `${(h / 24).toFixed(1)} d`);
const short = (k: string | null) => (k ? k.replace(/^urn:li:domain:/, '') : 'No domain');

function MissingDrawer({ drill, onClose }: { drill: DrillDown | null; onClose: () => void }) {
    const key = drill ? `/reports/assets?${drill.missing}|${drill.domain}|${drill.platform}` : null;
    const q = useDheQuery<ReportAsset[]>(key, (c, signal) =>
        c.reportAssets({ missing: drill!.missing, domain: drill!.domain, platform: drill!.platform }, { signal }),
    );
    return (
        <Drawer open={!!drill} width={560} title={drill?.title} onClose={onClose}>
            {q.error && <Alert type="error" message={q.error.message} />}
            <Table<ReportAsset>
                size="small"
                rowKey="urn"
                loading={q.loading}
                dataSource={q.data ?? []}
                pagination={{ pageSize: 25, hideOnSinglePage: true }}
                columns={[
                    { title: 'Dataset', render: (_, a) => <Link to={assetPath(a.urn)}>{a.name}</Link> },
                    { title: 'Platform', dataIndex: 'platform' },
                ]}
            />
        </Drawer>
    );
}

function CoverageTable({
    rows,
    by,
    onDrill,
}: {
    rows: CoverageRow[];
    by: 'domain' | 'platform';
    onDrill: (d: DrillDown) => void;
}) {
    return (
        <Table<CoverageRow>
            size="small"
            rowKey={(r) => r.key ?? '(none)'}
            dataSource={rows}
            pagination={rows.length > 15 ? { pageSize: 15, showSizeChanger: false } : false}
            columns={[
                {
                    title: by === 'domain' ? 'Domain' : 'Platform',
                    render: (_, r) => (by === 'domain' ? short(r.key) : r.key),
                },
                { title: 'Datasets', dataIndex: 'assets', width: 90 },
                ...FACETS.map(({ key, label }) => ({
                    title: label,
                    width: 130,
                    render: (_: unknown, r: CoverageRow) => (
                        <Button
                            type="link"
                            size="small"
                            style={{ padding: 0, width: '100%' }}
                            aria-label={`${label} in ${by === 'domain' ? short(r.key) : r.key}: ${r[key]}%`}
                            onClick={() =>
                                onDrill({
                                    title: `Not ${label.toLowerCase()}: ${by === 'domain' ? short(r.key) : r.key}`,
                                    missing: key,
                                    ...(by === 'domain' ? { domain: r.key } : { platform: r.key ?? undefined }),
                                })
                            }
                        >
                            <Progress percent={r[key]} size="small" format={(p) => `${p}%`} />
                        </Button>
                    ),
                })),
            ]}
        />
    );
}

const usd = (v: number) => `$${v.toLocaleString(undefined, { maximumFractionDigits: 2, minimumFractionDigits: 2 })}`;
const size = (b: number | null) =>
    b === null ? '–' : b >= 1e12 ? `${(b / 1e12).toFixed(1)} TB` : `${(b / 1e9).toFixed(1)} GB`;

function CostTable({ kind }: { kind: 'idle' | 'expensive' }) {
    const { client } = useConsumerApi();
    const q = useDheQuery<CostTotal[]>(`/reports/costs/${kind}`, (c, signal) =>
        kind === 'idle' ? c.idleTables(30, { signal }) : c.expensiveTables(30, { signal }),
    );
    const [proposed, setProposed] = useState<string[]>([]);
    const propose = async (t: CostTotal) => {
        try {
            const reason =
                kind === 'idle'
                    ? `No reads in 30 days and nothing downstream; it costs ${usd(t.monthly_storage_usd ?? t.storage_usd)} a month to store.`
                    : `It costs ${usd(t.cost_per_read_usd ?? t.cost_usd)} per read.`;
            await client.proposeDeprecation(t.urn, reason);
            setProposed([...proposed, t.urn]);
            message.success('Owners and followers have been asked');
        } catch (e) {
            message.error((e as Error).message);
        }
    };
    if (q.error) return <Alert type="error" message={q.error.message} />;
    if (q.data && !q.data.length)
        return <Muted>{kind === 'idle' ? 'No idle tables in the last 30 days.' : 'No cost figures yet.'}</Muted>;
    return (
        <Table<CostTotal>
            size="small"
            rowKey="urn"
            loading={q.loading}
            dataSource={q.data ?? []}
            pagination={{ pageSize: 15, hideOnSinglePage: true, showSizeChanger: false }}
            columns={[
                { title: 'Table', render: (_, t) => <Link to={assetPath(t.urn)}>{t.name}</Link> },
                { title: 'Platform', dataIndex: 'platform' },
                { title: 'Size', render: (_, t) => size(t.storage_bytes) },
                kind === 'idle'
                    ? { title: 'Storage / month', render: (_, t) => usd(t.monthly_storage_usd ?? 0) }
                    : { title: 'Cost per read', render: (_, t) => usd(t.cost_per_read_usd ?? 0) },
                { title: 'Cost, 30 days', render: (_, t) => usd(t.cost_usd) },
                { title: 'Reads', dataIndex: 'reads' },
                {
                    title: '',
                    render: (_, t) =>
                        proposed.includes(t.urn) ? (
                            <Muted>Proposed</Muted>
                        ) : (
                            <Button size="small" onClick={() => void propose(t)}>
                                Propose deprecation
                            </Button>
                        ),
                },
            ]}
        />
    );
}

/** Is the catalog used, and is the estate getting governed? */
export function ReportsPage() {
    const { client, cache } = useConsumerApi();
    const q = useDheQuery<ReportSnapshot>('/reports', (c, signal) => c.report({ signal }));
    const [drill, setDrill] = useState<DrillDown | null>(null);
    const [busy, setBusy] = useState(false);
    const s = q.data;

    const refresh = async () => {
        setBusy(true);
        try {
            cache.set('/reports', await client.refreshReport());
            message.success('Report recomputed');
        } catch (e) {
            message.error((e as Error).message);
        } finally {
            setBusy(false);
        }
    };

    return (
        <Page>
            <Space style={{ display: 'flex', justifyContent: 'space-between', flexWrap: 'wrap' }}>
                <div>
                    <h1 style={{ marginBottom: 0 }}>Reports</h1>
                    {s && (
                        <Muted>
                            {s.assets.toLocaleString()} datasets · computed {new Date(s.computed_ms).toLocaleString()}
                        </Muted>
                    )}
                </div>
                <Space>
                    <Button
                        onClick={() => void client.reportCsv().then((t) => download(`cairn-coverage-${s?.day}.csv`, t))}
                    >
                        Download CSV
                    </Button>
                    <Button onClick={() => void refresh()} loading={busy}>
                        Recompute now
                    </Button>
                </Space>
            </Space>
            {q.error && <Alert type="error" style={{ marginTop: 16 }} message={q.error.message} />}
            {s && (
                <>
                    <SectionTitle style={{ marginTop: 24 }}>Coverage</SectionTitle>
                    <Grid>
                        {FACETS.map(({ key, label }) => (
                            <Surface key={key} style={{ padding: 16 }} aria-label={`${label} overall`}>
                                <Muted>{label}</Muted>
                                <Progress
                                    type="circle"
                                    width={72}
                                    percent={s.coverage.overall[key]}
                                    format={(p) => `${p}%`}
                                />
                                <div>
                                    <Button
                                        type="link"
                                        size="small"
                                        style={{ padding: 0 }}
                                        onClick={() => setDrill({ title: `Not ${label.toLowerCase()}`, missing: key })}
                                    >
                                        Show the rest
                                    </Button>
                                </div>
                            </Surface>
                        ))}
                    </Grid>
                    <Tabs
                        items={[
                            {
                                key: 'domain',
                                label: 'By domain',
                                children: <CoverageTable rows={s.coverage.by_domain} by="domain" onDrill={setDrill} />,
                            },
                            {
                                key: 'platform',
                                label: 'By platform',
                                children: (
                                    <CoverageTable rows={s.coverage.by_platform} by="platform" onDrill={setDrill} />
                                ),
                            },
                        ]}
                    />
                    <SectionTitle style={{ marginTop: 24 }}>Adoption</SectionTitle>
                    <Grid>
                        <Stat
                            label="Weekly active users"
                            value={s.adoption.weekly_active_users}
                            hint={`${s.adoption.monthly_active_users} in 30 days`}
                        />
                        <Stat label="Asset views, 7 days" value={s.adoption.asset_views_7d.toLocaleString()} />
                        <Stat
                            label="Access requests, 30 days"
                            value={s.adoption.requests_30d}
                            hint={`${s.adoption.approved_30d} approved · ${s.adoption.pending_requests} pending`}
                        />
                        <Stat label="Median time to decision" value={hours(s.adoption.median_hours_to_decision)} />
                    </Grid>
                    <SectionTitle>Quality</SectionTitle>
                    <Grid>
                        <Stat label="Open incidents" value={s.quality.open_incidents} />
                        <Stat label="Datasets monitored" value={`${s.quality.monitored_pct}%`} />
                        <Stat
                            label="Mean time to resolve"
                            value={hours(s.quality.mean_hours_to_resolve)}
                            hint={`${s.quality.resolved_30d} resolved in 30 days`}
                        />
                    </Grid>
                    <SectionTitle>Cost</SectionTitle>
                    <Tabs
                        items={[
                            { key: 'idle', label: 'Idle tables', children: <CostTable kind="idle" /> },
                            { key: 'expensive', label: 'Expensive per read', children: <CostTable kind="expensive" /> },
                        ]}
                    />
                    {s.governance.personas !== undefined && (
                        <>
                            <SectionTitle>Governance</SectionTitle>
                            <Grid>
                                <Stat label="Personas" value={s.governance.personas} />
                                <Stat label="Purposes" value={s.governance.purposes} />
                                <Stat
                                    label="Active grants"
                                    value={s.governance.active_grants}
                                    hint={`${s.governance.grants_expiring_7d} expire within 7 days`}
                                />
                            </Grid>
                        </>
                    )}
                </>
            )}
            <MissingDrawer drill={drill} onClose={() => setDrill(null)} />
        </Page>
    );
}
