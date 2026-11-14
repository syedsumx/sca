import { ConsumerClient, GraphQLError, filtersToInput, parseSearch } from '../api/client';
import { QueryCache, optimistic } from '../api/cache';
import { activeFilterCount, parseSearchState, serializeSearchState, toggleFilter } from '../utils/urlState';
import { DS, MockCall, makeClient, mockFetch } from './mockApi';

describe('ConsumerClient', () => {
    it('calls consumer endpoints with the contract paths and bodies', async () => {
        const calls: MockCall[] = [];
        const { client } = makeClient(
            {
                [`PUT /collab/bookmarks/${DS}`]: { urn: DS, note: null, created_at: 'x' },
                'POST /collab/enrich': {},
                'POST /marketplace/access-requests': { id: 'r1' },
                'POST /marketplace/access-requests/r1/approve': { id: 'r1' },
                'POST /ai/chat': { conversation_id: 'c1', reply_md: 'hi', citations: [], tool_calls: [], suggested_actions: [] },
                'GET /collab/trending': [],
            },
            calls,
        );
        await client.addBookmark(DS);
        await client.enrich([DS]);
        await client.createAccessRequest({ product_urn: 'urn:li:dataProduct:x', purpose: 'p', justification: 'long enough', duration_days: 30 });
        await client.actOnAccessRequest('r1', 'approve', 'ok');
        await client.chat('hello', 'c0');
        await client.trending({ domain: 'urn:li:domain:finance', limit: 8 });
        expect(calls.map((c) => `${c.method} ${c.path}`)).toEqual([
            `PUT /collab/bookmarks/${DS}`,
            'POST /collab/enrich',
            'POST /marketplace/access-requests',
            'POST /marketplace/access-requests/r1/approve',
            'POST /ai/chat',
            'GET /collab/trending',
        ]);
        // URNs are percent-encoded in the path
        expect(calls[0].url.pathname).toContain(encodeURIComponent(DS));
        expect(calls[1].body).toEqual({ urns: [DS] });
        expect(calls[3].body).toEqual({ comment: 'ok' });
        expect(calls[4].body).toEqual({ message: 'hello', conversation_id: 'c0' });
        expect(calls[5].url.searchParams.get('domain')).toBe('urn:li:domain:finance');
    });

    it('gql() posts to /api/v2/graphql and raises GraphQLError on errors', async () => {
        const calls: MockCall[] = [];
        const ok = new ConsumerClient({
            fetchImpl: mockFetch({ 'GQL q1': { data: { x: 1 } } }, calls),
        });
        // mockFetch returns the route value as the whole body for GQL routes
        await expect(ok.gql('query q1 { x }')).resolves.toEqual({ x: 1 });
        expect(calls[0].url.pathname).toBe('/api/v2/graphql');
        const bad = new ConsumerClient({ fetchImpl: mockFetch({ 'GQL q2': { errors: [{ message: 'boom' }] } }) });
        await expect(bad.gql('query q2 { x }')).rejects.toBeInstanceOf(GraphQLError);
    });

    it('search() maps facets and entity names', async () => {
        const calls: MockCall[] = [];
        const client = new ConsumerClient({
            fetchImpl: mockFetch(
                {
                    'GQL cairnConsumerSearch': {
                        data: {
                            searchAcrossEntities: {
                                total: 1,
                                searchResults: [
                                    { entity: { urn: DS, type: 'DATASET', name: 'orders', platform: { name: 'snowflake' }, properties: { description: 'd' } } },
                                ],
                                facets: [
                                    {
                                        field: 'domains',
                                        displayName: 'Domain',
                                        aggregations: [
                                            { value: 'urn:li:domain:finance', count: 3, entity: { properties: { name: 'Finance' } } },
                                            { value: 'urn:li:domain:empty', count: 0 },
                                        ],
                                    },
                                ],
                            },
                        },
                    },
                },
                calls,
            ),
        });
        const r = await client.search({ query: 'orders', filters: { _entityType: ['DATASET'], domains: ['urn:li:domain:finance'] } });
        expect(r.results[0]).toEqual({ urn: DS, type: 'DATASET', name: 'orders', platform: 'snowflake', description: 'd' });
        expect(r.facets[0].aggregations).toEqual([{ value: 'urn:li:domain:finance', count: 3, label: 'Finance' }]);
        const input = (calls[0].body as { variables: { input: Record<string, unknown> } }).variables.input;
        expect(input.types).toEqual(['DATASET']);
        expect(input.orFilters).toEqual([{ and: [{ field: 'domains', values: ['urn:li:domain:finance'] }] }]);
    });

    it('filtersToInput / parseSearch handle empty input', () => {
        expect(filtersToInput({})).toEqual({ types: [], orFilters: undefined });
        expect(parseSearch(null)).toEqual({ total: 0, results: [], facets: [] });
    });
});

