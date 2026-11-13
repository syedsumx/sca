import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import React from 'react';
import { MemoryRouter, Route, Switch } from 'react-router-dom';

import { ASSET_TYPES } from '../api/catalog';
import { issuesFor } from '../components/CatalogHome';
import { AssetLabelsEditor } from '../components/GlossaryEdit';
import { CairnProvider } from '../context/CairnProvider';
import { I18nProvider } from '../i18n';
import { CatalogPage, LineagePage, columnEdges } from '../pages/CatalogPage';
import { DomainPage, DomainsPage, GlossaryEntryPage, GlossaryPage } from '../pages/GlossaryDomainsPages';
import { ContractPage, ContractsPage, DataProductPage, DataProductsPage } from '../pages/ProductsPages';
import { CoreMap } from '../resources/CapabilitiesPage';
import { DS, MockCall, PRODUCT, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const ME = 'urn:li:corpuser:ana';

function wrap(ui: React.ReactElement, routes: Record<string, unknown>, at = '/', calls: MockCall[] = [], path = '*') {
    const { client, cache } = makeClient({ 'GET /ui/config': UI_CONFIG, 'GET /notify/inbox': [], ...routes }, calls);
    let where = '';
    render(
        <MemoryRouter initialEntries={[at]}>
            <I18nProvider initial="en">
                <CairnProvider client={client} cache={cache}>
                    <Switch><Route path={path}>{ui}</Route></Switch>
                    <Route render={({ location }) => {
                        where = location.pathname + location.search;
                        return null;
                    }} />
                </CairnProvider>
            </I18nProvider>
        </MemoryRouter>,
    );
    return { calls, where: () => where };
}

// ---- navigation: the sidebar, the shell entries and every earlier route are tested in ia.test.tsx (L149) ----------

// ---- the catalog -------------------------------------------------------------------------------------------------
const FACETS = [
    { field: '_entityType', displayName: 'Type', aggregations: [
        { value: 'DATASET', count: 12, entity: null }, { value: 'DASHBOARD', count: 3, entity: null },
        { value: 'DATA_FLOW', count: 2, entity: null }, { value: 'MLMODEL', count: 0, entity: null }] },
    { field: 'platform', displayName: 'Platform', aggregations: [
        { value: 'urn:li:dataPlatform:snowflake', count: 10, entity: { urn: 'urn:li:dataPlatform:snowflake', name: 'snowflake' } },
        { value: 'urn:li:dataPlatform:looker', count: 3, entity: { urn: 'urn:li:dataPlatform:looker', name: 'looker' } }] },
    { field: 'domains', displayName: 'Domain', aggregations: [
        { value: 'urn:li:domain:finance', count: 7, entity: { urn: 'urn:li:domain:finance', properties: { name: 'Finance' } } }] },
];

function searchAnswer(body: unknown) {
    const input = (body as { variables: { input: { count: number; types?: string[] } } }).variables.input;
    const results = input.count === 0 ? [] : [
        { entity: { urn: DS, type: 'DATASET', name: 'orders', platform: { name: 'snowflake' }, properties: { name: 'orders', description: 'One row per order' } } },
        { entity: { urn: 'urn:li:dashboard:(looker,sales)', type: 'DASHBOARD', platform: { name: 'looker' }, properties: { name: 'Sales', description: null } } },
    ].filter((r) => !input.types || input.types.includes(r.entity.type));
    return { data: { searchAcrossEntities: { total: input.count === 0 ? 17 : results.length, searchResults: results, facets: FACETS } } };
}

describe('the Catalog page', () => {
    it('counts assets by entity type, browses by type, platform and domain, and links to asset pages', async () => {
        const calls: MockCall[] = [];
        const nav = wrap(<CatalogPage />, { 'GQL cairnConsumerSearch': searchAnswer, 'GET /collab/enrich': {} }, '/catalog', calls);
        const types = await screen.findByRole('group', { name: 'Entity type' });
        await waitFor(() => expect(within(types).getByRole('button', { name: /All assets\s*17/ })).toBeTruthy());
        expect(within(types).getByRole('button', { name: /Dataset\s*12/ })).toBeTruthy();
        expect(within(types).getByRole('button', { name: /Pipeline\s*2/ })).toBeTruthy();
        expect(within(types).queryByRole('button', { name: /ML model/ })).toBeNull();          // none of them
        const asked = () => calls.filter((c) => c.path === '/graphql#cairnConsumerSearch')
            .map((c) => (c.body as { variables: { input: { types: string[]; count: number; orFilters?: unknown } } }).variables.input);
        expect(asked().every((i) => JSON.stringify(i.types) === JSON.stringify([...ASSET_TYPES]))).toBe(true);
        const row = await screen.findByRole('link', { name: /orders/ });
        expect(row.getAttribute('href')).toBe(`/asset/${encodeURIComponent(DS)}`);
        // Facets beside the results: platform and domain (the type is the row of buttons above).
        const filters = screen.getByRole('navigation', { name: 'Search filters' });
        expect(within(filters).getByText('Platform')).toBeTruthy();
        expect(within(filters).getByText('Finance')).toBeTruthy();
        expect(within(filters).queryByText('Type')).toBeNull();
        fireEvent.click(within(types).getByRole('button', { name: /Dashboard\s*3/ }));
        await waitFor(() => expect(nav.where()).toBe('/catalog?f._entityType=DASHBOARD'));
        await waitFor(() => expect(asked().some((i) => i.count === 20 && JSON.stringify(i.types) === '["DASHBOARD"]')).toBe(true));
        expect(within(types).getByRole('button', { name: /Dashboard/ }).getAttribute('aria-pressed')).toBe('true');
        fireEvent.click(await screen.findByRole('checkbox', { name: /looker/ }));
        await waitFor(() => expect(nav.where()).toBe('/catalog?f._entityType=DASHBOARD&f.platform=urn%3Ali%3AdataPlatform%3Alooker'));
        await waitFor(() => expect(asked().some((i) => JSON.stringify(i.orFilters)
            === JSON.stringify([{ and: [{ field: 'platform', values: ['urn:li:dataPlatform:looker'] }] }]))).toBe(true));
    });

    it('a search also finds terms, products, domains and people; bulk edit and shortcuts are there (review of 6c666a7)', async () => {
        const calls: MockCall[] = [];
        const term = { entity: { urn: 'urn:li:glossaryTerm:revenue', type: 'GLOSSARY_TERM', properties: { name: 'Revenue', description: null } } };
        const answer = (body: unknown) => {
            const input = (body as { variables: { input: { count: number; types?: string[] } } }).variables.input;
            const base = searchAnswer(body).data.searchAcrossEntities;
            const withTerm = input.count && input.types?.includes('GLOSSARY_TERM') ? [...base.searchResults, term] : base.searchResults;
            return { data: { searchAcrossEntities: { ...base, searchResults: withTerm, total: input.count === 0 ? 17 : withTerm.length,
                facets: [{ ...FACETS[0], aggregations: [...FACETS[0].aggregations, { value: 'GLOSSARY_TERM', count: 1, entity: null }] }, ...FACETS.slice(1)] } } };
        };
        const nav = wrap(<CatalogPage />, { 'GQL cairnConsumerSearch': answer, 'GET /collab/enrich': {}, 'GET /collab/bookmarks': [],
            'GET /v1/ontology/search-terms': [] }, '/catalog?q=revenue', calls);
        const asked = () => calls.filter((c) => c.path === '/graphql#cairnConsumerSearch')
            .map((c) => (c.body as { variables: { input: { types: string[]; count: number } } }).variables.input);
        expect((await screen.findByRole('link', { name: /Revenue/ })).getAttribute('href'))
            .toBe(`/asset/${encodeURIComponent('urn:li:glossaryTerm:revenue')}`);
        for (const t of ['GLOSSARY_TERM', 'DATA_PRODUCT', 'DOMAIN', 'CORP_USER', 'CORP_GROUP', 'DATASET']) {
            expect(asked().every((i) => i.types.includes(t))).toBe(true);
        }
        const types = screen.getByRole('group', { name: 'Entity type' });
        await waitFor(() => expect(within(types).getByRole('button', { name: /Business term\s*1/ })).toBeTruthy());
        expect(screen.getByText(/j\/k move/)).toBeTruthy();
        // j then Enter opens the second result.
        fireEvent.keyDown(document.body, { key: 'j' });
        fireEvent.keyDown(document.body, { key: 'Enter' });
        await waitFor(() => expect(nav.where()).toBe(`/asset/${encodeURIComponent('urn:li:dashboard:(looker,sales)')}`));
    });

    it('browsing without a query stays on assets and still offers bulk edit', async () => {
        const calls: MockCall[] = [];
        wrap(<CatalogPage />, { 'GQL cairnConsumerSearch': searchAnswer, 'GET /collab/enrich': {}, 'GET /collab/bookmarks': [] }, '/catalog', calls);
        await screen.findByRole('link', { name: /orders/ });
        expect(screen.getByRole('button', { name: /bulk edit/i })).toBeTruthy();
    });

    it('says when Core cannot be reached', async () => {
        wrap(<CatalogPage />, { 'GQL cairnConsumerSearch': new Error('core down') }, '/catalog');
        expect((await screen.findAllByRole('alert')).length).toBeGreaterThan(0);
    });
});

// ---- data products -------------------------------------------------------------------------------------------------
const MINE = { ...PRODUCT, owners: [ME], contract: 'BROKEN' };
const RECORD = { id: 'customer360', urn: PRODUCT.urn, name: 'Customer 360', description: 'Unified customer view',
    domain: 'urn:li:domain:finance', owners: [ME], assets: [DS, 'urn:li:dashboard:(looker,sales)'], output_ports: [DS],
    managed_by_cairn: true };
const CONTRACT = { dataset: DS, version: '1.2.0', owner: ME, status: 'BROKEN', created_by: ME, checked_ms: Date.now() - 60_000,
    updated_ms: Date.now(), spec: { dataset: DS, version: '1.2.0', freshness: { max_age_hours: 24 },
        schema: { columns: [{ name: 'id', type: 'integer', required: true }], allow_extra_columns: true } },
    parts: [{ part: 'freshness', status: 'BROKEN', detail: 'last data 30 h ago' }] };

describe('Data products', () => {
    it('lists products with domain, owners, assets, output ports, health and contract', async () => {
        wrap(<DataProductsPage />, { 'GET /marketplace/products': [MINE], 'GET /catalog/products': { products: [RECORD] } }, '/products');
        const link = await screen.findByRole('link', { name: 'Customer 360' });
        expect(link.getAttribute('href')).toBe(`/products/${encodeURIComponent(PRODUCT.urn)}`);
        const row = link.closest('tr') as HTMLElement;
        expect(within(row).getByRole('link', { name: 'Finance' }).getAttribute('href')).toBe('/domains/urn%3Ali%3Adomain%3Afinance');
        expect(within(row).getByText('ana')).toBeTruthy();
        await waitFor(() => expect(within(row).getByText(/1 output ports/)).toBeTruthy());
        expect(within(row).getByText('Failing')).toBeTruthy();
        expect(screen.getByRole('button', { name: 'New data product' })).toBeTruthy();
    });

    it('a product page: assets with output ports, contracts on its datasets, lineage and editing', async () => {
        const detail = { ...MINE, readme_md: '# Customer 360', subscribed: false,
            assets: [{ urn: DS, name: 'orders', type: 'DATASET', platform: 'snowflake' },
                { urn: 'urn:li:dashboard:(looker,sales)', name: 'Sales', type: 'DASHBOARD', platform: 'looker' }] };
        wrap(<DataProductPage />, {
            [`GET /marketplace/products/${PRODUCT.urn}`]: detail,
            'GET /catalog/products/customer360': RECORD,
            'GET /quality/contracts': [CONTRACT],
            'GET /embed/v1/graph': { nodes: [{ urn: DS, name: 'orders', type: 'DATASET', platform: 'snowflake', quality_status: null }],
                edges: [], hidden_count: 0 },
        }, `/products/${encodeURIComponent(PRODUCT.urn)}`, [], '/products/:urn');
        expect(await screen.findByRole('heading', { name: 'Customer 360', level: 1 })).toBeTruthy();
        // One product page (L149): Overview · Assets & ports · Contracts & service levels · Pipelines & health · Lineage ·
        // Access · Distribution.
        expect(screen.getAllByRole('tab').map((t) => (t.textContent ?? '').replace(/ \(\d+\)$/, ''))).toEqual(['Overview',
            'Assets & ports', 'Contracts & service levels', 'Pipelines & health', 'Lineage', 'Access', 'Distribution']);
        fireEvent.click(screen.getByRole('tab', { name: 'Assets & ports (2)' }));
        const ordersRow = screen.getByRole('link', { name: 'orders' }).closest('tr') as HTMLElement;
        expect(within(ordersRow).getByText('Output port')).toBeTruthy();
        expect(within(screen.getByRole('link', { name: 'Sales' }).closest('tr') as HTMLElement).queryByText('Output port')).toBeNull();
        await waitFor(() => expect(screen.getByRole('tab', { name: 'Contracts & service levels (1)' })).toBeTruthy());
        fireEvent.click(screen.getByRole('tab', { name: 'Contracts & service levels (1)' }));
        expect((await screen.findByRole('link', { name: 'orders' }, {})).getAttribute('href')).toBeTruthy();
        expect(screen.getAllByText('Failing').length).toBeGreaterThan(0);
        fireEvent.click(screen.getByRole('tab', { name: 'Lineage' }));
        expect(await screen.findByRole('combobox', { name: 'Lineage of' })).toBeTruthy();
        // An owner edits it with the generated form, prefilled from Cairn's record.
        fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
        const dialog = await screen.findByRole('dialog');
        expect(within(dialog).getAllByText(/Edit data product/).length).toBeGreaterThan(0);
    });
});

// ---- contracts --------------------------------------------------------------------------------------------------------
describe('Contracts', () => {
    const other = { ...CONTRACT, dataset: 'urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.refunds,PROD)', status: 'MET', owner: null };
    it('lists contracts across assets as passing, failing or not checked, filters by status, and opens one', async () => {
        const nav = wrap(<ContractsPage />, { 'GET /quality/contracts': [CONTRACT, other] }, '/contracts');
        await screen.findByRole('link', { name: 'orders' });
        expect(screen.getByRole('button', { name: 'All 2' })).toBeTruthy();
        expect(screen.getByText('Passing', { selector: '.label' })).toBeTruthy();
        expect(screen.getByText('Failing', { selector: '.label' })).toBeTruthy();
        fireEvent.click(screen.getByRole('button', { name: 'Failing 1' }));
        await waitFor(() => expect(nav.where()).toBe('/contracts?status=BROKEN'));
        await waitFor(() => expect(screen.queryByRole('link', { name: 'refunds' })).toBeNull());
        expect(screen.getByRole('link', { name: 'orders' }).getAttribute('href')).toBe(`/contracts/${encodeURIComponent(DS)}`);
        expect(screen.getByRole('button', { name: 'New contract' })).toBeTruthy();
    });

    it('a contract page shows its terms and whether each holds', async () => {
        wrap(<ContractPage />, { 'GET /quality/contracts/one': { ...CONTRACT, versions: [] } }, `/contracts/${encodeURIComponent(DS)}`,
            [], '/contracts/:dataset');
        expect(await screen.findByText('last data 30 h ago')).toBeTruthy();
        expect(await screen.findByText('Fresh: new data at least every 24 hours.')).toBeTruthy();
        expect(screen.getByText('id', { selector: 'code' })).toBeTruthy();
        expect(screen.getByRole('button', { name: 'Publish a new version' })).toBeTruthy();
    });
});

// ---- glossary and domains -------------------------------------------------------------------------------------------
describe('Glossary and domains', () => {
    const TERM = 'urn:li:glossaryTerm:Revenue';
    it('shows term groups and terms from Core, read-only with the reason for someone who may not change them', async () => {
        wrap(<GlossaryPage />, {
            'GET /catalog/glossary/rights': { may_manage: false, reason: 'MANAGE_GLOSSARIES is needed to add to the top of the glossary' },
            'GQL cairnGlossaryRoot': { data: {
            getRootGlossaryNodes: { nodes: [{ urn: 'urn:li:glossaryNode:Finance', type: 'GLOSSARY_NODE',
                properties: { name: 'Finance', description: 'Money words' }, childrenCount: { termsCount: 4, nodesCount: 1 } }] },
            getRootGlossaryTerms: { terms: [{ urn: TERM, type: 'GLOSSARY_TERM', name: 'Revenue',
                properties: { name: 'Revenue', description: 'Money in', definition: 'Money in' } }] },
        } } }, '/glossary');
        expect((await screen.findByRole('link', { name: /Finance/ })).getAttribute('href')).toBe('/glossary/urn%3Ali%3AglossaryNode%3AFinance');
        expect(screen.getByText('4 terms · 1 groups')).toBeTruthy();
        expect(screen.getByRole('link', { name: 'Revenue' })).toBeTruthy();
        await waitFor(() => expect(document.querySelector('[data-read-only]')?.textContent)
            .toBe('Read-only for you: MANAGE_GLOSSARIES is needed to add to the top of the glossary.'));
        expect(document.querySelector('[data-in-core]')?.textContent).toContain('Kept in Cairn Core');
        expect(screen.queryByRole('button', { name: 'New term' })).toBeNull();
        fireEvent.change(screen.getByLabelText('Filter terms and groups'), { target: { value: 'zzz' } });
        expect(screen.queryByRole('link', { name: 'Revenue' })).toBeNull();
    });

    it('someone who may manage the glossary makes groups and terms with the generated forms', async () => {
        const calls: MockCall[] = [];
        wrap(<GlossaryPage />, {
            'GET /catalog/glossary/rights': { may_manage: true, reason: '' },
            'GQL cairnGlossaryRoot': { data: { getRootGlossaryNodes: { nodes: [] }, getRootGlossaryTerms: { terms: [] } } },
            'POST /catalog/glossary/nodes': (body: unknown) => ({ ...(body as object), urn: 'urn:li:glossaryNode:finance' }),
        }, '/glossary', calls);
        fireEvent.click(await screen.findByRole('button', { name: 'New term group' }));
        const dialog = await screen.findByRole('dialog');
        expect(within(dialog).getAllByText(/New term group/).length).toBeGreaterThan(0);
        expect(screen.getByRole('button', { name: 'New term' })).toBeTruthy();
    });

    it('a term: a steward edits or deletes it, applies it to an asset; others propose a change', async () => {
        const calls: MockCall[] = [];
        const record = { id: 'Revenue', urn: TERM, name: 'Revenue', definition: 'Money in', parent: 'Finance', is_a: [], has_a: [],
            related: [], owners: [], stewards: ['urn:li:corpuser:olga'], custom_properties: {}, source_url: null,
            deprecated: true, deprecation_note: 'use Income' };
        const routes = {
            'GQL cairnGlossaryTerm': { data: { glossaryTerm: { urn: TERM, type: 'GLOSSARY_TERM', name: 'Revenue',
                properties: { name: 'Revenue', definition: 'Money in' }, parentNodes: { nodes: [] }, ownership: { owners: [] },
                isA: { relationships: [] }, hasA: { relationships: [] }, related: { relationships: [] } } } },
            'GET /catalog/glossary/terms/Revenue': record, 'GQL cairnConsumerSearch': searchAnswer, 'GET /collab/enrich': {},
        };
        wrap(<GlossaryEntryPage />, { ...routes, 'GET /catalog/glossary/rights': { may_manage: true, reason: '', may_propose: true } },
            `/glossary/${encodeURIComponent(TERM)}`, calls, '/glossary/:urn');
        expect(await screen.findByRole('button', { name: 'Edit' })).toBeTruthy();
        expect(screen.getByRole('button', { name: 'Delete' })).toBeTruthy();
        expect(screen.getByText('Deprecated: use Income')).toBeTruthy();
        expect(screen.getByText('This term is deprecated: it is not put on more assets.')).toBeTruthy();
        fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
        const dialog = await screen.findByRole('dialog');
        expect(within(dialog).getAllByText(/Edit term/).length).toBeGreaterThan(0);
    });

    it('a term someone may not change: read-only with the reason, and a proposal instead; applying it to an asset', async () => {
        const calls: MockCall[] = [];
        const record = { id: 'Revenue', urn: TERM, name: 'Revenue', definition: 'Money in', parent: null, is_a: [], has_a: [],
            related: [], owners: [], stewards: [], custom_properties: {}, source_url: null, deprecated: false, deprecation_note: '' };
        wrap(<GlossaryEntryPage />, {
            'GQL cairnGlossaryTerm': { data: { glossaryTerm: { urn: TERM, type: 'GLOSSARY_TERM', name: 'Revenue',
                properties: { name: 'Revenue', definition: 'Money in' }, parentNodes: { nodes: [] }, ownership: { owners: [] },
                isA: { relationships: [] }, hasA: { relationships: [] }, related: { relationships: [] } } } },
            'GET /catalog/glossary/terms/Revenue': record, 'GQL cairnConsumerSearch': searchAnswer, 'GET /collab/enrich': {},
            'GET /catalog/glossary/rights': { may_manage: false, reason: 'MANAGE_GLOSSARIES is needed on it or on a term group above it',
                may_propose: true },
            'POST /catalog/assets/terms': { changed: true },
        }, `/glossary/${encodeURIComponent(TERM)}`, calls, '/glossary/:urn');
        expect(await screen.findByRole('button', { name: 'Propose a change' })).toBeTruthy();
        expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull();
        expect(document.querySelector('[data-read-only]')?.textContent).toContain('MANAGE_GLOSSARIES is needed on it');
        // Apply it to an asset: Core's search finds it, Cairn's API puts the term on it (as the person).
        const apply = screen.getByRole('region', { name: 'Apply to an asset' });
        const box = within(apply).getByRole('combobox', { name: 'Find an asset' });
        fireEvent.change(box, { target: { value: 'orders' } });
        fireEvent.click(await screen.findByText('orders (snowflake)', { selector: '.ant-select-item-option-content' }, { timeout: 3000 }));
        fireEvent.change(within(apply).getByLabelText('Column (optional)'), { target: { value: 'amount' } });
        fireEvent.click(within(apply).getByRole('button', { name: 'Apply' }));
        await waitFor(() => expect(calls.find((c) => c.method === 'POST' && c.path === '/catalog/assets/terms')?.body)
            .toEqual({ asset: DS, term: TERM, field: 'amount' }));
    });

    it('a term: definition, related terms, stewards and the assets that use it', async () => {
        const calls: MockCall[] = [];
        wrap(<GlossaryEntryPage />, {
            'GQL cairnGlossaryTerm': { data: { glossaryTerm: { urn: TERM, type: 'GLOSSARY_TERM', name: 'Revenue', hierarchicalName: 'Finance.Revenue',
                properties: { name: 'Revenue', description: 'Money **in**', definition: 'Money in', termSource: 'INTERNAL' },
                parentNodes: { nodes: [{ urn: 'urn:li:glossaryNode:Finance', properties: { name: 'Finance' } }] },
                ownership: { owners: [{ owner: { urn: 'urn:li:corpuser:olga', username: 'olga' } }] },
                isA: { relationships: [{ entity: { urn: 'urn:li:glossaryTerm:Income', type: 'GLOSSARY_TERM', properties: { name: 'Income' } } }] },
                hasA: { relationships: [] }, related: { relationships: [] } } } },
            'GQL cairnConsumerSearch': searchAnswer, 'GET /collab/enrich': {},
        }, `/glossary/${encodeURIComponent(TERM)}`, calls, '/glossary/:urn');
        expect(await screen.findByRole('heading', { name: 'Revenue', level: 1 })).toBeTruthy();
        expect(screen.getByText('in', { selector: 'strong' })).toBeTruthy();                  // the definition, as Markdown
        expect(screen.getByRole('link', { name: 'Income' }).getAttribute('href')).toBe('/glossary/urn%3Ali%3AglossaryTerm%3AIncome');
        expect(screen.getByText('olga')).toBeTruthy();
        expect(screen.getByRole('link', { name: 'Finance' })).toBeTruthy();
        expect(await screen.findByRole('link', { name: /orders/ })).toBeTruthy();
        const search = calls.find((c) => c.path === '/graphql#cairnConsumerSearch')!.body as { variables: { input: { orFilters: unknown } } };
        expect(search.variables.input.orFilters).toEqual([{ and: [{ field: 'glossaryTerms', values: [TERM] }] }]);
        // Related terms are Core's edges of glossaryRelatedTerms (GlossaryRelatedTerms.pdl): IsA, HasA and IsRelatedTo.
        const term = (calls.find((c) => c.path === '/graphql#cairnGlossaryTerm')!.body as { query: string }).query;
        for (const edge of ['IsA', 'HasA', 'IsRelatedTo']) expect(term).toContain(`types: ["${edge}"], direction: OUTGOING`);
        expect(term).not.toContain('"RelatedTo"');
    });

    it("an asset's terms and domain: changed by someone who may, read-only with the reason otherwise", async () => {
        const calls: MockCall[] = [];
        const labels = { asset: DS, terms: [TERM], columns: { amount: ['urn:li:glossaryTerm:Income'], id: [] },
            domain: 'urn:li:domain:finance', may: { terms: true, column_terms: false, domain: true },
            why: { terms: '', column_terms: 'EDIT_DATASET_COL_GLOSSARY_TERMS is needed on this dataset', domain: '' } };
        const terms = { terms: [{ id: 'Revenue', urn: TERM, name: 'Revenue', deprecated: false },
            { id: 'Income', urn: 'urn:li:glossaryTerm:Income', name: 'Income', deprecated: false }] };
        const domainsList = { domains: [{ id: 'finance', urn: 'urn:li:domain:finance', name: 'Finance' },
            { id: 'sales', urn: 'urn:li:domain:sales', name: 'Sales' }] };
        wrap(<AssetLabelsEditor urn={DS} />, { 'GET /catalog/assets/labels': labels, 'GET /catalog/glossary/terms': terms,
            'GET /catalog/domains': domainsList, 'POST /catalog/assets/terms/remove': { changed: true },
            'PUT /catalog/assets/domain': { changed: true, domain: 'urn:li:domain:sales' } }, '/', calls);
        const list = await screen.findByRole('list', { name: 'Terms on this asset' });
        await waitFor(() => expect(within(list).getByText('Revenue')).toBeTruthy());
        expect(screen.getByText('amount', { selector: 'code' })).toBeTruthy();
        // Taking a term off the asset.
        fireEvent.click(within(list).getByRole('img', { name: 'close' }));
        await waitFor(() => expect(calls.find((c) => c.path === '/catalog/assets/terms/remove')?.body)
            .toEqual({ asset: DS, term: TERM, field: null }));
        // Its domain: picked from Cairn's list of domains.
        fireEvent.mouseDown(screen.getByRole('combobox', { name: 'Domain' }));
        fireEvent.click(await screen.findByText('Sales', { selector: '.ant-select-item-option-content' }));
        await waitFor(() => expect(calls.find((c) => c.method === 'PUT' && c.path === '/catalog/assets/domain')?.body)
            .toEqual({ asset: DS, domain: 'urn:li:domain:sales' }));
    });

    it('an asset someone may not label: the reasons, no controls', async () => {
        const labels = { asset: DS, terms: [], columns: {}, domain: null, may: { terms: false, column_terms: false, domain: false },
            why: { terms: 'EDIT_ENTITY_GLOSSARY_TERMS is needed on this asset', column_terms: 'x',
                domain: 'EDIT_ENTITY_DOMAINS is needed on this asset' } };
        wrap(<AssetLabelsEditor urn={DS} />, { 'GET /catalog/assets/labels': labels, 'GET /catalog/glossary/terms': { terms: [] },
            'GET /catalog/domains': { domains: [] } });
        expect(await screen.findByText('EDIT_ENTITY_GLOSSARY_TERMS is needed on this asset')).toBeTruthy();
        expect(screen.getByText('EDIT_ENTITY_DOMAINS is needed on this asset')).toBeTruthy();
        expect(screen.queryByRole('button', { name: 'Add term' })).toBeNull();
        expect(screen.queryByRole('combobox', { name: 'Domain' })).toBeNull();
    });

    it('domains with their owners and asset counts; a domain with its products and assets', async () => {
        wrap(<DomainsPage />, { 'GQL cairnDomains': { data: { listDomains: { total: 1, domains: [{ urn: 'urn:li:domain:finance',
            type: 'DOMAIN', properties: { name: 'Finance', description: 'Money' },
            ownership: { owners: [{ owner: { urn: 'urn:li:corpGroup:fin', name: 'fin' } }] }, entities: { total: 42 } }] } } } }, '/domains');
        const tile = await screen.findByRole('link', { name: /Finance/ });
        expect(tile.getAttribute('href')).toBe('/domains/urn%3Ali%3Adomain%3Afinance');
        expect(within(tile).getByText('42 assets · fin')).toBeTruthy();
    });

    it('a domain page', async () => {
        wrap(<DomainPage />, {
            'GQL cairnDomain': { data: { domain: { urn: 'urn:li:domain:finance', type: 'DOMAIN', properties: { name: 'Finance', description: 'Money' },
                ownership: { owners: [{ owner: { urn: 'urn:li:corpuser:olga', username: 'olga' } }] }, parentDomains: { domains: [] },
                children: { relationships: [{ entity: { urn: 'urn:li:domain:tax', type: 'DOMAIN', properties: { name: 'Tax' } } }] } } } },
            'GET /marketplace/products': [MINE], 'GQL cairnConsumerSearch': searchAnswer, 'GET /collab/enrich': {},
        }, '/domains/urn%3Ali%3Adomain%3Afinance', [], '/domains/:urn');
        expect(await screen.findByRole('heading', { name: 'Finance', level: 1 })).toBeTruthy();
        expect(screen.getByRole('link', { name: 'Tax' })).toBeTruthy();
        expect((await screen.findByRole('link', { name: 'Customer 360' })).getAttribute('href'))
            .toBe(`/products/${encodeURIComponent(PRODUCT.urn)}`);
        expect(await screen.findByRole('link', { name: /orders/ })).toBeTruthy();
    });
});

// ---- lineage, home, map ---------------------------------------------------------------------------------------------
describe('Lineage, home and the platform map', () => {
    it('column lineage from Core: each column with the columns that feed it', () => {
        const up = 'urn:li:dataset:(urn:li:dataPlatform:iceberg,raw.orders,PROD)';
        const edges = columnEdges(DS, [{ upstreams: [{ urn: `urn:li:schemaField:(${up},amount)`, path: 'amount' }],
            downstreams: [{ urn: `urn:li:schemaField:(${DS},total)`, path: 'total' }], transformOperation: 'SUM' }]);
        expect(edges).toEqual([{ column: 'total', from: [{ dataset: up, column: 'amount' }], operation: 'SUM' }]);
    });

    it('the lineage section draws the picked asset and its column lineage', async () => {
        wrap(<LineagePage />, {
            'GET /embed/v1/graph': { nodes: [{ urn: DS, name: 'orders', type: 'DATASET', platform: 'snowflake', quality_status: null },
                { urn: 'urn:li:dataset:(urn:li:dataPlatform:iceberg,raw.orders,PROD)', name: 'raw.orders', type: 'DATASET', platform: 'iceberg', quality_status: null }],
            edges: [{ source: 'urn:li:dataset:(urn:li:dataPlatform:iceberg,raw.orders,PROD)', target: DS, indirect: false }], hidden_count: 0 },
            'GQL cairnColumnLineage': { data: { dataset: { urn: DS, fineGrainedLineages: [] } } },
        }, `/lineage?urn=${encodeURIComponent(DS)}`);
        expect(await screen.findByTestId('lineage-graph')).toBeTruthy();
        expect(await screen.findByText('No column-level lineage recorded for this dataset.')).toBeTruthy();
    });

    it('issues on your assets: your failing contracts and failing checks', () => {
        const monitors = [{ id: 'm', name: 'fresh', dataset_urn: DS, created_by: ME, last_evaluation: { status: 'FAIL' } },
            { id: 'n', name: 'other', dataset_urn: DS, created_by: 'urn:li:corpuser:bob', last_evaluation: { status: 'FAIL' } }];
        const issues = issuesFor(ME, [CONTRACT as never], monitors as never);
        expect(issues.map((i) => i.kind)).toEqual(['contract', 'check']);
        expect(issuesFor(undefined, [CONTRACT as never], monitors as never)).toEqual([]);
    });

    it('draws Cairn Core at the centre with the tiers around it', () => {
        wrap(<CoreMap />, {});
        const map = document.querySelector('[data-core-map]') as HTMLElement;
        expect(within(map).getByRole('heading', { name: 'Cairn Core' })).toBeTruthy();
        expect([...map.querySelectorAll('[data-tier]')].map((e) => e.getAttribute('data-tier')))
            .toEqual(['catalog', 'govern', 'activate', 'extend']);
        expect(within(map).getByRole('link', { name: 'Catalog' }).getAttribute('href')).toBe('/catalog');
    });
});
