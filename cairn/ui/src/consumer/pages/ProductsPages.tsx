import { Button, Empty, Input, Select, Space, Switch, Table, Tabs, Tag, message } from 'antd';
import React, { useMemo, useState } from 'react';
import { Link, useHistory, useLocation, useParams } from 'react-router-dom';

import { CairnApiError } from '../../api/client';
import { CatalogProduct, catalogProduct, catalogProducts, contracts as listContracts } from '../api/catalog';
import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { Contract, ContractStatus, ProductCard, ProductDetail } from '../api/types';
import { assetPath } from '../components/Assets';
import { ContractPanel } from '../components/Contract';
import { ErrorNotice, notifyError } from '../components/ErrorNotice';
import { LineageGraphView } from '../components/LineageGraph';
import { RequestAccessDrawer, accessLabel } from '../components/Marketplace';
import { ProductSlos } from '../components/Slos';
import { StatusBadge, StatusTone } from '../components/StatusBadge';
import { Muted, Page, Skeleton, Surface } from '../components/tokens';
import { TrustBadges } from '../components/TrustBadges';
import { useDheSession } from '../context/CairnProvider';
import { PageHeader } from '../guide/PageHeader';
import { pageHelp } from '../guide/help';
import { MessageKey, useT } from '../i18n';
import { RESOURCE_SPECS } from '../resources/catalog';
import { ActionDialog, createAction } from '../resources/ResourceView';
import type { ResourceAction } from '../resources/types';
import { actorName, entityTypeLabel, platformName, relativeTime, shortUrn } from '../utils/format';
import { MarkdownView } from '../utils/markdown';

const PRODUCTS = RESOURCE_SPECS['catalog-products'];
const CONTRACTS = RESOURCE_SPECS.contracts;
const action = (spec: typeof PRODUCTS, method: string, row: boolean): ResourceAction | undefined =>
    spec.actions?.find((a) => a.method === method && /\{\w+\}/.test(a.path) === row);

export function productHref(urn: string): string {
    return `/products/${encodeURIComponent(urn)}`;
}

export function contractHref(dataset: string): string {
    return `/contracts/${encodeURIComponent(dataset)}`;
}

/** A contract's state in words and a shape: passing, failing, or not checked yet. */
export function ContractState({ status, plain }: { status: ContractStatus | null | undefined; plain?: boolean }) {
    const t = useT();
    if (!status) return <Muted>{t('contracts.none')}</Muted>;
    const [tone, key]: [StatusTone, MessageKey] = status === 'MET' ? ['ok', 'contracts.passing']
        : status === 'BROKEN' ? ['error', 'contracts.failing'] : ['waiting', 'contracts.pending'];
    return <StatusBadge tone={tone} label={t(key)} plain={plain} state={status.toLowerCase()} />;
}

function useProductActions() {
    const { cache } = useConsumerApi();
    const history = useHistory();
    const [open, setOpen] = useState<{ action: ResourceAction; row: Record<string, unknown> | null } | null>(null);
    const dialog = open && (
        <ActionDialog spec={PRODUCTS} action={open.action} row={open.row} onClose={() => setOpen(null)}
            onDone={() => {
                // Core's search is fed asynchronously: the lists are asked again, and a removed product's page is left.
                cache.invalidate('/catalog/products');
                cache.invalidate('/marketplace/products');
                if (open.action.method === 'DELETE') history.push('/products');
            }} />
    );
    return { dialog, open: (a: ResourceAction | null | undefined, row: Record<string, unknown> | null = null) => a && setOpen({ action: a, row }) };
}

