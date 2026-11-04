import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { CairnProvider } from '../context/CairnProvider';
import { DashboardsPage } from '../pages/DashboardsPage';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const Q = { id: 'q1', owner: 'urn:li:corpuser:ana', name: 'Orders by region', description: '', connection: 'trino',
            sql: 'x', shared: true, chart: { type: 'bar', x: 'region', y: ['orders'] }, created_ms: 1, updated_ms: 1 };

it('shows a dashboard tile by tile, as the viewer, and manages alerts', async () => {
    const calls: MockCall[] = [];
    const { client } = makeClient(
        {
            'GET /ui/config': UI_CONFIG,
            'GET /sql/saved': [Q],
            'GET /sql/dashboards': [{ id: 'd1', owner: Q.owner, name: 'Sales', description: '', shared: true,
                                      tiles: [{ query: 'q1', title: 'Orders', width: 2 }], updated_ms: 1 }],
            'GET /sql/dashboards/d1/render': { id: 'd1', owner: Q.owner, name: 'Sales', description: '', shared: true, updated_ms: 1,
                tiles: [{ query: 'q1', title: 'Orders', width: 2, chart: Q.chart,
                          result: { columns: ['region', 'orders'], rows: [['EU', 2], ['US', 1]], masked_columns: [],
                                    truncated: false, elapsed_ms: 3, tables: [] } },
                        { query: 'q2', title: 'Salaries', width: 1, error: 'you may not query hr.salaries' }] },
            'GET /sql/alerts': [{ id: 'a1', name: 'Big orders', query: 'q1', measure: 'rows', op: '>', value: 1, every_minutes: 60,
                                  recipients: [], state: 'triggered', last_value: 2, last_error: null, last_checked_ms: 1,
                                  enabled: true }],
            'POST /sql/alerts/a1/check': {},
        },
        calls,
    );
    render(
        <MemoryRouter>
            <CairnProvider client={client} cache={new QueryCache()}>
                <DashboardsPage />
            </CairnProvider>
        </MemoryRouter>,
    );
    fireEvent.mouseDown((await screen.findAllByRole('combobox'))[0]);
    fireEvent.click(await screen.findByText('Sales (shared)'));
    expect(await screen.findByRole('img', { name: /bar chart of orders by region/ })).toBeTruthy();
    expect(screen.getByText('you may not query hr.salaries')).toBeTruthy();
    fireEvent.click(screen.getByRole('tab', { name: 'Alerts' }));
    expect(await screen.findByText('triggered (2)')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Check now' }));
    await waitFor(() => expect(calls.some((c) => c.path === '/sql/alerts/a1/check')).toBe(true));
});

const TILE_RESULT = (rows: unknown[][], columns = ['region', 'Revenue']) => ({ columns, rows, masked_columns: [], truncated: false, elapsed_ms: 2, tables: [] });

it('filters tiles by a control, cross-filters on a click and drills down', async () => {
    const calls: MockCall[] = [];
    const board = { id: 'd2', owner: 'urn:li:corpuser:ana', name: 'Revenue', description: '', shared: false, updated_ms: 1,
                    controls: [{ id: 'region', label: 'Region', column: 'region', kind: 'values', targets: null,
                                 source: { connection: 'lakehouse', table: 'sales.orders' }, default: null }] };
    const byRegion = { element: { source: { table: 'sales.orders' }, group_by: ['region'] }, connection: 'lakehouse', title: 'By region',
                       width: 1, drill: ['region', 'country'], chart: { type: 'bar', x: 'region', y: ['Revenue'] } };
    const { client } = makeClient({
        'GET /ui/config': UI_CONFIG,
        'GET /sql/saved': [],
        'GET /sql/workbooks': [],
        'GET /sql/dashboards': [{ ...board, tiles: [byRegion] }],
        'GET /sql/dashboards/d2/render': { ...board, tiles: [{ ...byRegion, result: TILE_RESULT([['EU', 150], ['US', 80]]), applied: [],
                                                                level: { path: [], column: 'region' } }] },
        'GET /sql/dashboards/d2/controls/region/values': ['EU', 'US'],
        'POST /sql/dashboards/d2/render': { ...board, tiles: [{ ...byRegion, chart: { type: 'bar', x: 'country', y: ['Revenue'] },
                                                                 result: TILE_RESULT([['DE', 130], ['FR', 50]], ['country', 'Revenue']),
                                                                 applied: [{ column: 'region', op: 'in', values: ['EU'] }],
                                                                 level: { path: ['EU'], column: 'country' } }] },
    }, calls);
    render(<MemoryRouter><CairnProvider client={client} cache={new QueryCache()}><DashboardsPage /></CairnProvider></MemoryRouter>);
    fireEvent.mouseDown((await screen.findAllByRole('combobox'))[0]);
    fireEvent.click(await screen.findByText('Revenue'));
    expect(await screen.findByRole('img', { name: /bar chart of Revenue by region/ })).toBeTruthy();
    // The control's choices come from the data, as the viewer sees it.
    fireEvent.mouseDown(screen.getByRole('combobox', { name: 'Region' }));
    fireEvent.click(await screen.findByTitle('EU'));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true));
    expect(calls.filter((c) => c.method === 'POST').slice(-1)[0].body).toEqual({ controls: { region: ['EU'] } });
    expect(await screen.findByText('Filtered by region')).toBeTruthy();
});

it('a click on a bar selects its value, and a drillable tile drills into it', async () => {
    const calls: MockCall[] = [];
    const byRegion = { element: { source: { table: 'sales.orders' }, group_by: ['region'] }, connection: 'lakehouse', title: 'By region',
                       width: 1, drill: ['region', 'country'], chart: { type: 'bar', x: 'region', y: ['Revenue'] } };
    const board = { id: 'd3', owner: 'urn:li:corpuser:ana', name: 'Drill', description: '', shared: false, updated_ms: 1, controls: [] };
    const top = { ...board, tiles: [{ ...byRegion, result: TILE_RESULT([['EU', 150], ['US', 80]]), level: { path: [], column: 'region' } }] };
    const { client } = makeClient({
        'GET /ui/config': UI_CONFIG, 'GET /sql/saved': [], 'GET /sql/workbooks': [],
        'GET /sql/dashboards': [{ ...board, tiles: [byRegion] }],
        'GET /sql/dashboards/d3/render': top,
        'POST /sql/dashboards/d3/render': top,
    }, calls);
    render(<MemoryRouter><CairnProvider client={client} cache={new QueryCache()}><DashboardsPage /></CairnProvider></MemoryRouter>);
    fireEvent.mouseDown((await screen.findAllByRole('combobox'))[0]);
    fireEvent.click(await screen.findByText('Drill'));
    fireEvent.click(await screen.findByText('EU: 150'));                 // the bar's tooltip, inside the bar
    await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true));
    expect(calls.filter((c) => c.method === 'POST').slice(-1)[0].body).toEqual({ selection: [{ column: 'region', value: 'EU', from: 0 }] });
    expect(await screen.findByText('region = EU')).toBeTruthy();
    fireEvent.click(await screen.findByRole('button', { name: 'Drill into EU' }));
    await waitFor(() => expect((calls.filter((c) => c.method === 'POST').slice(-1)[0].body as { drill?: unknown }).drill).toEqual({ 0: ['EU'] }));
});
