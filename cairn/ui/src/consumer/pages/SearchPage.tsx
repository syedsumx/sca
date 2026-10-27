import { Empty, Pagination, Tag } from 'antd';
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useHistory, useLocation } from 'react-router-dom';
import styled from 'styled-components';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { EnrichmentMap, SearchResponse } from '../api/types';
import { AssetRow, assetPath } from '../components/Assets';
import { useBookmarkToggle, useBookmarks } from '../components/Engagement';
import { FACET_LABELS, FacetPanel } from '../components/FacetPanel';
import { OmniSearchBar } from '../components/OmniSearchBar';
import { RelatedByMeaning } from '../components/Semantic';
import { Muted, Page, Skeleton } from '../components/tokens';
import { SearchState, parseSearchState, serializeSearchState, toggleFilter } from '../utils/urlState';
import { useHotkeys } from '../utils/useHotkeys';

const PAGE_SIZE = 20;

const Layout = styled.div`
    display: grid;
    grid-template-columns: 240px 1fr;
    gap: 24px;
    margin-top: 16px;
    @media (max-width: 800px) {
        grid-template-columns: 1fr;
    }
`;

export function SearchPage() {
    const location = useLocation();
    const history = useHistory();
    const { client } = useConsumerApi();
    const state = useMemo(() => parseSearchState(location.search), [location.search]);
    const key = `gql:search${serializeSearchState(state)}`;
    const startedAt = useRef(0);
    const [latency, setLatency] = useState<number | null>(null);

    const result = useDheQuery<SearchResponse>(
        key,
        async (c, signal) => {
            startedAt.current = performance.now();
            const r = await c.search(
                { query: state.query, filters: state.filters, start: (state.page - 1) * PAGE_SIZE, count: PAGE_SIZE },
                signal,
            );
            setLatency(Math.round(performance.now() - startedAt.current));
            return r;
        },
        { staleMs: 15_000 },
    );
    const urns = (result.data?.results ?? []).map((r) => r.urn);
    const enrichment = useDheQuery<EnrichmentMap>(urns.length ? `/collab/enrich?${urns.join('|')}` : null, (c, signal) =>
        c.enrich(urns, { signal }),
    );
    useBookmarks();
    const toggleBookmark = useBookmarkToggle();
    const { cache } = useConsumerApi();

    const [selected, setSelected] = useState(0);
    const rowRefs = useRef<(HTMLAnchorElement | null)[]>([]);
    useEffect(() => setSelected(0), [key]);

    const navigate = (next: SearchState) => history.push(`/search${serializeSearchState(next)}`);

    useHotkeys({
        j: () => setSelected((s) => Math.min(s + 1, urns.length - 1)),
        k: () => setSelected((s) => Math.max(s - 1, 0)),
        Enter: () => urns[selected] && history.push(assetPath(urns[selected])),
        b: () => {
            const urn = urns[selected];
            if (!urn) return;
            const bookmarked = !!cache.get<{ urn: string }[]>('/collab/bookmarks')?.data?.some((b) => b.urn === urn);
            void toggleBookmark(urn, bookmarked);
        },
    });

    useEffect(() => {
        rowRefs.current[selected]?.scrollIntoView?.({ block: 'nearest' });
    }, [selected]);

    const chips = Object.entries(state.filters).flatMap(([field, values]) =>
        values.map((v) => {
            const facet = result.data?.facets.find((f) => f.field === field);
            const label = facet?.aggregations.find((a) => a.value === v)?.label ?? v.split(':').pop();
            return { field, value: v, label: `${FACET_LABELS[field] ?? field}: ${label}` };
        }),
    );

    return (
        <Page>
            <OmniSearchBar key={state.query} initialQuery={state.query} />
            <Layout>
                <aside>
                    {result.data ? (
                        <FacetPanel
                            facets={result.data.facets}
                            selected={state.filters}
                            onToggle={(field, value) => navigate(toggleFilter(state, field, value))}
                        />
                    ) : (
                        <Skeleton h={200} />
                    )}
                </aside>
                <section aria-label="Search results" aria-busy={result.loading}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
                        <div>
                            {chips.map((c) => (
                                <Tag key={`${c.field}=${c.value}`} closable onClose={() => navigate(toggleFilter(state, c.field, c.value))}>
                                    {c.label}
                                </Tag>
                            ))}
                        </div>
                        <Muted aria-live="polite">
                            {result.data ? `${result.data.total.toLocaleString()} results` : ''}
                            {latency !== null && result.data ? ` · ${latency} ms` : ''}
                        </Muted>
                    </div>
                    {result.loading &&
                        Array.from({ length: 6 }).map((_, i) => (
                            // eslint-disable-next-line react/no-array-index-key
                            <div key={i} style={{ padding: 12 }}>
                                <Skeleton h={16} w="45%" />
                                <div style={{ height: 6 }} />
                                <Skeleton h={12} w="70%" />
                            </div>
                        ))}
                    {result.error && <div role="alert">Search failed: {result.error.message}</div>}
                    {result.data && result.data.results.length === 0 && (
                        <Empty
                            description={
                                state.query
                                    ? `No exact matches for “${state.query}”. Try fewer filters, see related assets below, or ask the assistant.`
                                    : 'No results'
                            }
                        />
                    )}
                    {result.data?.results.map((r, i) => (
                        <AssetRow
                            key={r.urn}
                            asset={r}
                            enrichment={enrichment.data?.[r.urn]}
                            selected={i === selected}
                            rowRef={(el: HTMLAnchorElement | null) => {
                                rowRefs.current[i] = el;
                            }}
                        />
                    ))}
                    {result.data && result.data.total > PAGE_SIZE && (
                        <Pagination
                            style={{ marginTop: 16 }}
                            current={state.page}
                            pageSize={PAGE_SIZE}
                            total={Math.min(result.data.total, 10_000)}
                            showSizeChanger={false}
                            onChange={(page) => navigate({ ...state, page })}
                        />
                    )}
                    {result.data && state.page === 1 && !Object.keys(state.filters).length && (
                        <RelatedByMeaning query={state.query} exclude={urns} enrichment={enrichment.data} />
                    )}
                    <Muted style={{ display: 'block', marginTop: 12, fontSize: 12 }}>
                        Shortcuts: j/k move · enter open · b bookmark · / search
                    </Muted>
                </section>
            </Layout>
        </Page>
    );
}
