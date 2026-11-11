import { Empty, Input, Pagination, Select, Table, Tag } from 'antd';
import React, { useMemo, useState } from 'react';
import { Link, useHistory, useLocation } from 'react-router-dom';
import styled from 'styled-components';

import { ASSET_TYPES, ColumnLineage, catalogCounts, catalogPage, columnLineage } from '../api/catalog';
import { useDheQuery } from '../api/hooks';
import type { EnrichmentMap, SearchEntity, SearchResponse } from '../api/types';
import { AssetRow, assetPath } from '../components/Assets';
import { BulkEdit } from '../components/BulkEdit';
import { ErrorNotice } from '../components/ErrorNotice';
import { FacetPanel } from '../components/FacetPanel';
import { LineageGraphView } from '../components/LineageGraph';
import { RelatedByMeaning } from '../components/Semantic';
import { Muted, Page, Skeleton, Surface, focusRing, tokens } from '../components/tokens';
import { PageHeader } from '../guide/PageHeader';
import { pageHelp } from '../guide/help';
import { TermSearchResults } from '../ontology/Embeds';
import { useT } from '../i18n';
import { entityTypeLabel, platformName, shortUrn } from '../utils/format';
import { parseSearchState, serializeSearchState, toggleFilter } from '../utils/urlState';

const PAGE_SIZE = 20;

const Body = styled.div`
    display: grid;
    grid-template-columns: 240px minmax(0, 1fr);
    gap: 24px;
    margin-top: 16px;
    @media (max-width: 800px) {
        grid-template-columns: minmax(0, 1fr);
    }
`;

const Types = styled.div`
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-top: 16px;
`;

const TypeButton = styled.button<{ $on: boolean }>`
    display: inline-flex;
    align-items: baseline;
    gap: 8px;
    min-height: ${tokens.control}px;
    padding: 0 14px;
    border-radius: 999px;
    border: 1px solid ${(p) => (p.$on ? tokens.color.primary : tokens.color.border)};
    background: ${(p) => (p.$on ? tokens.color.primarySoft : tokens.color.surface)};
    color: ${tokens.color.text};
    font: inherit;
    font-size: 14px;
    font-weight: ${(p) => (p.$on ? 600 : 500)};
    cursor: pointer;
    ${focusRing}
    &:hover {
        border-color: ${tokens.color.primary};
    }
    .n {
        font-size: 12px;
        color: ${tokens.color.textMuted};
        font-variant-numeric: tabular-nums;
    }
`;

/** Counts per value of one facet field in a search answer. */
export function facetCounts(r: SearchResponse | undefined, field: string): Map<string, number> {
    return new Map((r?.facets.find((f) => f.field === field)?.aggregations ?? []).map((a) => [a.value, a.count]));
}

/**
 * The catalog (Cairn Core): every asset, browsed by entity type (with counts), platform, domain and the other facets
 * Core's search gives, with a search box; each result opens its asset page. The state is in the URL.
 */
