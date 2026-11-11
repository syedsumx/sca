import React from 'react';
import { Link } from 'react-router-dom';
import styled from 'styled-components';

import type { Monitor } from '../../api/types';
import { contracts as listContracts } from '../api/catalog';
import { useDheQuery } from '../api/hooks';
import type { AccessRequest, Contract, ProductCard } from '../api/types';
import { useDheSession } from '../context/CairnProvider';
import { useT } from '../i18n';
import { shortUrn } from '../utils/format';
import { assetPath } from './Assets';
import { Card } from './HomeCards';
import { StatusBadge } from './StatusBadge';
import { Muted, Skeleton, tokens } from './tokens';
import { TrustBadges } from './TrustBadges';

const List = styled.ul`
    list-style: none;
    margin: 0;
    padding: 0;
    li {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 8px;
        min-height: 32px;
        font-size: 14px;
        border-bottom: 1px solid ${tokens.color.border};
    }
    li:last-child {
        border-bottom: 0;
    }
`;

/** An issue on something the person owns: a failing contract or a failing quality check (Cairn's own records). */
export interface Issue {
    urn: string;
    kind: 'contract' | 'check';
    name: string;
    to: string;
}

/** The person's issues: contracts they own (or published) that are failing, and their monitors whose last check failed. */
export function issuesFor(me: string | undefined, contracts: Contract[], monitors: Monitor[]): Issue[] {
    if (!me) return [];
    const out: Issue[] = contracts
        .filter((c) => c.status === 'BROKEN' && (c.owner === me || c.created_by === me))
        .map((c) => ({ urn: c.dataset, kind: 'contract', name: shortUrn(c.dataset), to: `/contracts/${encodeURIComponent(c.dataset)}` }));
    for (const m of monitors) {
        const by = (m as Monitor & { created_by?: string | null }).created_by;
        const status = m.last_evaluation?.status;
        if (by === me && (status === 'FAIL' || status === 'ERROR')) {
            out.push({ urn: m.dataset_urn, kind: 'check', name: `${shortUrn(m.dataset_urn)} · ${m.name}`, to: assetPath(m.dataset_urn) });
        }
    }
    return out;
}

/** Home, catalog first: the person's data products and domains, their open requests, and issues on what they own. */
export function CatalogHome() {
    const t = useT();
    const session = useDheSession();
    const me = session.config?.user.urn;
    const products = useDheQuery<ProductCard[]>('/marketplace/products?sort=name', (c, signal) =>
        c.listProducts({ sort: 'name' }, { signal }));
    const requests = useDheQuery<AccessRequest[]>('/marketplace/access-requests?scope=mine', (c, signal) =>
        c.listAccessRequests({ scope: 'mine' }, { signal }));
    const contracts = useDheQuery<Contract[]>('/quality/contracts', (c, signal) => listContracts(c, { signal }));
    const monitors = useDheQuery<Monitor[]>('/quality/monitors', (c, signal) => c.listMonitors({ signal }));
    const mine = (products.data ?? []).filter((p) => !!me && p.owners.includes(me));
    const domains = new Map<string, string>();
    for (const p of mine) if (p.domain) domains.set(p.domain.urn, p.domain.name || shortUrn(p.domain.urn));
    const open = (requests.data ?? []).filter((r) => r.state === 'PENDING' || r.state === 'APPROVED');
    const issues = issuesFor(me, contracts.data ?? [], monitors.data ?? []);
    return (
        <div style={{ display: 'grid', gap: 16, gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))' }} aria-label={t('home.catalog')}
            role="region">
            <Card aria-labelledby="home-products">
                <header><h2 id="home-products">{t('home.yourProducts')}</h2><Link to="/products">{t('nav.products')}</Link></header>
                {products.loading && !products.data && <Skeleton h={48} />}
                {products.data && mine.length === 0 && <Muted>{t('home.noProducts')}</Muted>}
                <List>
                    {mine.slice(0, 6).map((p) => (
                        <li key={p.urn}>
                            <Link to={`/products/${encodeURIComponent(p.urn)}`}>{p.name}</Link>
                            <TrustBadges certification={p.certification} qualityStatus={p.quality_status} compact />
                        </li>
                    ))}
                </List>
                {domains.size > 0 && (
                    <Muted>
                        {t('home.yourDomains')}:{' '}
                        {[...domains.entries()].map(([urn, name], i) => (
                            <React.Fragment key={urn}>{i > 0 && ', '}<Link to={`/domains/${encodeURIComponent(urn)}`}>{name}</Link></React.Fragment>
                        ))}
                    </Muted>
                )}
            </Card>
            <Card aria-labelledby="home-requests">
                <header><h2 id="home-requests">{t('home.yourRequests')}</h2><Link to="/requests">{t('nav.requests')}</Link></header>
                {requests.loading && !requests.data && <Skeleton h={48} />}
                {requests.data && open.length === 0 && <Muted>{t('home.noRequests')}</Muted>}
                <List>
                    {open.slice(0, 6).map((r) => (
                        <li key={r.id}>
                            <Link to={`/products/${encodeURIComponent(r.product_urn)}`}>{r.product_name}</Link>
                            <StatusBadge state={r.state === 'PENDING' ? 'pending' : 'approved'} plain />
                        </li>
                    ))}
                </List>
            </Card>
            <Card aria-labelledby="home-issues">
                <header><h2 id="home-issues">{t('home.issues')}</h2><Link to="/contracts?status=BROKEN">{t('nav.contracts')}</Link></header>
                {(contracts.loading || monitors.loading) && !contracts.data && <Skeleton h={48} />}
                {contracts.data && issues.length === 0 && <Muted>{t('home.noIssues')}</Muted>}
                <List>
                    {issues.slice(0, 6).map((i) => (
                        <li key={`${i.kind}:${i.to}:${i.name}`} data-issue={i.kind}>
                            <Link to={i.to}>{i.name}</Link>
                            <StatusBadge tone="error" label={t(i.kind === 'contract' ? 'home.contractFailing' : 'home.checkFailing')} plain />
                        </li>
                    ))}
                </List>
            </Card>
        </div>
    );
}