/** Data products (Cairn Core): every product with its domain, owners, assets, health and contract; made and changed here. */
export function DataProductsPage() {
    const t = useT();
    const location = useLocation();
    const history = useHistory();
    const params = new URLSearchParams(location.search);
    const q = params.get('q') ?? '';
    const domain = params.get('domain') ?? '';
    const cards = useDheQuery<ProductCard[]>(`/marketplace/products?q=${q}&domain=${domain}&sort=name`, (c, signal) =>
        c.listProducts({ q: q || undefined, domain: domain || undefined, sort: 'name' }, { signal }));
    const mine = useDheQuery<{ products: CatalogProduct[] }>('/catalog/products', (c, signal) => catalogProducts(c, { signal }));
    const ports = useMemo(() => new Map((mine.data?.products ?? []).map((p) => [p.urn, p])), [mine.data]);
    const domains = useMemo(() => {
        const seen = new Map<string, string>();
        for (const p of cards.data ?? []) if (p.domain) seen.set(p.domain.urn, p.domain.name || shortUrn(p.domain.urn));
        return [...seen.entries()].map(([value, label]) => ({ value, label }));
    }, [cards.data]);
    const set = (k: string, v: string | null) => {
        const next = new URLSearchParams(location.search);
        if (v) next.set(k, v);
        else next.delete(k);
        history.replace(`/products${next.toString() ? `?${next}` : ''}`);
    };
    const actions = useProductActions();
    const create = createAction(PRODUCTS);

    return (
        <Page>
            <PageHeader title={t('products.title')} description={t('products.description')} help={pageHelp('/products')}
                actions={create && <Button type="primary" onClick={() => actions.open(create)}>{t('products.new')}</Button>} />
            <Space wrap style={{ margin: '16px 0' }}>
                <Input.Search aria-label={t('products.search')} placeholder={t('products.search')} defaultValue={q} allowClear
                    onSearch={(v) => set('q', v.trim() || null)} style={{ width: 300 }} />
                <Select aria-label={t('products.domain')} placeholder={t('products.domain')} allowClear style={{ minWidth: 200 }}
                    value={domain || undefined} onChange={(v) => set('domain', v ?? null)} options={domains} />
            </Space>
            {cards.error && <ErrorNotice error={cards.error} area={t('products.title')} />}
            {cards.loading && !cards.data && <Skeleton h={160} />}
            {cards.data && (
                <Table<ProductCard>
                    rowKey="urn"
                    size="middle"
                    dataSource={cards.data}
                    pagination={cards.data.length > 25 ? { pageSize: 25, showSizeChanger: false } : false}
                    locale={{ emptyText: <Empty description={t('products.empty')} /> }}
                    columns={[
                        {
                            title: t('products.name'),
                            dataIndex: 'name',
                            render: (v: string, p) => (
                                <span>
                                    <Link to={productHref(p.urn)}>{v}</Link>
                                    {p.description && <Muted style={{ display: 'block', fontSize: 12 }}>{p.description}</Muted>}
                                </span>
                            ),
                        },
                        { title: t('products.domain'), render: (_, p) => (p.domain
                            ? <Link to={`/domains/${encodeURIComponent(p.domain.urn)}`}>{p.domain.name || shortUrn(p.domain.urn)}</Link>
                            : <Muted>—</Muted>) },
                        { title: t('products.owners'), render: (_, p) => p.owners.map(actorName).join(', ') || <Muted>—</Muted> },
                        { title: t('products.assets'), render: (_, p) => {
                            const ours = ports.get(p.urn);
                            return (
                                <span>
                                    {p.assets_count.toLocaleString()}
                                    {ours && ours.output_ports.length > 0 && (
                                        <Muted style={{ fontSize: 12 }}> · {t('products.portsCount', { n: ours.output_ports.length })}</Muted>
                                    )}
                                </span>
                            );
                        } },
                        { title: t('products.health'), render: (_, p) => (
                            <TrustBadges certification={p.certification} qualityStatus={p.quality_status} compact />
                        ) },
                        { title: t('products.contract'), render: (_, p) => <ContractState status={p.contract} plain /> },
                    ]}
                />
            )}
            {actions.dialog}
        </Page>
    );
}