describe('QueryCache', () => {
    it('dedupes concurrent loads and caches', async () => {
        const cache = new QueryCache();
        let n = 0;
        const loader = () => new Promise<number>((r) => setTimeout(() => r(++n), 5));
        const [a, b] = await Promise.all([cache.fetch('k', loader), cache.fetch('k', loader)]);
        expect(a).toBe(1);
        expect(b).toBe(1);
        expect(cache.isStale('k', 10_000)).toBe(false);
    });

    it('invalidate(prefix) marks matching keys stale and notifies', () => {
        const cache = new QueryCache();
        cache.set('/marketplace/products', [1]);
        cache.set('/marketplace/access-requests?scope=mine', [2]);
        cache.set('/collab/bookmarks', [3]);
        const seen: string[] = [];
        cache.subscribe('/marketplace/products', () => seen.push('p'));
        expect(cache.invalidate('/marketplace')).toBe(2);
        expect(seen).toEqual(['p']);
        expect(cache.isStale('/marketplace/products', 1e9)).toBe(true);
        expect(cache.isStale('/collab/bookmarks', 1e9)).toBe(false);
    });

    it('optimistic() applies immediately and rolls back on failure', async () => {
        const cache = new QueryCache();
        cache.set<string[]>('/collab/bookmarks', ['a']);
        const pending = optimistic<string[], void>(cache, '/collab/bookmarks', (p) => [...(p ?? []), 'b'], () =>
            Promise.reject(new Error('nope')),
        );
        expect(cache.get<string[]>('/collab/bookmarks')?.data).toEqual(['a', 'b']);
        await expect(pending).rejects.toThrow('nope');
        expect(cache.get<string[]>('/collab/bookmarks')?.data).toEqual(['a']);
    });
});

describe('search URL state', () => {
    it('round-trips query, multi-valued facets and page deterministically', () => {
        const s = { query: 'revenue ytd', filters: { domains: ['urn:li:domain:finance'], _entityType: ['DATASET', 'DASHBOARD'] }, page: 3 };
        const qs = serializeSearchState(s);
        expect(qs).toBe('?q=revenue+ytd&f._entityType=DASHBOARD&f._entityType=DATASET&f.domains=urn%3Ali%3Adomain%3Afinance&page=3');
        expect(parseSearchState(qs)).toEqual({ ...s, filters: { domains: ['urn:li:domain:finance'], _entityType: ['DASHBOARD', 'DATASET'] } });
    });

    it('toggleFilter adds/removes values and resets page', () => {
        const base = parseSearchState('?q=x&page=4');
        const on = toggleFilter(base, 'platform', 'urn:li:dataPlatform:snowflake');
        expect(on.page).toBe(1);
        expect(activeFilterCount(on)).toBe(1);
        const off = toggleFilter(on, 'platform', 'urn:li:dataPlatform:snowflake');
        expect(off.filters).toEqual({});
        expect(serializeSearchState(off)).toBe('?q=x');
    });
});
