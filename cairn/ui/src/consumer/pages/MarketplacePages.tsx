import { Alert, Button, Empty, Input, Modal, Radio, Select, Switch, Table, Tabs, Tag, message } from 'antd';
import React, { useMemo, useState } from 'react';
import { Link, useHistory, useLocation, useParams } from 'react-router-dom';

import type { ProductAccessFilter, ProductSort } from '../api/client';
import { useConsumerApi, useDheQuery } from '../api/hooks';
import type {
    AccessRequest,
    AccessRequestAction,
    FormTask,
    ProductCard as ProductCardData,
    ProductDetail,
} from '../api/types';
import { assetPath } from '../components/Assets';
import { ProductCard, RequestAccessDrawer, accessLabel } from '../components/Marketplace';
import { CardGrid, Muted, Page, SectionTitle, Skeleton, Surface, tokens } from '../components/tokens';
import { FormTasksList } from '../components/Forms';
import { TrustBadges } from '../components/TrustBadges';
import { actorName, entityTypeLabel, platformName, relativeTime } from '../utils/format';
import { MarkdownView } from '../utils/markdown';

export function MarketplacePage() {
    const location = useLocation();
    const history = useHistory();
    const params = new URLSearchParams(location.search);
    const q = params.get('q') ?? '';
    const accessParam = params.get('access');
    const access: ProductAccessFilter | '' = accessParam === 'mine' || accessParam === 'requestable' ? accessParam : '';
    const certified = params.get('certified') === 'true';
    const sortParam = params.get('sort');
    const sort: ProductSort = sortParam === 'name' || sortParam === 'relevance' ? sortParam : 'consumers';
    const [requesting, setRequesting] = useState<ProductCardData | null>(null);
    const key = `/marketplace/products?${params.toString()}`;
    const products = useDheQuery<ProductCardData[]>(key, (c, signal) =>
        c.listProducts({ q: q || undefined, access: access || undefined, certified: certified || undefined, sort }, { signal }),
    );
    const update = (k: string, v: string | null) => {
        const next = new URLSearchParams(location.search);
        if (v) next.set(k, v);
        else next.delete(k);
        history.replace(`/marketplace?${next.toString()}`);
    };
    const domains = useMemo(
        () => Array.from(new Set((products.data ?? []).map((p) => p.domain?.name).filter(Boolean) as string[])),
        [products.data],
    );
    const [domain, setDomain] = useState<string | null>(null);
    const shown = (products.data ?? []).filter((p) => !domain || p.domain?.name === domain);

    return (
        <Page>
            <SectionTitle as="h1" style={{ fontSize: 24 }}>
                Data Marketplace
            </SectionTitle>
            <Muted>Curated, owned and quality-checked data products. Request access in seconds — no tickets.</Muted>
            <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', margin: '16px 0', alignItems: 'center' }}>
                <Input.Search
                    aria-label="Search data products"
                    placeholder="Search products…"
                    defaultValue={q}
                    allowClear
                    style={{ maxWidth: 320 }}
                    onSearch={(v) => update('q', v || null)}
                />
                <Select
                    aria-label="Domain"
                    allowClear
                    placeholder="Domain"
                    style={{ minWidth: 160 }}
                    value={domain ?? undefined}
                    onChange={(v) => setDomain(v ?? null)}
                    options={domains.map((d) => ({ value: d, label: d }))}
                />
                <Radio.Group
                    aria-label="Access"
                    optionType="button"
                    value={access}
                    onChange={(e) => update('access', e.target.value || null)}
                    options={[
                        { label: 'All', value: '' },
                        { label: 'Mine', value: 'mine' },
                        { label: 'Requestable', value: 'requestable' },
                    ]}
                />
                <span>
                    <Switch size="small" checked={certified} onChange={(v) => update('certified', v ? 'true' : null)} aria-label="Certified only" />{' '}
                    Certified only
                </span>
                <Select
                    aria-label="Sort"
                    value={sort}
                    style={{ minWidth: 150 }}
                    onChange={(v) => update('sort', v)}
                    options={[
                        { value: 'consumers', label: 'Most used' },
                        { value: 'relevance', label: 'Best match' },
                        { value: 'name', label: 'Name' },
                    ]}
                />
            </div>
            {products.loading && (
                <CardGrid aria-busy="true">
                    {[0, 1, 2].map((i) => (
                        <Surface key={i} style={{ padding: 16 }}>
                            <Skeleton h={18} w="50%" />
                            <div style={{ height: 10 }} />
                            <Skeleton h={60} />
                        </Surface>
                    ))}
                </CardGrid>
            )}
            {products.error && <Alert type="error" message={`Could not load products: ${products.error.message}`} />}
            {products.data && shown.length === 0 && <Empty description="No data products match these filters." />}
            <CardGrid>
                {shown.map((p) => (
                    <ProductCard key={p.urn} product={p} onRequest={setRequesting} />
                ))}
            </CardGrid>
            <RequestAccessDrawer product={requesting} open={!!requesting} onClose={() => setRequesting(null)} />
        </Page>
    );
}