/** A data product: its assets and output ports, contracts, lineage, service levels and access, from Core and Cairn. */
export function DataProductPage() {
    const { urn: raw } = useParams<{ urn: string }>();
    const urn = decodeURIComponent(raw);
    const id = urn.replace(/^urn:li:dataProduct:/, '');
    const t = useT();
    const session = useDheSession();
    const { client, cache } = useConsumerApi();
    const key = `/marketplace/products/${urn}`;
    const product = useDheQuery<ProductDetail>(key, (c, signal) => c.getProduct(urn, { signal }));
    const ours = useDheQuery<CatalogProduct>(`/catalog/products/${id}`, (c, signal) => catalogProduct(c, id, { signal }));
    const promises = useDheQuery<Contract[]>('/quality/contracts', (c, signal) => listContracts(c, { signal }));
    const [requesting, setRequesting] = useState(false);
    const [lineageOf, setLineageOf] = useState<string | null>(null);
    const actions = useProductActions();
    const p = product.data;
    if (product.loading && !p) return <Page><Skeleton h={28} w="40%" /></Page>;
    if (product.error || !p) return <Page>{product.error && <ErrorNotice error={product.error} area={t('products.title')} />}</Page>;
    const record = ours.data;
    const portSet = new Set(record?.output_ports ?? []);
    const me = session.config?.user.urn;
    const canEdit = !!record && (!!session.config?.is_admin || (!!me && p.owners.includes(me)));
    const requestable = ['NONE', 'EXPIRED', 'REJECTED'].includes(p.access.status);
    const access = accessLabel(p.access.status, p.access.expires_at);
    const assetUrns = new Set(p.assets.map((a) => a.urn));
    const productContracts = (promises.data ?? []).filter((c) => assetUrns.has(c.dataset));
    const lineageAsset = lineageOf ?? [...portSet][0] ?? p.assets[0]?.urn ?? null;
    const edit = action(PRODUCTS, 'PUT', true);
    const remove = action(PRODUCTS, 'DELETE', true);
    const toggleSubscribe = async (on: boolean) => {
        try {
            await client.subscribe(urn, on);
            cache.invalidate(key);
            message.success(on ? t('products.subscribed') : t('products.unsubscribed'));
        } catch (e) {
            notifyError(e);
        }
    };

    return (
        <Page>
            <Surface style={{ padding: 20, marginTop: 12 }}>
                <PageHeader title={p.name} description={p.domain?.name ?? t('products.noDomain')} help={pageHelp('/products')}
                    actions={canEdit && (
                        <Space>
                            <Button onClick={() => actions.open(edit, record as unknown as Record<string, unknown>)}>{t('products.edit')}</Button>
                            <Button danger onClick={() => actions.open(remove, record as unknown as Record<string, unknown>)}>
                                {t('products.delete')}
                            </Button>
                        </Space>
                    )} />
                <TrustBadges certification={p.certification} qualityStatus={p.quality_status} />
                {p.description && <p style={{ marginTop: 10 }}>{p.description}</p>}
                <Space wrap align="center">
                    {requestable ? (
                        <Button type="primary" onClick={() => setRequesting(true)}>{t('products.requestAccess')}</Button>
                    ) : (
                        <Tag color={access.color}>{access.text}</Tag>
                    )}
                    <span>
                        <Switch checked={p.subscribed} onChange={(v) => void toggleSubscribe(v)} aria-label={t('products.subscribe')} />{' '}
                        {t('products.subscribe')}
                    </span>
                    <Muted>
                        {t('products.ownersLine', { owners: p.owners.map(actorName).join(', ') || '—', n: p.consumers })}
                    </Muted>
                    {p.domain && <Link to={`/domains/${encodeURIComponent(p.domain.urn)}`}>{t('products.openDomain')}</Link>}
                </Space>
            </Surface>
            <Tabs
                style={{ marginTop: 12 }}
                items={[
                    {
                        key: 'assets',
                        label: t('products.tab.assets', { n: p.assets.length }),
                        children: (
                            <Table
                                rowKey="urn"
                                size="small"
                                dataSource={p.assets}
                                pagination={false}
                                columns={[
                                    { title: t('products.name'), dataIndex: 'name', render: (v: string, r) => <Link to={assetPath(r.urn)}>{v}</Link> },
                                    { title: t('products.type'), dataIndex: 'type', render: (v: string) => entityTypeLabel(v) },
                                    { title: t('products.platform'), dataIndex: 'platform', render: (v: string | null) => platformName(v) },
                                    { title: t('products.outputPort'), render: (_, r) => (portSet.has(r.urn)
                                        ? <StatusBadge tone="ok" label={t('products.outputPort')} plain /> : null) },
                                ]}
                            />
                        ),
                    },
                    {
                        key: 'ports',
                        label: t('products.tab.ports', { n: portSet.size }),
                        children: portSet.size ? (
                            <ul>
                                {[...portSet].map((u) => <li key={u}><Link to={assetPath(u)}>{shortUrn(u)}</Link></li>)}
                            </ul>
                        ) : <Muted>{t('products.noPorts')}</Muted>,
                    },
                    {
                        key: 'contracts',
                        label: t('products.tab.contracts', { n: productContracts.length }),
                        children: productContracts.length ? (
                            <Table<Contract> rowKey="dataset" size="small" pagination={false} dataSource={productContracts}
                                columns={[
                                    { title: t('contracts.dataset'), render: (_, c) => <Link to={contractHref(c.dataset)}>{shortUrn(c.dataset)}</Link> },
                                    { title: t('contracts.status'), render: (_, c) => <ContractState status={c.status} /> },
                                    { title: t('contracts.version'), dataIndex: 'version' },
                                ]} />
                        ) : <Muted>{promises.error ? t('contracts.unavailable') : t('products.noContracts')}</Muted>,
                    },
                    {
                        key: 'lineage',
                        label: t('nav.lineage'),
                        children: lineageAsset ? (
                            <Surface style={{ padding: 16 }}>
                                <Select aria-label={t('products.lineageOf')} style={{ minWidth: 320, marginBottom: 12 }} value={lineageAsset}
                                    onChange={(v: string) => setLineageOf(v)}
                                    options={p.assets.map((a) => ({ value: a.urn, label: `${a.name}${portSet.has(a.urn) ? ` · ${t('products.outputPort')}` : ''}` }))} />
                                <LineageGraphView urn={lineageAsset} />
                            </Surface>
                        ) : <Muted>{t('products.noAssets')}</Muted>,
                    },
                    {
                        key: 'readme',
                        label: t('products.tab.about'),
                        children: (
                            <Surface style={{ padding: 18 }}>
                                {p.readme_md ? <MarkdownView source={p.readme_md} /> : <Muted>{t('products.noReadme')}</Muted>}
                                <p style={{ marginTop: 12 }}>
                                    {p.request_policy.auto_approve ? t('products.autoApprove')
                                        : t('products.reviewedBy', { who: p.request_policy.approvers.map(actorName).join(', ') || '—' })}
                                    {' '}{t('products.maxDays', { n: p.request_policy.max_duration_days })}
                                </p>
                                <Link to={`/marketplace/${encodeURIComponent(urn)}`}>{t('products.inMarketplace')}</Link>
                            </Surface>
                        ),
                    },
                    { key: 'slos', label: t('products.tab.slos'), children: <ProductSlos urn={urn} canEdit={canEdit} /> },
                ]}
            />
            {!record && ours.error instanceof CairnApiError && ours.error.status !== 404 && (
                <ErrorNotice error={ours.error} area={t('products.title')} />
            )}
            <RequestAccessDrawer product={p} open={requesting} onClose={() => setRequesting(false)}
                onSubmitted={() => cache.invalidate(key)} />
            {actions.dialog}
        </Page>
    );
}

