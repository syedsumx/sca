import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { ConsumerApiProvider } from '../api/hooks';
import { BOOKMARKS_KEY, BookmarkToggle } from '../components/Engagement';
import { RequestAccessDrawer } from '../components/Marketplace';
import { ConsumerRoutes } from '../ConsumerApp';
import { CairnProvider } from '../context/CairnProvider';
import { mount } from '../mount';
import { DS, MockCall, PRODUCT, UI_CONFIG, makeClient, mockFetch } from './mockApi';
import { installMatchMedia } from './setup';
import { ConsumerClient } from '../api/client';
import { QueryCache } from '../api/cache';
import type { ProductCard } from '../api/types';

beforeAll(installMatchMedia);

describe('BookmarkToggle', () => {
    it('updates optimistically and rolls back when the API fails', async () => {
        let release: (v: Response) => void = () => undefined;
        const calls: MockCall[] = [];
        const { client, cache } = makeClient(
            {
                'GET /collab/bookmarks': [],
                [`PUT /collab/bookmarks/${DS}`]: () => new Promise<Response>((r) => (release = r)),
            },
            calls,
        );
        render(
            <ConsumerApiProvider client={client} cache={cache}>
                <BookmarkToggle urn={DS} />
            </ConsumerApiProvider>,
        );
        await waitFor(() => expect(cache.get(BOOKMARKS_KEY)?.data).toEqual([]));
        const btn = screen.getByRole('button', { name: 'Bookmark' });
        fireEvent.click(btn);
        // optimistic: flips before the server answers
        expect((await screen.findByRole('button', { name: 'Remove bookmark' })).getAttribute('aria-pressed')).toBe('true');
        await act(async () => {
            release(new Response(JSON.stringify({ detail: 'db down' }), { status: 500 }));
        });
        // rolled back
        expect((await screen.findByRole('button', { name: 'Bookmark' })).getAttribute('aria-pressed')).toBe('false');
        expect(cache.get(BOOKMARKS_KEY)?.data).toEqual([]);
        expect(calls.some((c) => c.method === 'PUT')).toBe(true);
    });
});

describe('RequestAccessDrawer', () => {
    it('walks purpose → duration → justification and submits', async () => {
        const calls: MockCall[] = [];
        const { client, cache } = makeClient(
            {
                'POST /marketplace/access-requests': (body: unknown) => ({
                    id: 'r1',
                    product_urn: PRODUCT.urn,
                    requester: 'urn:li:corpuser:ana',
                    state: 'APPROVED',
                    approvers: [],
                    expires_at: '2026-11-24T00:00:00Z',
                    created_at: '2026-10-25T00:00:00Z',
                    ...(body as object),
                }),
            },
            calls,
        );
        cache.set('/marketplace/products', [PRODUCT]);
        const onSubmitted = vi.fn();
        render(
            <MemoryRouter>
                <ConsumerApiProvider client={client} cache={cache}>
                    <RequestAccessDrawer product={PRODUCT as unknown as ProductCard} open onClose={() => undefined} onSubmitted={onSubmitted} />
                </ConsumerApiProvider>
            </MemoryRouter>,
        );
        const next = () => screen.getByRole('button', { name: 'Next' });
        expect((next() as HTMLButtonElement).disabled).toBe(true);
        fireEvent.click(screen.getByLabelText('Financial reporting'));
        expect(screen.getByText(/Will be auto-approved/)).toBeTruthy();
        fireEvent.click(next());
        fireEvent.click(screen.getByLabelText('90 days'));
        fireEvent.click(next());
        const submit = screen.getByRole('button', { name: 'Submit request' });
        fireEvent.change(screen.getByLabelText('Justification'), { target: { value: 'short' } });
        expect((submit as HTMLButtonElement).disabled).toBe(true);
        fireEvent.change(screen.getByLabelText('Justification'), { target: { value: 'Quarterly revenue close' } });
        expect((submit as HTMLButtonElement).disabled).toBe(false);
        fireEvent.click(submit);
        expect(await screen.findByText('Access granted')).toBeTruthy();
        expect(calls[0].body).toEqual({
            product_urn: PRODUCT.urn,
            purpose: 'Financial reporting',
            justification: 'Quarterly revenue close',
            duration_days: 90,
        });
        expect(onSubmitted).toHaveBeenCalledTimes(1);
        expect(cache.isStale('/marketplace/products', 1e9)).toBe(true);
    });
});