export function ProductPage() {
    const { urn: raw } = useParams<{ urn: string }>();
    const urn = decodeURIComponent(raw);
    const { client, cache } = useConsumerApi();
    const key = `/marketplace/products/${urn}`;
    const product = useDheQuery<ProductDetail>(key, (c, signal) => c.getProduct(urn, { signal }));
    const [requesting, setRequesting] = useState(false);
    const p = product.data;

    const toggleSubscribe = async (on: boolean) => {
        cache.set<ProductDetail>(key, (prev) => ({ ...(prev as ProductDetail), subscribed: on }));
        try {
            await client.subscribe(urn, on);
            message.success(on ? 'Subscribed — you will hear about schema changes, incidents and deprecations.' : 'Unsubscribed');
        } catch (e) {
            cache.set<ProductDetail>(key, (prev) => ({ ...(prev as ProductDetail), subscribed: !on }));
            message.error((e as Error).message);
        }
    };

    if (product.loading) {
        return (
            <Page>
                <Skeleton h={28} w="40%" />
            </Page>
        );
    }
    if (product.error || !p) return <Page>{product.error && <Alert type="error" message={product.error.message} />}</Page>;
    const access = accessLabel(p.access.status, p.access.expires_at);
    const requestable = ['NONE', 'EXPIRED', 'REJECTED'].includes(p.access.status);
    return (
        <Page>
            <Link to="/marketplace">← Marketplace</Link>
            <Surface style={{ padding: 20, marginTop: 12 }}>
                <Muted>{p.domain?.name ?? 'Data product'}</Muted>
                <h1 style={{ margin: '4px 0 8px', fontSize: 24 }}>{p.name}</h1>
                <TrustBadges certification={p.certification} qualityStatus={p.quality_status} />
                <p style={{ marginTop: 10 }}>{p.description}</p>
                <div style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
                    {requestable ? (
                        <Button type="primary" onClick={() => setRequesting(true)}>
                            Request access
                        </Button>
                    ) : (
                        <Tag color={access.color}>{access.text}</Tag>
                    )}
                    <span>
                        <Switch checked={p.subscribed} onChange={(v) => void toggleSubscribe(v)} aria-label="Subscribe to updates" /> Subscribe to
                        updates
                    </span>
                    <Muted>
                        Owners: {p.owners.map(actorName).join(', ') || '—'} · {p.consumers} consumers
                        {p.sla.availability ? ` · SLA ${p.sla.availability}` : ''}
                        {p.sla.freshness ? ` · fresh ${p.sla.freshness}` : ''}
                    </Muted>
                </div>
            </Surface>
            <Tabs
                style={{ marginTop: 12 }}
                items={[
                    {
                        key: 'readme',
                        label: 'Overview',
                        children: (
                            <Surface style={{ padding: 18 }}>
                                {p.readme_md ? <MarkdownView source={p.readme_md} /> : <Muted>No README yet.</Muted>}
                            </Surface>
                        ),
                    },
                    {
                        key: 'assets',
                        label: `Assets (${p.assets.length})`,
                        children: (
                            <Table
                                rowKey="urn"
                                size="small"
                                dataSource={p.assets}
                                pagination={false}
                                columns={[
                                    { title: 'Name', dataIndex: 'name', render: (v: string, r) => <Link to={assetPath(r.urn)}>{v}</Link> },
                                    { title: 'Type', dataIndex: 'type', render: (v: string) => entityTypeLabel(v) },
                                    { title: 'Platform', dataIndex: 'platform', render: (v: string | null) => platformName(v) },
                                ]}
                            />
                        ),
                    },
                    {
                        key: 'policy',
                        label: 'Access policy',
                        children: (
                            <Surface style={{ padding: 18 }}>
                                <p>
                                    {p.request_policy.auto_approve
                                        ? '✅ Eligible requests are approved automatically.'
                                        : `Requests are reviewed by ${p.request_policy.approvers.map(actorName).join(', ') || 'the owners'}.`}
                                </p>
                                <p>Approved purposes: {p.request_policy.purposes.join(', ') || 'any (with justification)'}</p>
                                <p>Maximum duration: {p.request_policy.max_duration_days} days — access ends automatically.</p>
                            </Surface>
                        ),
                    },
                ]}
            />
            <RequestAccessDrawer
                product={p}
                open={requesting}
                onClose={() => setRequesting(false)}
                onSubmitted={() => cache.invalidate(key)}
            />
        </Page>
    );
}

