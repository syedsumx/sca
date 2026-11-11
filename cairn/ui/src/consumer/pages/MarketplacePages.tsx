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
import { actorName, entityTypeLabel, platformName, relativeTime } from '../utils/format';
import { RESOURCE_SPECS } from '../resources/catalog';
import { ResourceView } from '../resources/ResourceView';
import { useT } from '../i18n';
import { ErrorNotice, notifyError } from '../components/ErrorNotice';
import { PageHeader } from '../guide/PageHeader';
import { pageHelp } from '../guide/help';

/**
 * Data products › Browse (L149: what was the Marketplace): consumer-oriented product cards with filters and access
 * requests, and the agents and skills listed for others to use. Its state is in the URL (`/products?view=browse&q=…`).
 */
export function ProductBrowse() {
    const t = useT();
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
        next.set('view', 'browse');
        history.replace(`/products?${next.toString()}`);
    };
    const domains = useMemo(
        () => Array.from(new Set((products.data ?? []).map((p) => p.domain?.name).filter(Boolean) as string[])),
        [products.data],
    );
    const [domain, setDomain] = useState<string | null>(null);
    const shown = (products.data ?? []).filter((p) => !domain || p.domain?.name === domain);

    return (
        <section aria-label={t('page.marketplace')}>
            <Muted style={{ display: 'block' }}>{t('page.marketplaceIntro')}</Muted>
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
            {products.error && <ErrorNotice error={products.error} area="Data products" />}
            {products.data && shown.length === 0 && <Empty description="No data products match these filters." />}
            <CardGrid>
                {shown.map((p) => (
                    <ProductCard key={p.urn} product={p} onRequest={setRequesting} />
                ))}
            </CardGrid>
            <RequestAccessDrawer product={requesting} open={!!requesting} onClose={() => setRequesting(null)} />
            <section aria-label="Agents and skills" style={{ marginTop: 32 }}>
                <SectionTitle>Agents and skills</SectionTitle>
                <ResourceView spec={RESOURCE_SPECS['agent-listings']} />
            </section>
        </section>
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
    const t = useT();
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
            notifyError(e);
        } finally {
            setBusy(false);
        }
    };

    const columns = (scope: 'mine' | 'inbox') => [
        {
            title: 'Product',
            dataIndex: 'product_name',
            render: (v: string, r: AccessRequest) => <Link to={`/products/${encodeURIComponent(r.product_urn)}`}>{v}</Link>,
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
                    {(r.provisioning ?? []).map((p) => (
                        <div key={`${p.target}${p.what}`} style={{ fontSize: 12 }}>
                            <Tag color={p.status === 'failed' ? 'red' : p.status === 'granted' ? 'green' : 'default'} title={p.error}>
                                {p.what} on {p.target}: {p.status === 'failed' ? `${p.action ?? 'grant'} failed` : p.status}
                            </Tag>
                        </div>
                    ))}
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
            <PageHeader title={t('page.requests')} help={pageHelp('/requests')}
                description="Access you asked for and requests waiting for your decision, compliance forms to complete, and your grants." />
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
                                locale={{ emptyText: <Link to="/products?view=browse">Browse data products to request data →</Link> }}
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
