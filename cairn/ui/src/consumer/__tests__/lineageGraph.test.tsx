import { fireEvent, render, screen } from '@testing-library/react';
import React from 'react';
import { MemoryRouter, Route } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import type { LineageGraph } from '../api/types';
import { LineageGraphView, layout } from '../components/LineageGraph';
import { CairnProvider } from '../context/CairnProvider';
import { UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const ROOT = 'urn:li:dataset:(urn:li:dataPlatform:trino,lakehouse.sales.by_region,PROD)';
const ORDERS = 'urn:li:dataset:(urn:li:dataPlatform:trino,lakehouse.sales.orders,PROD)';
const RAW = 'urn:li:dataset:(urn:li:dataPlatform:s3,raw.orders,PROD)';
const DASH = 'urn:li:dashboard:(looker,regional_sales)';
const GRAPH: LineageGraph = {
    nodes: [
        { urn: ROOT, name: 'by_region', type: 'DATASET', platform: 'trino', quality_status: 'HEALTHY' },
        { urn: ORDERS, name: 'orders', type: 'DATASET', platform: 'trino', quality_status: 'FAILING' },
        { urn: RAW, name: 'raw orders', type: 'DATASET', platform: 's3', quality_status: null },
        { urn: DASH, name: 'Regional sales', type: 'DASHBOARD', platform: 'looker', quality_status: 'WARNING' },
    ],
    edges: [
        { source: ORDERS, target: ROOT },
        { source: RAW, target: ROOT, indirect: true },
        { source: ROOT, target: DASH },
    ],
    hidden_count: 2,
};

it('lays upstreams out to the left and downstreams to the right, further hops further out', () => {
    const { placed } = layout(ROOT, GRAPH);
    const x = Object.fromEntries(placed.map((p) => [p.node.urn, p.x]));
    expect(x[RAW]).toBeLessThan(x[ORDERS]);
    expect(x[ORDERS]).toBeLessThan(x[ROOT]);
    expect(x[ROOT]).toBeLessThan(x[DASH]);
    // The same graph always draws the same way.
    expect(layout(ROOT, { ...GRAPH, nodes: [...GRAPH.nodes].reverse() }).placed.map((p) => [p.node.urn, p.x, p.y]).sort())
        .toEqual(placed.map((p) => [p.node.urn, p.x, p.y]).sort());
});

it('places what lies downstream of an asset that is also upstream (a job that reads and writes the asset)', () => {
    // A MERGE job reads by_region and writes it back, and also writes a second table: the job is both upstream
    // and downstream of the asset, and the second table lies downstream through it.
    const JOB = 'urn:li:dataJob:(urn:li:dataFlow:(trino,lakehouse,PROD),merge_by_region)';
    const graph: LineageGraph = {
        nodes: [
            { urn: ROOT, name: 'by_region', type: 'DATASET', platform: 'trino', quality_status: null },
            { urn: JOB, name: 'merge_by_region', type: 'DATA_JOB', platform: 'trino', quality_status: null },
            { urn: ORDERS, name: 'orders', type: 'DATASET', platform: 'trino', quality_status: null },
        ],
        edges: [
            { source: JOB, target: ROOT },
            { source: ROOT, target: JOB },
            { source: JOB, target: ORDERS },
        ],
        hidden_count: 0,
    };
    const { placed } = layout(ROOT, graph);
    const x = Object.fromEntries(placed.map((p) => [p.node.urn, p.x]));
    expect(Object.keys(x).sort()).toEqual([JOB, ORDERS, ROOT].sort());
    expect(x[ORDERS]).toBeGreaterThan(x[ROOT]);
});

it('draws the lineage, says what is hidden, and opens an asset when selected', async () => {
    const { client, calls } = makeClient({ 'GET /ui/config': UI_CONFIG, 'GET /embed/v1/graph': GRAPH });
    let at = '';
    render(
        <MemoryRouter initialEntries={['/asset/x']}>
            <CairnProvider client={client} cache={new QueryCache()}>
                <LineageGraphView urn={ROOT} />
                <Route path="*" render={({ location }) => { at = location.pathname; return null; }} />
            </CairnProvider>
        </MemoryRouter>,
    );
    expect(await screen.findByTestId('lineage-graph')).toBeTruthy();
    expect(screen.getByText('by_region')).toBeTruthy();
    expect(screen.getByText('Regional sales')).toBeTruthy();
    expect(screen.getByTestId('lineage-hidden').textContent).toContain('2 more asset(s) hidden by access policy');
    expect(calls.some((c) => c.path === '/embed/v1/graph' && c.url.searchParams.get('depth') === '2')).toBe(true);
    fireEvent.click(screen.getByLabelText('Open orders'));
    expect(at).toBe(`/asset/${encodeURIComponent(ORDERS)}`);
    // One hop at a time.
    fireEvent.click(screen.getByText('1 hop'));
    await screen.findByTestId('lineage-graph');
    expect(calls.some((c) => c.url.searchParams.get('depth') === '1')).toBe(true);
});

it('says when there is no lineage yet', async () => {
    const { client } = makeClient({
        'GET /ui/config': UI_CONFIG,
        'GET /embed/v1/graph': { nodes: [GRAPH.nodes[0]], edges: [], hidden_count: 0 },
    });
    render(
        <MemoryRouter>
            <CairnProvider client={client} cache={new QueryCache()}>
                <LineageGraphView urn={ROOT} />
            </CairnProvider>
        </MemoryRouter>,
    );
    expect(await screen.findByText(/No lineage recorded yet/)).toBeTruthy();
});

it('keeps the assets in the graph reachable by assistive technology', async () => {
    const { client } = makeClient({ 'GET /ui/config': UI_CONFIG, 'GET /embed/v1/graph': GRAPH });
    let at = '';
    render(
        <MemoryRouter initialEntries={['/asset/x']}>
            <CairnProvider client={client} cache={new QueryCache()}>
                <LineageGraphView urn={ROOT} />
                <Route path="*" render={({ location }) => { at = location.pathname; return null; }} />
            </CairnProvider>
        </MemoryRouter>,
    );
    const svg = await screen.findByTestId('lineage-graph');
    // role="img" makes everything inside it presentational, so the asset links would never be announced.
    expect(svg.getAttribute('role')).not.toBe('img');
    expect(screen.getByRole('group', { name: 'Lineage of by_region' })).toBe(svg);
    // Space opens a link-like node without also scrolling the page.
    const orders = screen.getByRole('link', { name: 'Open orders' });
    expect(fireEvent.keyDown(orders, { key: ' ' })).toBe(false);
    expect(at).toBe(`/asset/${encodeURIComponent(ORDERS)}`);
});

it('shows a quality check in error as failing, not as no checks', async () => {
    const graph: LineageGraph = {
        ...GRAPH,
        nodes: GRAPH.nodes.map((n) => (n.urn === ORDERS ? { ...n, quality_status: 'ERROR' } : n)),
    };
    const { client } = makeClient({ 'GET /ui/config': UI_CONFIG, 'GET /embed/v1/graph': graph });
    render(
        <MemoryRouter>
            <CairnProvider client={client} cache={new QueryCache()}>
                <LineageGraphView urn={ROOT} />
            </CairnProvider>
        </MemoryRouter>,
    );
    const svg = await screen.findByTestId('lineage-graph');
    const title = svg.querySelector(`[data-urn="${ORDERS}"] > title`);
    expect(title?.textContent).toMatch(/failing/);
    expect(title?.textContent).not.toMatch(/no quality checks/);
});