export function CatalogPage() {
    const t = useT();
    const location = useLocation();
    const history = useHistory();
    const state = useMemo(() => parseSearchState(location.search), [location.search]);
    const go = (next: typeof state) => history.push(`/catalog${serializeSearchState(next)}`);
    const keyed = serializeSearchState(state);
    const withoutTypes = { ...state.filters };
    delete withoutTypes._entityType;
    const counts = useDheQuery<SearchResponse>(`gql:catalog-counts${serializeSearchState({ ...state, filters: withoutTypes, page: 1 })}`,
        (c, signal) => catalogCounts(c, state.query, withoutTypes, signal), { staleMs: 30_000 });
    const page = useDheQuery<SearchResponse>(`gql:catalog${keyed}`,
        (c, signal) => catalogPage(c, state.query, state.filters, (state.page - 1) * PAGE_SIZE, PAGE_SIZE, signal),
        { staleMs: 15_000 });
    const urns = (page.data?.results ?? []).map((r) => r.urn);
    const enrichment = useDheQuery<EnrichmentMap>(urns.length ? `/collab/enrich?${urns.join('|')}` : null, (c, signal) =>
        c.enrich(urns, { signal }));
    const byType = facetCounts(counts.data, '_entityType');
    const chosen = state.filters._entityType ?? [];
    const pick = (type: string | null) =>
        go({ ...state, filters: type ? { ...withoutTypes, _entityType: [type] } : withoutTypes, page: 1 });
    const facets = (page.data?.facets ?? []).filter((f) => f.field !== '_entityType');

    return (
        <Page>
            <PageHeader title={t('catalog.title')} description={t('catalog.description')} help={pageHelp('/catalog')} />
            <Input.Search
                key={state.query}
                defaultValue={state.query}
                allowClear
                enterButton={t('catalog.searchButton')}
                aria-label={t('catalog.search')}
                placeholder={t('catalog.searchPlaceholder')}
                onSearch={(q) => go({ ...state, query: q.trim(), page: 1 })}
                style={{ maxWidth: 640 }}
            />
            <Types role="group" aria-label={t('catalog.types')}>
                <TypeButton type="button" $on={chosen.length === 0} aria-pressed={chosen.length === 0} onClick={() => pick(null)}>
                    {t('catalog.allAssets')}
                    <span className="n">{counts.data ? counts.data.total.toLocaleString() : '…'}</span>
                </TypeButton>
                {ASSET_TYPES.map((type) => {
                    const n = byType.get(type) ?? 0;
                    const on = chosen.includes(type);
                    if (!on && counts.data && n === 0) return null;
                    return (
                        <TypeButton key={type} type="button" $on={on} aria-pressed={on} onClick={() => pick(on ? null : type)}
                            data-type={type}>
                            {entityTypeLabel(type)}
                            <span className="n">{counts.data ? n.toLocaleString() : '…'}</span>
                        </TypeButton>
                    );
                })}
            </Types>
            {counts.error && <ErrorNotice error={counts.error} area={t('catalog.title')} />}
            <Body>
                <aside aria-label={t('catalog.filters')}>
                    {page.data ? (
                        <FacetPanel facets={facets} selected={state.filters} onToggle={(f, v) => go(toggleFilter(state, f, v))} />
                    ) : (
                        <Skeleton h={200} />
                    )}
                </aside>
                <section aria-label={t('catalog.results')} aria-busy={page.loading}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8, marginBottom: 8, flexWrap: 'wrap' }}>
                        <Muted aria-live="polite">
                            {page.data ? t('catalog.count', { n: page.data.total.toLocaleString() }) : ''}
                        </Muted>
                        {page.data && page.data.total > 0 && state.query && <BulkEdit query={state.query} />}
                    </div>
                    {state.query && state.page === 1 && <TermSearchResults query={state.query} />}
                    {page.loading && !page.data && <Skeleton h={120} />}
                    {page.error && <ErrorNotice error={page.error} area={t('catalog.title')} />}
                    {page.data && page.data.results.length === 0 && <Empty description={t('catalog.none')} />}
                    {page.data?.results.map((r) => <AssetRow key={r.urn} asset={r} enrichment={enrichment.data?.[r.urn]} />)}
                    {page.data && page.data.total > PAGE_SIZE && (
                        <Pagination style={{ marginTop: 16 }} current={state.page} pageSize={PAGE_SIZE}
                            total={Math.min(page.data.total, 10_000)} showSizeChanger={false}
                            onChange={(p) => go({ ...state, page: p })} />
                    )}
                    {/* By meaning as well as by words (what /search added; L149 merged it here). */}
                    {page.data && state.query && state.page === 1 && !Object.keys(state.filters).length && (
                        <RelatedByMeaning query={state.query} exclude={urns} enrichment={enrichment.data} />
                    )}
                </section>
            </Body>
        </Page>
    );
}

/** One column's upstream columns, from Core's fine-grained lineage (column level). */
export interface ColumnEdge {
    column: string;
    from: { dataset: string; column: string }[];
    operation: string | null;
}

