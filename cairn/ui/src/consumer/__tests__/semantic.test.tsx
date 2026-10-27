import { render, screen, waitFor, within } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { ConsumerClient } from '../api/client';
import { ConsumerRoutes } from '../ConsumerApp';
import { CairnProvider } from '../context/CairnProvider';
import { DS, MockCall, UI_CONFIG, mockFetch } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const REVENUE = 'urn:li:dataset:(urn:li:dataPlatform:snowflake,finance.revenue_daily,PROD)';

function routes(semantic: boolean, keywordHits: string[]) {
    return {
        'GET /ui/config': { ...UI_CONFIG, features: { ...UI_CONFIG.features, semantic_search: semantic } },
        'POST /collab/enrich': {},
        'GET /collab/bookmarks': [],
        'POST /ai/search/semantic': (body: { exclude: string[] }) => ({
            results: [
                { urn: DS, name: 'orders', type: 'dataset', score: 0.6, matched: ['orders'] },
                { urn: REVENUE, name: 'revenue_daily', type: 'dataset', platform: 'snowflake', score: 0.41,
                  description: 'Money recognised per region each day.', matched: ['region', 'day'] },
            ].filter((r) => !body.exclude.includes(r.urn)),
        }),
        'GQL cairnConsumerSearch': {
            data: {
                searchAcrossEntities: {
                    total: keywordHits.length,
                    searchResults: keywordHits.map((urn) => ({
                        entity: { urn, type: 'DATASET', name: 'orders', platform: { name: 'snowflake' } },
                    })),
                    facets: [],
                },
            },
        },
        'GET /ai/similar': { urn: DS, results: [{ urn: REVENUE, name: 'revenue_daily', type: 'dataset', score: 0.3 }] },
    };
}

function renderAt(path: string, r: Record<string, unknown>, calls: MockCall[] = []) {
    const client = new ConsumerClient({ fetchImpl: mockFetch(r, calls) });
    return render(
        <MemoryRouter initialEntries={[path]}>
            <CairnProvider client={client} cache={new QueryCache()}>
                <ConsumerRoutes />
            </CairnProvider>
        </MemoryRouter>,
    );
}

describe('Related by meaning', () => {
    it('adds meaning-based matches the keyword search missed, with the words they matched', async () => {
        const calls: MockCall[] = [];
        renderAt('/search?q=money%20per%20region%20each%20day', routes(true, [DS]), calls);
        const section = await screen.findByRole('region', { name: 'Related by meaning' });
        expect(within(section).getByText('revenue_daily')).toBeTruthy();
        expect(within(section).getByText('Matched: region, day')).toBeTruthy();
        expect(within(section).queryByText('orders')).toBeNull();          // already a keyword hit
        const req = calls.find((c) => c.path === '/ai/search/semantic');
        expect((req?.body as { exclude: string[] }).exclude).toEqual([DS]);
    });

    it('stands in when keyword search finds nothing', async () => {
        renderAt('/search?q=money%20per%20region', routes(true, []), []);
        expect(await screen.findByText(/No exact matches/)).toBeTruthy();
        const section = await screen.findByRole('region', { name: 'Related by meaning' });
        expect(within(section).getByText('orders')).toBeTruthy();
    });

    it('stays out of the way when semantic search is off', async () => {
        const calls: MockCall[] = [];
        renderAt('/search?q=orders', routes(false, [DS]), calls);
        await screen.findAllByText('orders');
        await waitFor(() => expect(calls.some((c) => c.path === '/ui/config')).toBe(true));
        expect(calls.some((c) => c.path === '/ai/search/semantic')).toBe(false);
        expect(screen.queryByRole('region', { name: 'Related by meaning' })).toBeNull();
    });
});

describe('SimilarAssets', () => {
    it('lists similar assets on an asset page', async () => {
        const { SimilarAssets } = await import('../components/Semantic');
        const client = new ConsumerClient({ fetchImpl: mockFetch(routes(true, [])) });
        render(
            <MemoryRouter>
                <CairnProvider client={client} cache={new QueryCache()}>
                    <SimilarAssets urn={DS} />
                </CairnProvider>
            </MemoryRouter>,
        );
        const panel = await screen.findByLabelText('Similar assets');
        expect(within(panel).getByText('revenue_daily')).toBeTruthy();
    });
});