/** Data contracts across assets: passing, failing or not checked yet, with the dataset and owner; published here. */
export function ContractsPage() {
    const t = useT();
    const { cache } = useConsumerApi();
    const location = useLocation();
    const history = useHistory();
    const status = new URLSearchParams(location.search).get('status') ?? '';
    const list = useDheQuery<Contract[]>('/quality/contracts', (c, signal) => listContracts(c, { signal }));
    const [creating, setCreating] = useState(false);
    const create = createAction(CONTRACTS);
    const rows = (list.data ?? []).filter((c) => !status || c.status === status);
    const n = (s: ContractStatus) => (list.data ?? []).filter((c) => c.status === s).length;
    return (
        <Page>
            <PageHeader title={t('contracts.title')} description={t('contracts.description')} help={pageHelp('/contracts')}
                actions={create && <Button type="primary" onClick={() => setCreating(true)}>{t('contracts.new')}</Button>} />
            <Space wrap style={{ margin: '16px 0' }} role="group" aria-label={t('contracts.status')}>
                {([['', t('contracts.all', { n: list.data?.length ?? 0 })], ['MET', `${t('contracts.passing')} ${n('MET')}`],
                    ['BROKEN', `${t('contracts.failing')} ${n('BROKEN')}`], ['PENDING', `${t('contracts.pending')} ${n('PENDING')}`]] as const)
                    .map(([v, label]) => (
                        <Button key={v || 'all'} type={status === v ? 'primary' : 'default'} aria-pressed={status === v}
                            onClick={() => history.replace(v ? `/contracts?status=${v}` : '/contracts')}>{label}</Button>
                    ))}
            </Space>
            {list.error && <ErrorNotice error={list.error} area={t('contracts.title')} />}
            {list.loading && !list.data && <Skeleton h={160} />}
            {list.data && (
                <Table<Contract>
                    rowKey="dataset"
                    dataSource={rows}
                    pagination={rows.length > 25 ? { pageSize: 25, showSizeChanger: false } : false}
                    locale={{ emptyText: <Empty description={t('contracts.empty')} /> }}
                    columns={[
                        { title: t('contracts.dataset'), render: (_, c) => (
                            <span>
                                <Link to={contractHref(c.dataset)}>{shortUrn(c.dataset)}</Link>
                                <Muted style={{ display: 'block', fontSize: 12 }}>{platformName(/dataPlatform:([^,]+)/.exec(c.dataset)?.[1])}</Muted>
                            </span>
                        ) },
                        { title: t('contracts.status'), render: (_, c) => <ContractState status={c.status} /> },
                        { title: t('contracts.version'), dataIndex: 'version' },
                        { title: t('contracts.owner'), render: (_, c) => (c.owner ? actorName(c.owner) : <Muted>—</Muted>) },
                        { title: t('contracts.checked'), render: (_, c) => relativeTime(new Date(c.checked_ms).toISOString()) },
                    ]}
                />
            )}
            {creating && create && (
                <ActionDialog spec={CONTRACTS} action={create} row={null} onClose={() => setCreating(false)}
                    onDone={() => cache.invalidate('/quality/contracts')} />
            )}
        </Page>
    );
}

