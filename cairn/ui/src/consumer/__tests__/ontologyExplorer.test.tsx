import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { installOntologyDemo } from '../../../demo/ontologySamples';
import { CairnProvider } from '../context/CairnProvider';
import { I18nProvider } from '../i18n';
import type { ExplorerData, OntologyRun, Proposal } from '../ontology/api';
import { CompileWizard } from '../ontology/CompileWizard';
import { BusinessTerms, GroundedChips, TermSearchResults } from '../ontology/Embeds';
import {
    CLUSTER_THRESHOLD,
    NO_FILTERS,
    applyFilters,
    buildGraph,
    cluster,
    facets,
    fit,
    layout,
    neighbours,
    searchNodes,
    zoomAt,
} from '../ontology/graph';
import { ReviewQueue } from '../ontology/ReviewQueue';
import { aboveThreshold, filterQueue, keyIntent, queueReducer, targets } from '../ontology/review';
import { INITIAL, canNext, next, percent, phaseLabel, resumable, scopeBody } from '../ontology/wizard';
import { OntologyPage } from '../pages/OntologyPage';
import { UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

type Json = any; // eslint-disable-line @typescript-eslint/no-explicit-any

/** The demo's in-memory ontology API (the same sample the standalone demo shows), as mock routes. */
function demoRoutes(): Record<string, (body: unknown, url: URL) => unknown> {
    const handlers: [string, RegExp, (b: Json, q: URLSearchParams, p: string[]) => unknown][] = [];
    installOntologyDemo(
        (method, pattern, h) => handlers.push([method, new RegExp(`^${pattern.replace(/:id/g, '([^/]+)')}$`), h]),
        (status, msg) => {
            throw Object.assign(new Error(msg), { status });
        },
        'urn:li:corpuser:alice',
    );
    return new Proxy({}, {
        has: () => true,
        getOwnPropertyDescriptor: () => ({ configurable: true, enumerable: true, writable: true }),
        get: (_t, key: string) => {
            if (key === 'GET /ui/config') return UI_CONFIG;
            if (key === 'GET /notify/inbox') return [];
            const [method, path] = key.split(' ');
            for (const [m, re, h] of handlers) {
                const hit = m === method ? re.exec(path) : null;
                if (hit) return (body: unknown, url: URL) => {
                    try {
                        return h(body, url.searchParams, hit.slice(1));
                    } catch (e) {
                        return new Response(JSON.stringify({ detail: (e as Error).message }), { status: (e as { status?: number }).status ?? 500 });
                    }
                };
            }
            return undefined;
        },
    }) as Record<string, (body: unknown, url: URL) => unknown>;
}

async function explorerData(): Promise<ExplorerData> {
    const routes = demoRoutes();
    return (await routes['GET /v1/ontology/explorer'](undefined, new URL('http://x/v1/ontology/explorer'))) as ExplorerData;
}

function big(n: number): ExplorerData {
    const domains = ['Sales', 'Finance', 'Customer', 'Supply'];
    const terms = Array.from({ length: n }, (_, i) => ({
        id: `t${i}`, name: `Term ${i}`, description: '', synonyms: [], table: `tbl${i}`, table_name: `s.t${i}`, catalog: '', schema: `s${i % 7}`,
        table_label: `s.t${i}`, domain: null, domain_name: domains[i % 4], owners: [], attributes: 3, date_formats: 0, bridge: false,
        provenance: ['model'], confidence: (i % 10) / 10, status: (i % 3 ? 'accepted' : 'proposed') as 'accepted' | 'proposed', proposal: null,
    }));
    const relationships = Array.from({ length: n }, (_, i) => ({
        id: `r${i}`, source: `tbl${i}`, target: `tbl${(i * 7 + 3) % n}`, source_column: 'x_id', target_column: 'id', source_term: `t${i}`,
        target_term: `t${(i * 7 + 3) % n}`, cardinality: 'many_to_one', confidence: 0.8, join_default: true, provenance: ['catalog_fk'], queries: 0,
        status: 'accepted' as const, proposal: null, bridge: false,
    }));
    return { run_id: 'r', version: 1, terms, relationships, bridges: [], metrics: [], tables: [] };
}

describe('the graph', () => {
    it('has a node per term, bridge tables as their own kind, and labelled edges', async () => {
        const d = await explorerData();
        expect(d.terms.length).toBeGreaterThanOrEqual(38);
        const g = buildGraph(d);
        const bridges = g.nodes.filter((n) => n.kind === 'bridge');
        expect(bridges.map((b) => b.label).sort()).toEqual(['sales.order_promotions', 'supply.product_categories', 'supply.product_suppliers']);
        expect(facets(g).groups).toEqual(['Customer', 'Finance', 'Sales', 'Supply Chain']);
        const e = g.edges.find((x) => x.id === 'rel_orders_customer_id')!;
        expect(e.label).toBe('customer_id → id · N:1');
        expect(e.source).toBe('term_orders');
        expect(g.edges.find((x) => x.id === 'rel_product_categories_product_id')!.type).toBe('bridge');
        expect(g.edges.find((x) => x.id === 'rel_carts_customer_id')!.type).toBe('inferred');
        expect(neighbours(g, 'term_customers').has('term_orders')).toBe(true);
    });

    it('filters by domain, schema, status and confidence', async () => {
        const g = buildGraph(await explorerData());
        const fin = applyFilters(g, { ...NO_FILTERS, domains: ['Finance'] });
        expect(fin.nodes.every((n) => n.group === 'Finance')).toBe(true);
        expect(fin.edges.every((e) => fin.nodes.some((n) => n.id === e.source) && fin.nodes.some((n) => n.id === e.target))).toBe(true);
        const proposed = applyFilters(g, { ...NO_FILTERS, statuses: ['proposed'] });
        expect(proposed.nodes.filter((n) => n.kind === 'term').every((n) => n.status === 'proposed')).toBe(true);
        expect(proposed.nodes.some((n) => n.label === 'Return')).toBe(true);
        const sure = applyFilters(g, { ...NO_FILTERS, minConfidence: 0.9 });
        expect(sure.edges.every((e) => e.confidence >= 0.9)).toBe(true);
        const crm = applyFilters(g, { ...NO_FILTERS, schemas: ['crm'] });
        expect(crm.nodes.every((n) => n.schema === 'crm')).toBe(true);
    });

    it('finds a term by name and lays the graph out deterministically inside the canvas', async () => {
        const g = buildGraph(await explorerData());
        expect(searchNodes(g, 'cust').map((n) => n.label)[0]).toBe('Customer');
        const a = layout(g);
        const b = layout(g);
        expect([...a.entries()]).toEqual([...b.entries()]);
        for (const p of a.values()) {
            expect(p.x).toBeGreaterThanOrEqual(0);
            expect(p.x).toBeLessThanOrEqual(1000);
        }
        const v = fit([...a.values()], 960, 620);
        expect(v.k).toBeGreaterThan(0);
        const z = zoomAt(v, 2, 480, 310);
        expect(z.k).toBeCloseTo(Math.min(6, v.k * 2));
    });

    it('stays usable for a big ontology: grouped into domains, and laid out quickly', () => {
        const g = buildGraph(big(600));
        expect(g.nodes.length).toBeGreaterThan(CLUSTER_THRESHOLD);
        const c = cluster(g);
        expect(c.clustered).toBe(true);
        expect(c.nodes.map((n) => [n.label, n.size])).toEqual(expect.arrayContaining([['Sales', 150], ['Finance', 150]]));
        expect(c.edges.reduce((s, e) => s + e.count, 0)).toBeLessThanOrEqual(600);
        const started = Date.now();
        const pos = layout(g);
        expect(pos.size).toBe(600);
        expect(Date.now() - started).toBeLessThan(10_000);
    });
});

describe('the review queue', () => {
    const P = (id: string, kind: Proposal['kind'], confidence: number): Proposal => ({
        id, type: '', kind, status: 'OPEN', confidence, title: id, summary_md: '', entities: [], element_id: null, version: 1,
    });
    const items = [P('a', 'term', 0.95), P('b', 'relationship', 0.6), P('c', 'metric', 0.91), P('d', 'term', 0.4)];

    it('filters, moves, selects and targets', () => {
        expect(filterQueue(items, { kind: 'term', minConfidence: 0.5 }).map((p) => p.id)).toEqual(['a']);
        expect(aboveThreshold(items, 0.9).map((p) => p.id)).toEqual(['a', 'c']);
        let s = { cursor: 0, selected: [] as string[] };
        s = queueReducer(s, { type: 'move', by: 1, count: 4 });
        s = queueReducer(s, { type: 'move', by: 9, count: 4 });
        expect(s.cursor).toBe(3);
        expect(targets(items, s)).toEqual(['d']);
        s = queueReducer(s, { type: 'toggle', id: 'b' });
        s = queueReducer(s, { type: 'toggle', id: 'c' });
        expect(targets(items, s)).toEqual(['b', 'c']);
        s = queueReducer(s, { type: 'removed', ids: ['b', 'c'], count: 2 });
        expect(s).toEqual({ cursor: 1, selected: [] });
        expect(keyIntent('j')).toEqual({ move: 1 });
        expect(keyIntent('k')).toEqual({ move: -1 });
        expect(keyIntent('a')).toEqual({ decide: 'accept' });
        expect(keyIntent('r')).toEqual({ decide: 'reject' });
        expect(keyIntent('a', { ctrl: true })).toBeNull();
    });

    it('accepts with the keyboard: j moves, a accepts the row under the cursor with the comment', async () => {
        const routes = demoRoutes();
        const { client, cache, calls } = makeClient(routes);
        render(
            <MemoryRouter>
                <CairnProvider client={client} cache={cache}><ReviewQueue /></CairnProvider>
            </MemoryRouter>,
        );
        const list = await screen.findByTestId('review-queue');
        const rows = list.querySelectorAll('[data-proposal]');
        expect(rows.length).toBeGreaterThan(5);
        const second = rows[1].getAttribute('data-proposal');
        fireEvent.change(screen.getByLabelText('Review comment'), { target: { value: 'looks right' } });
        list.focus();
        fireEvent.keyDown(list, { key: 'j' });
        expect(list.querySelector('[aria-current="true"]')?.getAttribute('data-proposal')).toBe(second);
        await act(async () => {
            fireEvent.keyDown(list, { key: 'a' });
        });
        await waitFor(() => expect(screen.getByText(/Accepted 1/)).toBeTruthy());
        const call = calls.find((c) => c.path === '/v1/ontology/proposals/batch')!;
        expect(call.body).toEqual({ ids: [second], action: 'accept', comment: 'looks right' });
        expect(list.querySelector(`[data-proposal="${second}"]`)).toBeNull();
        // x selects two rows and r rejects both.
        fireEvent.keyDown(list, { key: 'x' });
        fireEvent.keyDown(list, { key: 'j' });
        fireEvent.keyDown(list, { key: 'x' });
        await act(async () => {
            fireEvent.keyDown(list, { key: 'r' });
        });
        await waitFor(() => expect(screen.getByText(/Rejected 2/)).toBeTruthy());
    });

    it('previews “accept all above X” before accepting', async () => {
        const { client, cache, calls } = makeClient(demoRoutes());
        render(
            <MemoryRouter>
                <CairnProvider client={client} cache={cache}><ReviewQueue /></CairnProvider>
            </MemoryRouter>,
        );
        await screen.findByTestId('review-queue');
        fireEvent.click(screen.getByRole('button', { name: /Accept all above 0.9/ }));
        const preview = await screen.findByTestId('threshold-preview');
        const n = preview.querySelectorAll('li').length;
        expect(n).toBeGreaterThan(0);
        expect(calls.some((c) => c.path === '/v1/ontology/proposals/batch')).toBe(false);
        await act(async () => {
            fireEvent.click(screen.getByRole('button', { name: `Accept ${n}` }));
        });
        await waitFor(() => expect(screen.getByText(new RegExp(`Accepted ${n}`))).toBeTruthy());
        expect((calls.find((c) => c.path === '/v1/ontology/proposals/batch')!.body as Json).ids).toHaveLength(n);
    });
});

describe('the compile wizard', () => {
    it('has steps that need a scope, then shows the cost', () => {
        expect(canNext(INITIAL)).toBe(true);
        const schemas = { ...INITIAL, scope: 'schemas' as const };
        expect(canNext(schemas)).toBe(false);
        expect(canNext({ ...schemas, schemas: ['crm'] })).toBe(true);
        expect(scopeBody({ ...schemas, schemas: ['crm'] })).toEqual({ schemas: ['crm'] });
        expect(scopeBody({ ...INITIAL, scope: 'tables', tables: ['u'] })).toEqual({ tables: ['u'] });
        expect(scopeBody(INITIAL)).toEqual({});
        expect(canNext(next(next(INITIAL)))).toBe(false);
        const run = (p: Partial<OntologyRun>): OntologyRun => ({ id: 'r', version: 1, state: 'running', mode: 'full', started_ms: 0, finished_ms: null,
            stats: {}, error: null, requested_by: '', ...p });
        expect(percent(run({ progress: { run_id: 'r', phase: 'tables', tables_total: 10, tables_done: 5, scope: null } }))).toBe(40);
        expect(percent(run({ progress: { run_id: 'r', phase: 'publishing', tables_total: 10, tables_done: 10, scope: null } }))).toBe(99);
        expect(percent(run({ state: 'completed' }))).toBe(100);
        expect(phaseLabel(run({ progress: { run_id: 'r', phase: 'tables', tables_total: 10, tables_done: 3, scope: null } }))).toBe('compiling tables: 3 of 10');
        expect(resumable(run({ state: 'cancelled' }))).toBe(true);
        expect(resumable(run({ state: 'completed' }))).toBe(false);
    });

    it('runs a scoped compile, follows it to the end and shows the diff', async () => {
        const { client, cache, calls } = makeClient(demoRoutes());
        render(
            <MemoryRouter>
                <CairnProvider client={client} cache={cache}><CompileWizard /></CairnProvider>
            </MemoryRouter>,
        );
        await screen.findByText(/Every table \(\d+ you may see\)/);
        fireEvent.click(screen.getByLabelText('Some schemas'));
        expect((screen.getByRole('button', { name: 'Next' }) as HTMLButtonElement).disabled).toBe(true);
        fireEvent.mouseDown(screen.getByRole('combobox', { name: 'Schemas' }));
        fireEvent.click(await screen.findByText('crm', { selector: '.ant-select-item-option-content' }));
        fireEvent.click(screen.getByRole('button', { name: 'Next' }));
        fireEvent.click(screen.getByLabelText(/Full: compile every table/));
        fireEvent.click(screen.getByRole('button', { name: 'Next' }));
        expect(await screen.findByText('Tables to compile')).toBeTruthy();
        expect(screen.getByText(/up to \d+ \(4 a table\)/)).toBeTruthy();
        expect((calls.find((c) => c.path === '/v1/ontology/estimate')!.body as Json)).toEqual({ mode: 'full', schemas: ['crm'] });
        await act(async () => {
            fireEvent.click(screen.getByRole('button', { name: 'Run' }));
        });
        expect((calls.find((c) => c.method === 'POST' && c.path === '/v1/ontology/runs')!.body as Json)).toMatchObject({ mode: 'full', schemas: ['crm'], wait: false });
        expect(await screen.findByText(/compiling tables/)).toBeTruthy();
        expect(await screen.findByTestId('run-diff', undefined, { timeout: 20_000 })).toBeTruthy();
        expect(screen.getByText(/new proposals/)).toBeTruthy();
        expect(screen.getByText('Legacy Order')).toBeTruthy();
    }, 30_000);
});

describe('the ontology elsewhere', () => {
    const wrap = (node: React.ReactNode) => {
        const { client, cache } = makeClient(demoRoutes());
        return render(<MemoryRouter><CairnProvider client={client} cache={cache}>{node}</CairnProvider></MemoryRouter>);
    };

    it('an asset page lists the business terms of the table and of each column', async () => {
        wrap(<BusinessTerms urn="urn:li:dataset:(urn:li:dataPlatform:postgres,billing.public.invoices,PROD)" />);
        const box = await screen.findByTestId('business-terms');
        expect(box.querySelector('a[href="/ontology/terms/term_invoices"]')?.textContent).toBe('Invoice');
        expect(box.textContent).toContain('also bill');
        expect(box.textContent).toContain('YYYY-MM-DD');
    });

    it('search shows a term found by its synonym, with a Term badge', async () => {
        wrap(<TermSearchResults query="vendor" />);
        const hits = await screen.findByTestId('term-results');
        expect(hits.textContent).toContain('Term');
        expect(hits.textContent).toContain('Supplier');
        expect(hits.textContent).toContain('synonym “vendor”');
    });

    it('an answer says what it was grounded in', () => {
        wrap(<GroundedChips grounded={{ terms: [{ id: 'term_orders', name: 'Order', table: 'u' }],
            relationships: [{ id: 'r', label: 'orders.customer_id → customers.id' }] }} />);
        expect(screen.getByTestId('grounded-in').textContent).toBe('Grounded in: Orderorders.customer_id → customers.id');
        expect(screen.getByRole('link', { name: 'Order' }).getAttribute('href')).toBe('/ontology/terms/term_orders');
        cleanupGrounded();
    });
});

function cleanupGrounded() {
    // Nothing is shown without grounding.
    const { container } = render(<MemoryRouter><GroundedChips grounded={{ terms: [], relationships: [] }} /></MemoryRouter>);
    expect(container.textContent).toBe('');
}

describe('the explorer page', () => {
    it('searches a term, zooms to it and opens its details beside the graph', async () => {
        const { client, cache } = makeClient(demoRoutes());
        render(
            <MemoryRouter initialEntries={['/ontology']}>
                <I18nProvider initial="en">
                    <CairnProvider client={client} cache={cache}><OntologyPage /></CairnProvider>
                </I18nProvider>
            </MemoryRouter>,
        );
        const canvas = await screen.findByTestId('ontology-canvas');
        expect(canvas.querySelectorAll('[data-node]').length).toBeGreaterThanOrEqual(38);
        const search = screen.getByRole('combobox', { name: 'Find a term' });
        fireEvent.change(search, { target: { value: 'Invoi' } });
        fireEvent.click(await screen.findByText('Invoice', { selector: '.ant-select-item-option-content' }));
        const node = canvas.querySelector('[data-node="term_invoices"]')!;
        expect(node.getAttribute('aria-pressed')).toBe('true');
        expect(canvas.querySelector('[data-node="term_tax_rates"]')!.getAttribute('opacity')).toBe('0.2');   // not a neighbour
        expect(canvas.querySelector('[data-node="term_payments"]')!.getAttribute('opacity')).toBe('1');
        const detail = await screen.findByTestId('term-detail');
        expect(detail.textContent).toContain('bill');
        expect(detail.textContent).toContain('YYYY-MM-DD');
    });
});