export function columnEdges(urn: string, lineages: ColumnLineage[]): ColumnEdge[] {
    const out = new Map<string, ColumnEdge>();
    for (const l of lineages) {
        for (const d of l.downstreams ?? []) {
            if (d.urn && !d.urn.includes(urn)) continue;            // another dataset's column
            const col = d.path;
            const e = out.get(col) ?? { column: col, from: [], operation: l.transformOperation ?? null };
            for (const u of l.upstreams ?? []) {
                const m = /^urn:li:schemaField:\((.+),([^,]+)\)$/.exec(u.urn);
                e.from.push({ dataset: m ? m[1] : u.urn, column: u.path || (m ? m[2] : '') });
            }
            out.set(col, e);
        }
    }
    return [...out.values()].sort((a, b) => a.column.localeCompare(b.column));
}

function ColumnLevel({ urn }: { urn: string }) {
    const t = useT();
    const q = useDheQuery<ColumnLineage[]>(`gql:column-lineage:${urn}`, (c, signal) => columnLineage(c, urn, signal));
    if (q.loading) return <Skeleton h={60} />;
    if (q.error) return <Muted>{t('lineage.columnsUnavailable')}</Muted>;
    const edges = columnEdges(urn, q.data ?? []);
    if (!edges.length) return <Muted>{t('lineage.noColumns')}</Muted>;
    return (
        <Table<ColumnEdge>
            size="small"
            rowKey="column"
            pagination={false}
            dataSource={edges}
            columns={[
                { title: t('lineage.column'), dataIndex: 'column', render: (v: string) => <code>{v}</code> },
                {
                    title: t('lineage.from'),
                    render: (_, e) => e.from.map((f) => (
                        <Tag key={`${f.dataset}.${f.column}`}>
                            <Link to={assetPath(f.dataset)}>{shortUrn(f.dataset)}</Link>.{f.column}
                        </Tag>
                    )),
                },
                { title: t('lineage.operation'), dataIndex: 'operation', render: (v: string | null) => v ?? '' },
            ]}
        />
    );
}

/** Lineage as a section: pick any asset (Core's search), then its upstream and downstream graph and column lineage. */
export function LineagePage() {
    const t = useT();
    const location = useLocation();
    const history = useHistory();
    const urn = new URLSearchParams(location.search).get('urn') ?? '';
    const [typed, setTyped] = useState('');
    const found = useDheQuery<SearchResponse>(typed.trim().length >= 2 ? `gql:lineage-pick:${typed.trim()}` : null,
        (c, signal) => catalogPage(c, typed.trim(), {}, 0, 10, signal), { staleMs: 15_000 });
    const choose = (u: string) => history.push(`/lineage?urn=${encodeURIComponent(u)}`);
    const options = (found.data?.results ?? []).map((r: SearchEntity) => ({
        value: r.urn,
        label: `${r.name} · ${entityTypeLabel(r.type)}${r.platform ? ` · ${platformName(r.platform)}` : ''}`,
    }));
    return (
        <Page>
            <PageHeader title={t('lineage.title')} description={t('lineage.description')} help={pageHelp('/lineage')} />
            <Select
                showSearch
                filterOption={false}
                style={{ width: '100%', maxWidth: 640 }}
                aria-label={t('lineage.pick')}
                placeholder={t('lineage.pick')}
                onSearch={setTyped}
                onChange={(v: string) => choose(v)}
                value={urn || undefined}
                notFoundContent={found.loading ? t('lineage.searching') : typed.trim().length >= 2 ? t('catalog.none') : null}
                options={urn && !options.some((o) => o.value === urn) ? [{ value: urn, label: shortUrn(urn) }, ...options] : options}
            />
            {!urn && <Muted style={{ display: 'block', marginTop: 16 }}>{t('lineage.empty')}</Muted>}
            {urn && (
                <>
                    <Surface style={{ padding: 18, marginTop: 16 }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap', marginBottom: 8 }}>
                            <strong>{shortUrn(urn)}</strong>
                            <Link to={assetPath(urn)}>{t('lineage.openAsset')}</Link>
                        </div>
                        <LineageGraphView urn={urn} />
                    </Surface>
                    {urn.startsWith('urn:li:dataset:') && (
                        <Surface as="section" style={{ padding: 18, marginTop: 16 }} aria-label={t('lineage.columns')}>
                            <h2 style={{ margin: '0 0 8px', fontSize: 16 }}>{t('lineage.columns')}</h2>
                            <ColumnLevel urn={urn} />
                        </Surface>
                    )}
                </>
            )}
        </Page>
    );
}
