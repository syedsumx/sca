import { Empty, Select, Tag } from 'antd';
import React from 'react';
import { Link, useHistory } from 'react-router-dom';
import styled from 'styled-components';

import { useDheQuery } from '../api/hooks';
import type { ActivityItem, Persona, ProductCard, RecentItem, TrendingItem } from '../api/types';
import { AssetCard, assetPath } from '../components/Assets';
import { useBookmarks } from '../components/Engagement';
import { productPath } from '../components/Marketplace';
import { OmniSearchBar } from '../components/OmniSearchBar';
import { CardGrid, Muted, Page, SectionTitle, Skeleton, Surface, tokens } from '../components/tokens';
import { TrustBadges } from '../components/TrustBadges';
import { useDheSession } from '../context/CairnProvider';
import { actorName, relativeTime, shortUrn } from '../utils/format';
import { serializeSearchState } from '../utils/urlState';

const Hero = styled.section`
    padding: 28px 0 8px;
    h1 {
        font-size: 26px;
        font-weight: 600;
        margin: 0 0 14px;
    }
`;

const Columns = styled.div`
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 16px;
    margin-top: 8px;
`;

const Panel = styled(Surface)`
    padding: 14px 16px;
    h3 {
        font-size: 14px;
        font-weight: 600;
        margin: 0 0 8px;
    }
    ul {
        list-style: none;
        margin: 0;
        padding: 0;
    }
    li {
        padding: 5px 0;
        display: flex;
        justify-content: space-between;
        gap: 8px;
        font-size: 13px;
    }
`;

export function greeting(hour: number = new Date().getHours()): string {
    if (hour < 12) return 'Good morning';
    if (hour < 18) return 'Good afternoon';
    return 'Good evening';
}

const QUICK_FILTERS: { label: string; filters: Record<string, string[]>; query?: string }[] = [
    { label: 'Datasets', filters: { _entityType: ['DATASET'] } },
    { label: 'Dashboards', filters: { _entityType: ['DASHBOARD'] } },
    { label: 'Business terms', filters: { _entityType: ['GLOSSARY_TERM'] } },
    { label: 'Data products', filters: { _entityType: ['DATA_PRODUCT'] } },
    { label: 'Certified', filters: { tags: ['urn:li:tag:Certified'] } },
];

function SkeletonCards({ n = 4 }: { n?: number }) {
    return (
        <CardGrid aria-busy="true">
            {Array.from({ length: n }).map((_, i) => (
                // eslint-disable-next-line react/no-array-index-key
                <Surface key={i} style={{ padding: 16, height: 150 }}>
                    <Skeleton h={16} w="60%" />
                    <div style={{ height: 10 }} />
                    <Skeleton h={12} w="40%" />
                    <div style={{ height: 14 }} />
                    <Skeleton h={12} />
                </Surface>
            ))}
        </CardGrid>
    );
}

