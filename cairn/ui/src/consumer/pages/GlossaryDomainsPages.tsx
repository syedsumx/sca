import { Alert, Button, Empty, Input, Space, Table } from 'antd';
import React, { useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import styled from 'styled-components';

import {
    CatalogDomain, CatalogNode, CatalogTerm, DomainDetail, DomainSummary, EditRights, GlossaryGroup, GlossaryRoot,
    GlossaryTermDetail, NamedEntity, catalogDomain, catalogNode, catalogTerm, domain as loadDomain, domainRights,
    domains as loadDomains, glossaryNode, glossaryRights, glossaryRoot, glossaryTerm, nameOf, ownerUrns,
} from '../api/catalog';
import { useDheQuery } from '../api/hooks';
import type { EnrichmentMap, ProductCard, SearchResponse } from '../api/types';
import { AssetRow } from '../components/Assets';
import { ErrorNotice } from '../components/ErrorNotice';
import {
    AddToDomain, ApplyTerm, DOMAINS, Deprecated, NODES, ReadOnlyReason, TERMS, specAction, useEditDialog,
} from '../components/GlossaryEdit';
import { CardGrid, Muted, Page, Skeleton, Surface, focusRing, tokens } from '../components/tokens';
import { TrustBadges } from '../components/TrustBadges';
import { useCoreHref } from '../context/NativeLinks';
import { PageHeader } from '../guide/PageHeader';
import { pageHelp } from '../guide/help';
import { useT } from '../i18n';
import { actorName } from '../utils/format';
import { MarkdownView } from '../utils/markdown';
import { productHref } from './ProductsPages';

export function glossaryHref(urn: string): string {
    return `/glossary/${encodeURIComponent(urn)}`;
}

export function domainHref(urn: string): string {
    return `/domains/${encodeURIComponent(urn)}`;
}

const Tile = styled(Link)`
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 14px 16px;
    background: ${tokens.color.surface};
    border: 1px solid ${tokens.color.border};
    border-radius: ${tokens.radius.md};
    color: ${tokens.color.text};
    ${focusRing}
    &:hover {
        border-color: ${tokens.color.primary};
        color: ${tokens.color.text};
    }
    strong {
        font-size: 15px;
    }
    span {
        font-size: 13px;
        color: ${tokens.color.textMuted};
    }
`;

/** The glossary and domains live in Cairn Core; what is changed here is written there. A link to the entry's page
 *  in Core when a catalog view is configured. */
function InCore({ urn }: { urn?: string }) {
    const t = useT();
    const href = useCoreHref(urn ?? '');
    return (
        <Muted style={{ display: 'block', marginTop: 4 }} data-in-core="">
            {t('core.writtenToCore')}{' '}
            {urn && href && <a href={href}>{t('core.openInCore')}</a>}
        </Muted>
    );
}

/** What the person may do on an entry (or at the top level): the policy engine's answer through Cairn's API. */
function useRights(kind: 'glossary' | 'domain', urn?: string) {
    return useDheQuery<EditRights>(`/catalog/${kind === 'glossary' ? 'glossary' : 'domains'}/rights?${urn ?? ''}`, (c, signal) =>
        (kind === 'glossary' ? glossaryRights : domainRights)(c, urn, { signal }), { staleMs: 60_000 }).data;
}

const asRow = (r: unknown) => r as Record<string, unknown>;

function owners(o: GlossaryTermDetail['ownership']): string {
    return ownerUrns(o).map(actorName).join(', ');
}

/** Assets that carry a term, or are in a domain: Core's search, filtered on it. */
function TaggedAssets({ field, urn, label }: { field: string; urn: string; label: string }) {
    const t = useT();
    const q = useDheQuery<SearchResponse>(`gql:search?${field}=${urn}`, (c, signal) =>
        c.search({ query: '*', filters: { [field]: [urn] }, start: 0, count: 50 }, signal), { staleMs: 15_000 });
    const urns = (q.data?.results ?? []).map((r) => r.urn);
    const enrichment = useDheQuery<EnrichmentMap>(urns.length ? `/collab/enrich?${urns.join('|')}` : null, (c, signal) =>
        c.enrich(urns, { signal }));
    return (
        <section aria-label={label} style={{ marginTop: 16 }}>
            <h2 style={{ fontSize: 16, margin: '0 0 8px' }}>{label} {q.data ? <Muted>({q.data.total.toLocaleString()})</Muted> : null}</h2>
            {q.loading && !q.data && <Skeleton h={60} />}
            {q.error && <ErrorNotice error={q.error} area={label} />}
            {q.data && q.data.results.length === 0 && <Muted>{t('glossary.noAssets')}</Muted>}
            {q.data?.results.filter((r) => r.type !== 'DATA_PRODUCT').map((r) => (
                <AssetRow key={r.urn} asset={r} enrichment={enrichment.data?.[r.urn]} />
            ))}
        </section>
    );
}

/** The glossary: term groups and terms from Cairn Core, with a filter. */
export function GlossaryPage() {
    const t = useT();
    const root = useDheQuery<GlossaryRoot>('gql:glossary-root', (c, signal) => glossaryRoot(c, signal), { staleMs: 60_000 });
    const [filter, setFilter] = useState('');
    const rights = useRights('glossary');
    const edit = useEditDialog('/glossary');
    const f = filter.trim().toLowerCase();
    const match = (e: NamedEntity) => !f || `${nameOf(e)} ${e.properties?.description ?? ''}`.toLowerCase().includes(f);
    return (
        <Page>
            <PageHeader title={t('glossary.title')} description={t('glossary.description')} help={pageHelp('/glossary')}
                actions={rights?.may_manage && (
                    <Space>
                        <Button onClick={() => edit.show(NODES, specAction(NODES, 'POST'))}>{t('edit.newGroup')}</Button>
                        <Button type="primary" onClick={() => edit.show(TERMS, specAction(TERMS, 'POST', '/catalog/glossary/terms'))}>
                            {t('edit.newTerm')}
                        </Button>
                    </Space>
                )} />
            <InCore />
            <ReadOnlyReason rights={rights} what="glossary" />
            <Input.Search aria-label={t('glossary.filter')} placeholder={t('glossary.filter')} allowClear value={filter}
                onChange={(e) => setFilter(e.target.value)} style={{ maxWidth: 420, margin: '16px 0' }} />
            {root.error && <ErrorNotice error={root.error} area={t('glossary.title')} />}
            {root.loading && !root.data && <Skeleton h={120} />}
            {root.data && root.data.nodes.length + root.data.terms.length === 0 && <Empty description={t('glossary.empty')} />}
            {root.data && root.data.nodes.length > 0 && (
                <section aria-labelledby="glossary-groups">
                    <h2 id="glossary-groups" style={{ fontSize: 16 }}>{t('glossary.groups')}</h2>
                    <CardGrid>
                        {root.data.nodes.filter(match).map((n) => (
                            <Tile key={n.urn} to={glossaryHref(n.urn)}>
                                <strong>{nameOf(n)}</strong>
                                {n.properties?.description && <span>{n.properties.description}</span>}
                                {n.childrenCount && (
                                    <span>{t('glossary.counts', { terms: n.childrenCount.termsCount, groups: n.childrenCount.nodesCount })}</span>
                                )}
                            </Tile>
                        ))}
                    </CardGrid>
                </section>
            )}
            {root.data && root.data.terms.length > 0 && (
                <section aria-labelledby="glossary-terms" style={{ marginTop: 24 }}>
                    <h2 id="glossary-terms" style={{ fontSize: 16 }}>{t('glossary.terms')}</h2>
                    <TermTable terms={root.data.terms.filter(match)} />
                </section>
            )}
            {edit.dialog}
        </Page>
    );
}

function TermTable({ terms }: { terms: NamedEntity[] }) {
    const t = useT();
    return (
        <Table<NamedEntity> rowKey="urn" size="small" dataSource={terms}
            pagination={terms.length > 50 ? { pageSize: 50, showSizeChanger: false } : false}
            columns={[
                { title: t('glossary.term'), render: (_, e) => <Link to={glossaryHref(e.urn)}>{nameOf(e)}</Link> },
                { title: t('glossary.definition'), render: (_, e) => e.properties?.description || e.properties?.definition || '' },
            ]} />
    );
}

/** A term group (its terms and groups) or a term (definition, related terms, stewards, the assets that use it). */
export function GlossaryEntryPage() {
    const { urn: raw } = useParams<{ urn: string }>();
    const urn = decodeURIComponent(raw);
    return urn.startsWith('urn:li:glossaryNode:') ? <GlossaryGroupView urn={urn} /> : <GlossaryTermView urn={urn} />;
}

function Parents({ nodes }: { nodes: NamedEntity[] | undefined }) {
    if (!nodes?.length) return null;
    return (
        <Muted style={{ display: 'block' }}>
            {[...nodes].reverse().map((n, i) => (
                <React.Fragment key={n.urn}>{i > 0 && ' › '}<Link to={glossaryHref(n.urn)}>{nameOf(n)}</Link></React.Fragment>
            ))}
        </Muted>
    );
}

function GlossaryGroupView({ urn }: { urn: string }) {
    const t = useT();
    const q = useDheQuery<GlossaryGroup | null>(`gql:glossary-node:${urn}`, (c, signal) => glossaryNode(c, urn, signal));
    const rights = useRights('glossary', urn);
    const record = useDheQuery<CatalogNode>(rights?.may_manage ? `/catalog/glossary/nodes/${urn}` : null, (c, signal) =>
        catalogNode(c, urn, { signal }));
    const edit = useEditDialog('/glossary');
    if (q.loading && !q.data) return <Page><Skeleton h={28} w="40%" /></Page>;
    if (q.error) return <Page><ErrorNotice error={q.error} area={t('glossary.title')} /></Page>;
    if (!q.data) return <Page><Alert type="warning" message={t('glossary.notFound')} /></Page>;
    const children = (q.data.children?.relationships ?? []).map((r) => r.entity).filter((e): e is NamedEntity => !!e);
    const groups = children.filter((e) => e.urn.startsWith('urn:li:glossaryNode:'));
    const terms = children.filter((e) => e.urn.startsWith('urn:li:glossaryTerm:'));
    return (
        <Page>
            <PageHeader title={nameOf(q.data)} description={q.data.properties?.description || t('glossary.group')}
                help={pageHelp('/glossary')}
                actions={rights?.may_manage && record.data && (
                    <Space wrap>
                        <Button onClick={() => edit.show(TERMS, specAction(TERMS, 'POST', '/catalog/glossary/terms'))}>{t('edit.newTerm')}</Button>
                        <Button onClick={() => edit.show(NODES, specAction(NODES, 'PUT'), asRow(record.data))}>{t('edit.edit')}</Button>
                        <Button danger onClick={() => edit.show(NODES, specAction(NODES, 'DELETE'), asRow(record.data))}>{t('edit.delete')}</Button>
                    </Space>
                )} />
            <Parents nodes={q.data.parentNodes?.nodes} />
            <InCore urn={urn} />
            <ReadOnlyReason rights={rights} what="glossary" />
            {groups.length > 0 && (
                <>
                    <h2 style={{ fontSize: 16, marginTop: 16 }}>{t('glossary.groups')}</h2>
                    <CardGrid>{groups.map((g) => <Tile key={g.urn} to={glossaryHref(g.urn)}><strong>{nameOf(g)}</strong></Tile>)}</CardGrid>
                </>
            )}
            <h2 style={{ fontSize: 16, marginTop: 16 }}>{t('glossary.terms')}</h2>
            {terms.length ? <TermTable terms={terms} /> : <Muted>{t('glossary.noTerms')}</Muted>}
            {edit.dialog}
        </Page>
    );
}

function TermList({ label, rel }: { label: string; rel: GlossaryTermDetail['isA'] }) {
    const items = (rel?.relationships ?? []).map((r) => r.entity).filter((e): e is NamedEntity => !!e);
    if (!items.length) return null;
    return (
        <p style={{ margin: '4px 0' }}>
            <strong>{label}: </strong>
            {items.map((e, i) => <React.Fragment key={e.urn}>{i > 0 && ', '}<Link to={glossaryHref(e.urn)}>{nameOf(e)}</Link></React.Fragment>)}
        </p>
    );
}

function GlossaryTermView({ urn }: { urn: string }) {
    const t = useT();
    const q = useDheQuery<GlossaryTermDetail | null>(`gql:glossary-term:${urn}`, (c, signal) => glossaryTerm(c, urn, signal));
    const rights = useRights('glossary', urn);
    const record = useDheQuery<CatalogTerm>(`/catalog/glossary/terms/${urn}`, (c, signal) => catalogTerm(c, urn, { signal }));
    const edit = useEditDialog('/glossary');
    if (q.loading && !q.data) return <Page><Skeleton h={28} w="40%" /></Page>;
    if (q.error) return <Page><ErrorNotice error={q.error} area={t('glossary.title')} /></Page>;
    if (!q.data) return <Page><Alert type="warning" message={t('glossary.notFound')} /></Page>;
    const d = q.data;
    const text = d.properties?.description || d.properties?.definition || '';
    const stewards = owners(d.ownership);
    const hasRelated = [d.isA, d.hasA, d.related].some((r) => (r?.relationships ?? []).length > 0);
    return (
        <Page>
            <PageHeader title={nameOf(d)} description={t('glossary.termOf')} help={pageHelp('/glossary')}
                actions={record.data && (rights?.may_manage ? (
                    <Space wrap>
                        <Button onClick={() => edit.show(TERMS, specAction(TERMS, 'PUT'), asRow(record.data))}>{t('edit.edit')}</Button>
                        <Button danger onClick={() => edit.show(TERMS, specAction(TERMS, 'DELETE'), asRow(record.data))}>{t('edit.delete')}</Button>
                    </Space>
                ) : rights?.may_propose ? (
                    <Button onClick={() => edit.show(TERMS, specAction(TERMS, 'POST', '/catalog/glossary/proposals'),
                        asRow({ ...record.data, intent: '' }))}>{t('edit.propose')}</Button>
                ) : null)} />
            <Parents nodes={d.parentNodes?.nodes} />
            {record.data?.deprecated && <Deprecated note={record.data.deprecation_note} />}
            <InCore urn={urn} />
            <ReadOnlyReason rights={rights} what="glossary" />
            {rights && !rights.may_manage && rights.may_propose && <Muted style={{ display: 'block' }}>{t('edit.proposeHint')}</Muted>}
            <Surface as="section" style={{ padding: 18, marginTop: 12 }} aria-label={t('glossary.definition')}>
                <h2 style={{ fontSize: 16, margin: '0 0 8px' }}>{t('glossary.definition')}</h2>
                {text ? <MarkdownView source={text} /> : <Muted>{t('glossary.noDefinition')}</Muted>}
                <p style={{ margin: '8px 0 0' }}><strong>{t('glossary.stewards')}: </strong>{stewards || <Muted>{t('glossary.noStewards')}</Muted>}</p>
                {d.properties?.termSource && <Muted style={{ display: 'block' }}>{t('glossary.source', { source: d.properties.termSource })}</Muted>}
                {hasRelated && (
                    <div style={{ marginTop: 8 }}>
                        <TermList label={t('glossary.isA')} rel={d.isA} />
                        <TermList label={t('glossary.hasA')} rel={d.hasA} />
                        <TermList label={t('glossary.related')} rel={d.related} />
                    </div>
                )}
            </Surface>
            <ApplyTerm term={urn} deprecated={!!record.data?.deprecated} />
            <TaggedAssets field="glossaryTerms" urn={urn} label={t('glossary.usedBy')} />
            {edit.dialog}
        </Page>
    );
}

/** Domains from Cairn Core, with their owners and how many assets each holds. */
export function DomainsPage() {
    const t = useT();
    const q = useDheQuery<DomainSummary[]>('gql:domains', (c, signal) => loadDomains(c, signal), { staleMs: 60_000 });
    const [filter, setFilter] = useState('');
    const rights = useRights('domain');
    const edit = useEditDialog('/domains');
    const shown = useMemo(() => (q.data ?? []).filter((d) => !filter.trim()
        || nameOf(d).toLowerCase().includes(filter.trim().toLowerCase())), [q.data, filter]);
    return (
        <Page>
            <PageHeader title={t('domains.title')} description={t('domains.description')} help={pageHelp('/domains')}
                actions={rights?.may_manage && (
                    <Button type="primary" onClick={() => edit.show(DOMAINS, specAction(DOMAINS, 'POST'))}>{t('edit.newDomain')}</Button>
                )} />
            <InCore />
            <ReadOnlyReason rights={rights} what="domain" />
            <Input.Search aria-label={t('domains.filter')} placeholder={t('domains.filter')} allowClear value={filter}
                onChange={(e) => setFilter(e.target.value)} style={{ maxWidth: 420, margin: '16px 0' }} />
            {q.error && <ErrorNotice error={q.error} area={t('domains.title')} />}
            {q.loading && !q.data && <Skeleton h={120} />}
            {q.data && shown.length === 0 && <Empty description={t('domains.empty')} />}
            <CardGrid>
                {shown.map((d) => (
                    <Tile key={d.urn} to={domainHref(d.urn)} data-urn={d.urn}>
                        <strong>{nameOf(d)}</strong>
                        {d.properties?.description && <span>{d.properties.description}</span>}
                        <span>
                            {t('domains.assets', { n: (d.entities?.total ?? 0).toLocaleString() })}
                            {ownerUrns(d.ownership).length > 0 && ` · ${ownerUrns(d.ownership).map(actorName).join(', ')}`}
                        </span>
                    </Tile>
                ))}
            </CardGrid>
            {edit.dialog}
        </Page>
    );
}

/** A domain: its data products (the Marketplace's cards), its assets (Core's search) and its owners. */
export function DomainPage() {
    const { urn: raw } = useParams<{ urn: string }>();
    const urn = decodeURIComponent(raw);
    const t = useT();
    const q = useDheQuery<DomainDetail | null>(`gql:domain:${urn}`, (c, signal) => loadDomain(c, urn, signal));
    const products = useDheQuery<ProductCard[]>(`/marketplace/products?domain=${urn}`, (c, signal) =>
        c.listProducts({ domain: urn, sort: 'name' }, { signal }));
    const rights = useRights('domain', urn);
    const record = useDheQuery<CatalogDomain>(rights?.may_manage ? `/catalog/domains/${urn}` : null, (c, signal) =>
        catalogDomain(c, urn, { signal }));
    const edit = useEditDialog('/domains');
    if (q.loading && !q.data) return <Page><Skeleton h={28} w="40%" /></Page>;
    if (q.error) return <Page><ErrorNotice error={q.error} area={t('domains.title')} /></Page>;
    if (!q.data) return <Page><Alert type="warning" message={t('domains.notFound')} /></Page>;
    const d = q.data;
    const subdomains = (d.children?.relationships ?? []).map((r) => r.entity).filter((e): e is NamedEntity => !!e);
    const parents = d.parentDomains?.domains ?? [];
    return (
        <Page>
            <PageHeader title={nameOf(d)} description={d.properties?.description || t('domains.domain')} help={pageHelp('/domains')}
                actions={rights?.may_manage && record.data && (
                    <Space wrap>
                        <Button onClick={() => edit.show(DOMAINS, specAction(DOMAINS, 'PUT'), asRow(record.data))}>{t('edit.edit')}</Button>
                        <Button danger onClick={() => edit.show(DOMAINS, specAction(DOMAINS, 'DELETE'), asRow(record.data))}>{t('edit.delete')}</Button>
                    </Space>
                )} />
            {parents.length > 0 && (
                <Muted style={{ display: 'block' }}>
                    {[...parents].reverse().map((p, i) => <React.Fragment key={p.urn}>{i > 0 && ' › '}<Link to={domainHref(p.urn)}>{nameOf(p)}</Link></React.Fragment>)}
                </Muted>
            )}
            <InCore urn={urn} />
            <ReadOnlyReason rights={rights} what="domain" />
            <p style={{ marginTop: 8 }}>
                <strong>{t('domains.owners')}: </strong>
                {ownerUrns(d.ownership).map(actorName).join(', ') || <Muted>{t('glossary.noStewards')}</Muted>}
            </p>
            {subdomains.length > 0 && (
                <>
                    <h2 style={{ fontSize: 16 }}>{t('domains.subdomains')}</h2>
                    <CardGrid>{subdomains.map((s) => <Tile key={s.urn} to={domainHref(s.urn)}><strong>{nameOf(s)}</strong></Tile>)}</CardGrid>
                </>
            )}
            <section aria-label={t('nav.products')} style={{ marginTop: 16 }}>
                <h2 style={{ fontSize: 16, margin: '0 0 8px' }}>{t('nav.products')}</h2>
                {products.error && <ErrorNotice error={products.error} area={t('nav.products')} />}
                {products.loading && !products.data && <Skeleton h={40} />}
                {products.data && products.data.length === 0 && <Muted>{t('domains.noProducts')}</Muted>}
                <ul style={{ listStyle: 'none', padding: 0, margin: 0 }}>
                    {(products.data ?? []).map((p) => (
                        <li key={p.urn} style={{ display: 'flex', gap: 12, alignItems: 'center', padding: '4px 0' }}>
                            <Link to={productHref(p.urn)}>{p.name}</Link>
                            <TrustBadges certification={p.certification} qualityStatus={p.quality_status} compact />
                            <Muted style={{ fontSize: 12 }}>{t('domains.assets', { n: p.assets_count })}</Muted>
                        </li>
                    ))}
                </ul>
            </section>
            <AddToDomain domain={urn} />
            <TaggedAssets field="domains" urn={urn} label={t('domains.assetsTitle')} />
            {edit.dialog}
        </Page>
    );
}