interface Spec {
    description?: string;
    schema?: { columns: { name: string; type?: string | null; required?: boolean }[]; allow_extra_columns?: boolean } | null;
    freshness?: { max_age_hours: number } | null;
    volume?: { min_rows?: number | null; max_rows?: number | null } | null;
    quality?: { name: string; type: string; severity?: string }[];
}

/** One contract: its terms (schema, freshness, volume, quality rules) and whether each holds now. */
export function ContractPage() {
    const { dataset: raw } = useParams<{ dataset: string }>();
    const dataset = decodeURIComponent(raw);
    const t = useT();
    const { cache } = useConsumerApi();
    const one = useDheQuery<Contract>(`/quality/contracts/one?${dataset}`, (c, signal) => c.contract(dataset, { signal }));
    const [editing, setEditing] = useState(false);
    const create = createAction(CONTRACTS);
    const spec = (one.data?.spec ?? {}) as Spec;
    return (
        <Page>
            <PageHeader title={shortUrn(dataset)} description={t('contracts.of')} help={pageHelp('/contracts')}
                actions={one.data && create && <Button onClick={() => setEditing(true)}>{t('contracts.newVersion')}</Button>} />
            <p><Link to={assetPath(dataset)}>{t('contracts.openAsset')}</Link></p>
            <Surface style={{ padding: 18 }}>
                <ContractPanel urn={dataset} />
            </Surface>
            {one.data && (
                <Surface as="section" style={{ padding: 18, marginTop: 16 }} aria-label={t('contracts.terms')}>
                    <h2 style={{ margin: '0 0 8px', fontSize: 16 }}>{t('contracts.terms')}</h2>
                    {spec.description && <p>{spec.description}</p>}
                    {spec.schema && (
                        <>
                            <h3 style={{ fontSize: 14 }}>{t('contracts.schema')}</h3>
                            <Table rowKey="name" size="small" pagination={false} dataSource={spec.schema.columns}
                                columns={[
                                    { title: t('lineage.column'), dataIndex: 'name', render: (v: string) => <code>{v}</code> },
                                    { title: t('products.type'), dataIndex: 'type' },
                                    { title: t('contracts.required'), dataIndex: 'required', render: (v?: boolean) => (v ? t('contracts.yes') : '') },
                                ]} />
                            <Muted>{spec.schema.allow_extra_columns === false ? t('contracts.noExtra') : t('contracts.extraOk')}</Muted>
                        </>
                    )}
                    {spec.freshness && <p>{t('contracts.freshness', { h: spec.freshness.max_age_hours })}</p>}
                    {spec.volume && (spec.volume.min_rows != null || spec.volume.max_rows != null) && (
                        <p>{t('contracts.volume', { min: spec.volume.min_rows ?? '—', max: spec.volume.max_rows ?? '—' })}</p>
                    )}
                    {(spec.quality ?? []).length > 0 && (
                        <ul>{spec.quality!.map((r) => <li key={r.name}>{r.name} <Muted>({r.type}{r.severity ? `, ${r.severity}` : ''})</Muted></li>)}</ul>
                    )}
                </Surface>
            )}
            {editing && create && one.data && (
                <ActionDialog spec={CONTRACTS} action={create} row={one.data as unknown as Record<string, unknown>}
                    onClose={() => setEditing(false)}
                    onDone={() => {
                        cache.invalidate('/quality/contracts');
                        cache.invalidate(`/quality/contracts/one?${dataset}`);
                    }} />
            )}
        </Page>
    );
}