export function DiscoverPage() {
    const session = useDheSession();
    const history = useHistory();
    const personas = useDheQuery<Persona[]>('/governance/personas', (c, signal) => c.listPersonas({ signal }));
    const persona = personas.data?.find((p) => p.id === session.activePersona);
    const domain = persona?.scope.domains[0];
    const trending = useDheQuery<TrendingItem[]>(`/collab/trending?domain=${domain ?? ''}`, (c, signal) =>
        c.trending({ domain, limit: 8 }, { signal }),
    );
    const bookmarks = useBookmarks();
    const recent = useDheQuery<RecentItem[]>('/collab/recent', (c, signal) => c.recent(6, { signal }));
    const products = useDheQuery<ProductCard[]>('/marketplace/products?sort=consumers', (c, signal) =>
        c.listProducts({ sort: 'consumers' }, { signal }),
    );
    const activity = useDheQuery<ActivityItem[]>('/collab/activity', (c, signal) => c.activity(8, { signal }));
    const name = session.config?.user.display_name?.split(' ')[0];

    return (
        <Page>
            <Hero>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
                    <h1>
                        {greeting()}
                        {name ? `, ${name}` : ''}
                    </h1>
                    {(personas.data?.length ?? 0) > 0 && (
                        <Select
                            aria-label="Browse as persona"
                            style={{ minWidth: 220 }}
                            value={session.activePersona ?? '__all'}
                            onChange={(v: string) => session.setActivePersona(v === '__all' ? null : v)}
                            options={[
                                { value: '__all', label: 'Persona: Everything' },
                                ...(personas.data ?? []).map((p) => ({ value: p.id, label: `Persona: ${p.name}` })),
                            ]}
                        />
                    )}
                </div>
                <OmniSearchBar autoFocus />
                <div style={{ marginTop: 12 }} aria-label="Quick filters">
                    {QUICK_FILTERS.map((q) => (
                        <Tag.CheckableTag
                            key={q.label}
                            checked={false}
                            onChange={() =>
                                history.push(`/search${serializeSearchState({ query: q.query ?? '', filters: q.filters, page: 1 })}`)
                            }
                            style={{ border: `1px solid ${tokens.color.border}`, borderRadius: 999, padding: '2px 12px' }}
                        >
                            {q.label}
                        </Tag.CheckableTag>
                    ))}
                </div>
            </Hero>

            <SectionTitle>
                <span>Trending{persona ? ` for ${persona.name}` : ''}</span>
                <Link to="/search">See all →</Link>
            </SectionTitle>
            {trending.loading && <SkeletonCards />}
            {trending.error && <Muted role="alert">Trending is unavailable right now.</Muted>}
            {trending.data && trending.data.length === 0 && (
                <Empty description="Nothing trending yet — views and bookmarks will show up here." />
            )}
            {trending.data && trending.data.length > 0 && (
                <CardGrid>
                    {trending.data.map((t) => (
                        <AssetCard
                            key={t.urn}
                            asset={t}
                            trend={t.trend}
                            views={t.views_7d}
                            certification={t.certification}
                            qualityStatus={t.quality_status}
                        />
                    ))}
                </CardGrid>
            )}

            <Columns>
                <Panel aria-label="Your bookmarks">
                    <h3>Your bookmarks {bookmarks.data ? `(${bookmarks.data.length})` : ''}</h3>
                    {bookmarks.loading && <Skeleton h={60} />}
                    {bookmarks.data?.length === 0 && <Muted>Press “b” on any search result to bookmark it.</Muted>}
                    <ul>
                        {(bookmarks.data ?? []).slice(0, 8).map((b) => (
                            <li key={b.urn}>
                                <Link to={assetPath(b.urn)}>{shortUrn(b.urn)}</Link>
                                <Muted>{relativeTime(b.created_at)}</Muted>
                            </li>
                        ))}
                    </ul>
                </Panel>
                <Panel aria-label="Recently viewed">
                    <h3>Recently viewed</h3>
                    {recent.loading && <Skeleton h={60} />}
                    {recent.data?.length === 0 && <Muted>Assets you open will appear here.</Muted>}
                    <ul>
                        {(recent.data ?? []).map((r) => (
                            <li key={r.urn}>
                                <Link to={assetPath(r.urn)}>{shortUrn(r.urn)}</Link>
                                <Muted>{relativeTime(r.last_viewed_at)}</Muted>
                            </li>
                        ))}
                    </ul>
                </Panel>
                <Panel aria-label="Featured data products">
                    <h3>Featured data products</h3>
                    {products.loading && <Skeleton h={60} />}
                    {products.data?.length === 0 && <Muted>No data products published yet.</Muted>}
                    <ul>
                        {(products.data ?? []).slice(0, 4).map((p) => (
                            <li key={p.urn}>
                                <Link to={productPath(p.urn)}>{p.name}</Link>
                                <TrustBadges certification={p.certification} qualityStatus={p.quality_status} compact />
                            </li>
                        ))}
                    </ul>
                </Panel>
            </Columns>

            <SectionTitle>Activity you follow</SectionTitle>
            <Panel aria-label="Activity feed">
                {activity.loading && <Skeleton h={40} />}
                {activity.data?.length === 0 && <Muted>Follow assets to see comments, incidents and changes here.</Muted>}
                <ul>
                    {(activity.data ?? []).map((a) => (
                        <li key={a.id}>
                            <span>
                                <strong>{actorName(a.actor)}</strong> {a.summary} ·{' '}
                                <Link to={assetPath(a.entity_urn)}>{a.entity_name}</Link>
                            </span>
                            <Muted>{relativeTime(a.created_at)}</Muted>
                        </li>
                    ))}
                </ul>
            </Panel>
        </Page>
    );
}