function routes(): Record<string, unknown> {
    return {
        'GET /ui/config': UI_CONFIG,
        'GET /collab/trending': [{ urn: DS, name: 'orders', type: 'DATASET', score: 3, views: 10, unique_viewers: 4 }],
        'GET /collab/recent': [],
        'GET /collab/bookmarks': [],
        'GET /collab/follows': [],
        'GET /collab/activity': [],
        'GET /notify/inbox': [],
        'POST /collab/enrich': {},
        'GET /marketplace/products': [PRODUCT],
        'GET /marketplace/access-requests': [],
        'GET /governance/personas': [],
        'GET /governance/purposes': [],
        'GET /context/findings': [],
        'GET /notify/settings': {
            channels: { slack: { enabled: true, user_id: 'U1' }, teams: { enabled: false, webhook_url: null }, email: { enabled: false }, in_app: { enabled: true } },
            events: {},
        },
        'GQL cairnConsumerSearch': {
            data: {
                searchAcrossEntities: {
                    total: 1,
                    searchResults: [{ entity: { urn: DS, type: 'DATASET', name: 'orders', platform: { name: 'snowflake' } } }],
                    facets: [],
                },
            },
        },
    };
}

describe('ConsumerRoutes smoke', () => {
    it.each([
        ['/discover', /trending/i],
        ['/search?q=orders', /orders/],
        ['/marketplace', /Customer 360/],
        ['/requests', /request/i],
        ['/governance', /persona/i],
        ['/settings', /notification/i],
    ])('renders %s', async (path, text) => {
        const client = new ConsumerClient({ fetchImpl: mockFetch(routes()) });
        render(
            <MemoryRouter initialEntries={[path]}>
                <CairnProvider client={client} cache={new QueryCache()}>
                    <ConsumerRoutes />
                </CairnProvider>
            </MemoryRouter>,
        );
        expect((await screen.findAllByText(text, undefined, { timeout: 3000 })).length).toBeGreaterThan(0);
    });
});

describe('mount()', () => {
    it('renders into the host element and cleans up', async () => {
        const fetchImpl = mockFetch(routes());
        const el = document.createElement('div');
        document.body.append(el);
        const cleanup = mount(el, { client: new ConsumerClient({ fetchImpl }), cache: new QueryCache() });
        await waitFor(() => expect(el.textContent).toMatch(/Discover/));
        expect(window.location.hash === '' || window.location.hash.startsWith('#/')).toBe(true);
        cleanup();
        expect(el.innerHTML).toBe('');
        el.remove();
    });
});

describe('mount() inside the catalog shell', () => {
    afterEach(() => window.history.replaceState(null, '', '/'));

    it('opens the section of its shell entry, and section tabs move the shell', async () => {
        window.history.replaceState(null, '', '/mfe/cairn-requests');
        const fetchImpl = mockFetch(routes());
        const el = document.createElement('div');
        document.body.append(el);
        const cleanup = mount(el, { client: new ConsumerClient({ fetchImpl }), cache: new QueryCache() });
        expect(window.location.hash).toBe('#/requests');
        const popped: string[] = [];
        const onPop = () => popped.push(`${window.location.pathname}${window.location.hash}`);
        window.addEventListener('popstate', onPop);
        const tab = await screen.findByRole('link', { name: 'Marketplace' });
        fireEvent.click(tab);
        expect(popped).toEqual(['/mfe/cairn-marketplace#/marketplace']);
        // Search has no shell entry of its own: it stays inside the workspace.
        fireEvent.click(screen.getByRole('link', { name: 'Search' }));
        expect(popped).toHaveLength(1);
        window.removeEventListener('popstate', onPop);
        cleanup();
        el.remove();
    });
});