const STATE_COLOR: Record<string, string> = {
    PENDING: 'blue',
    APPROVED: 'green',
    REJECTED: 'red',
    CANCELLED: 'default',
    EXPIRED: 'orange',
    REVOKED: 'red',
};

export function RequestsPage() {
    const { client, cache } = useConsumerApi();
    const mine = useDheQuery<AccessRequest[]>('/marketplace/access-requests?scope=mine', (c, signal) =>
        c.listAccessRequests({ scope: 'mine' }, { signal }),
    );
    const inbox = useDheQuery<AccessRequest[]>('/marketplace/access-requests?scope=inbox', (c, signal) =>
        c.listAccessRequests({ scope: 'inbox' }, { signal }),
    );
    const forms = useDheQuery<FormTask[]>('/governance/forms/tasks', (c, signal) => c.formTasks('PENDING', { signal }));
    const [dialog, setDialog] = useState<{ req: AccessRequest; action: AccessRequestAction } | null>(null);
    const [comment, setComment] = useState('');
    const [busy, setBusy] = useState(false);

    const act = async () => {
        if (!dialog) return;
        setBusy(true);
        try {
            await client.actOnAccessRequest(dialog.req.id, dialog.action, comment || undefined);
            message.success(`Request ${dialog.action === 'approve' ? 'approved' : `${dialog.action}ed`}`);
            cache.invalidate('/marketplace');
            setDialog(null);
            setComment('');
        } catch (e) {
            message.error((e as Error).message);
        } finally {
            setBusy(false);
        }
    };

    const columns = (scope: 'mine' | 'inbox') => [
        {
            title: 'Product',
            dataIndex: 'product_name',
            render: (v: string, r: AccessRequest) => <Link to={`/marketplace/${encodeURIComponent(r.product_urn)}`}>{v}</Link>,
        },
        ...(scope === 'inbox' ? [{ title: 'Requester', dataIndex: 'requester', render: (v: string) => actorName(v) }] : []),
        { title: 'Purpose', dataIndex: 'purpose' },
        { title: 'Duration', dataIndex: 'duration_days', render: (v: number) => `${v} days` },
        {
            title: 'Status',
            dataIndex: 'state',
            render: (v: string, r: AccessRequest) => (
                <span>
                    <Tag color={STATE_COLOR[v]}>{v}</Tag>
                    {r.auto_approved && <Muted>auto</Muted>}
                    {r.expires_at && v === 'APPROVED' && <Muted> until {new Date(r.expires_at).toLocaleDateString()}</Muted>}
                </span>
            ),
        },
        { title: 'Requested', dataIndex: 'created_at', render: (v: string) => relativeTime(v) },
        {
            title: '',
            key: 'actions',
            render: (_: unknown, r: AccessRequest) => {
                if (scope === 'mine' && r.state === 'PENDING')
                    return (
                        <Button size="small" onClick={() => setDialog({ req: r, action: 'cancel' })}>
                            Cancel
                        </Button>
                    );
                if (scope === 'inbox' && r.state === 'PENDING')
                    return (
                        <span style={{ display: 'flex', gap: 6 }}>
                            <Button size="small" type="primary" onClick={() => setDialog({ req: r, action: 'approve' })}>
                                Approve
                            </Button>
                            <Button size="small" danger onClick={() => setDialog({ req: r, action: 'reject' })}>
                                Reject
                            </Button>
                        </span>
                    );
                if (scope === 'inbox' && r.state === 'APPROVED')
                    return (
                        <Button size="small" danger onClick={() => setDialog({ req: r, action: 'revoke' })}>
                            Revoke
                        </Button>
                    );
                return null;
            },
        },
    ];

    return (
        <Page>
            <SectionTitle as="h1" style={{ fontSize: 24 }}>
                Requests
            </SectionTitle>
            <Tabs
                items={[
                    {
                        key: 'inbox',
                        label: `Approvals${inbox.data?.filter((r) => r.state === 'PENDING').length ? ` (${inbox.data.filter((r) => r.state === 'PENDING').length})` : ''}`,
                        children: (
                            <Table
                                rowKey="id"
                                loading={inbox.loading}
                                dataSource={inbox.data ?? []}
                                columns={columns('inbox')}
                                locale={{ emptyText: 'Nothing waiting for your approval 🎉' }}
                                expandable={{
                                    expandedRowRender: (r) => (
                                        <div>
                                            <p style={{ marginBottom: 4 }}>
                                                <strong>Justification:</strong> {r.justification}
                                            </p>
                                            {r.history.map((h) => (
                                                <div key={`${h.at}-${h.to_state}`} style={{ fontSize: 12, color: tokens.color.textMuted }}>
                                                    {relativeTime(h.at)} · {actorName(h.actor)} → {h.to_state}
                                                    {h.comment ? ` — ${h.comment}` : ''}
                                                </div>
                                            ))}
                                        </div>
                                    ),
                                }}
                            />
                        ),
                    },
                    {
                        key: 'mine',
                        label: 'My requests',
                        children: (
                            <Table
                                rowKey="id"
                                loading={mine.loading}
                                dataSource={mine.data ?? []}
                                columns={columns('mine')}
                                locale={{ emptyText: <Link to="/marketplace">Browse the marketplace to request data →</Link> }}
                            />
                        ),
                    },
                    {
                        key: 'forms',
                        label: `Forms${forms.data?.length ? ` (${forms.data.length})` : ''}`,
                        children: <FormTasksList />,
                    },
                ]}
            />
            <Modal
                open={!!dialog}
                title={dialog ? `${dialog.action.charAt(0).toUpperCase()}${dialog.action.slice(1)} request · ${dialog.req.product_name}` : ''}
                onCancel={() => setDialog(null)}
                onOk={() => void act()}
                okText="Confirm"
                confirmLoading={busy}
                okButtonProps={{ danger: dialog?.action === 'reject' || dialog?.action === 'revoke' }}
            >
                {dialog && (
                    <p>
                        {actorName(dialog.req.requester)} · {dialog.req.purpose} · {dialog.req.duration_days} days
                    </p>
                )}
                <Input.TextArea
                    aria-label="Comment"
                    placeholder="Optional comment (shared with the requester)"
                    value={comment}
                    onChange={(e) => setComment(e.target.value)}
                />
            </Modal>
        </Page>
    );
}
