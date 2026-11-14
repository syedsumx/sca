import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { CairnProvider } from '../context/CairnProvider';
import { DashboardsPage } from '../pages/DashboardsPage';
import { SqlAlertsView } from '../components/Operations';
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
    // Alerts on queries moved to Alerts & routing (L149): the same view, there.
    expect(screen.queryByRole('tab', { name: 'Alerts' })).toBeNull();
    render(
        <MemoryRouter>
            <CairnProvider client={client} cache={new QueryCache()}>
                <SqlAlertsView />
            </CairnProvider>
        </MemoryRouter>,
    );
    expect(await screen.findByText('Triggered')).toBeTruthy();
    expect(screen.getByText('(2)')).toBeTruthy();
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
    // With none open, the dashboards are cards (L150); one opens with a click.
    fireEvent.click(await screen.findByRole('button', { name: /^Revenue/ }));
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
    fireEvent.click(await waitFor(() => document.querySelector('.ant-select-item-option[title="Drill"]') as HTMLElement));
    fireEvent.click(await screen.findByText('EU: 150'));                 // the bar's tooltip, inside the bar
    await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true));
    expect(calls.filter((c) => c.method === 'POST').slice(-1)[0].body).toEqual({ selection: [{ column: 'region', value: 'EU', from: 0 }] });
    expect(await screen.findByText('region = EU')).toBeTruthy();
    fireEvent.click(await screen.findByRole('button', { name: 'Drill into EU' }));
    await waitFor(() => expect((calls.filter((c) => c.method === 'POST').slice(-1)[0].body as { drill?: unknown }).drill).toEqual({ 0: ['EU'] }));
});

it('builds a dashboard from a question, saves it and opens it, listing what was left out', async () => {
    const calls: MockCall[] = [];
    const tile = { metric: { metrics: ['revenue'], dimensions: ['region'] }, title: 'Revenue by region', width: 1,
                   chart: { type: 'bar', x: 'region', y: ['revenue'] } };
    const board = { id: 'd9', owner: 'urn:li:corpuser:ana', name: 'Sales overview', description: '', shared: false, updated_ms: 1,
                    controls: [], tiles: [tile] };
    const { client } = makeClient({
        'GET /ui/config': UI_CONFIG, 'GET /sql/saved': [], 'GET /sql/workbooks': [], 'GET /sql/metrics': [],
        'GET /sql/dashboards': [board],
        'GET /sql/dashboards/d9/render': { ...board, tiles: [{ ...tile, result: TILE_RESULT([['EU', 100], ['US', 70]], ['region', 'revenue']) }] },
        'POST /sql/builder': { title: 'Sales overview', tiles: [tile], controls: [], metrics: ['revenue'], tables: [],
                               refused: [{ error: 'metric profit is not one of the metrics listed' }], dashboard: board },
    }, calls);
    render(<MemoryRouter><CairnProvider client={client} cache={new QueryCache()}><DashboardsPage /></CairnProvider></MemoryRouter>);
    fireEvent.click(await screen.findByRole('button', { name: 'Build from a question' }));
    fireEvent.change(await screen.findByLabelText('Question'), { target: { value: 'Revenue by region and profit' } });
    fireEvent.click(screen.getByRole('button', { name: 'Build' }));
    await waitFor(() => expect(calls.some((c) => c.path === '/sql/builder')).toBe(true));
    expect(calls.find((c) => c.path === '/sql/builder')!.body).toEqual({ question: 'Revenue by region and profit', save: true });
    expect(await screen.findByText('metric profit is not one of the metrics listed')).toBeTruthy();
    expect(await screen.findByRole('img', { name: /bar chart of revenue by region/ })).toBeTruthy();   // opened
});

it('runs a form action and a row action, as the person clicking', async () => {
    const calls: MockCall[] = [];
    const tile = { element: { source: { table: 'sales.requests' } }, connection: 'lakehouse', title: 'Requests', width: 1,
                   chart: { type: 'table' } };
    const board = { id: 'd5', owner: 'urn:li:corpuser:ana', name: 'Requests', description: '', shared: true, updated_ms: 1, controls: [] };
    const rowId = 'a'.repeat(32);
    const { client } = makeClient({
        'GET /ui/config': UI_CONFIG, 'GET /sql/saved': [], 'GET /sql/workbooks': [], 'GET /sql/metrics': [],
        'GET /sql/dashboards': [{ ...board, tiles: [tile] }],
        'GET /sql/dashboards/d5/render': { ...board, tiles: [{ ...tile, result: TILE_RESULT(
            [[rowId, 'laptop', 'open', '2026-11-04 10:00:00+00:00']], ['_row_id', 'item', 'status', '_updated_at']) }] },
        'POST /sql/dashboards/d5/render': { ...board, tiles: [{ ...tile, result: TILE_RESULT(
            [[rowId, 'laptop', 'open', '2026-11-04 10:00:00+00:00']], ['_row_id', 'item', 'status', '_updated_at']) }] },
        'GET /sql/dashboards/d5/actions': [
            { id: 'a1', dashboard: 'd5', name: 'request', label: 'New request', kind: 'input_row', owner: board.owner,
              config: { fields: [{ name: 'item', label: 'Item', type: 'text', required: true }], confirm: '' } },
            { id: 'a2', dashboard: 'd5', name: 'approve', label: 'Approve', kind: 'input_update', owner: board.owner,
              config: { fields: [], confirm: '' } }],
        'POST /sql/dashboards/d5/actions/a1/run': { action: 'request', status: 'ok' },
        'POST /sql/dashboards/d5/actions/a2/run': { action: 'approve', status: 'ok' },
    }, calls);
    render(<MemoryRouter><CairnProvider client={client} cache={new QueryCache()}><DashboardsPage /></CairnProvider></MemoryRouter>);
    fireEvent.mouseDown((await screen.findAllByRole('combobox'))[0]);
    fireEvent.click(await screen.findByText('Requests (shared)'));
    fireEvent.click(await screen.findByRole('button', { name: 'New request' }));
    fireEvent.change(await screen.findByLabelText('Item'), { target: { value: 'monitor' } });
    fireEvent.click(screen.getAllByRole('button', { name: 'New request' }).slice(-1)[0]);
    await waitFor(() => expect(calls.some((c) => c.path === '/sql/dashboards/d5/actions/a1/run')).toBe(true));
    expect(calls.find((c) => c.path === '/sql/dashboards/d5/actions/a1/run')!.body).toEqual({ inputs: { item: 'monitor' } });
    // A row action waits for a row to be picked, and sends the row as read.
    const approve = await screen.findByRole('button', { name: 'Approve' });
    expect((approve as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(await screen.findByText('laptop'));
    await waitFor(() => expect((screen.getByRole('button', { name: 'Approve' }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
    await waitFor(() => expect(calls.some((c) => c.path === '/sql/dashboards/d5/actions/a2/run')).toBe(true));
    expect(calls.find((c) => c.path === '/sql/dashboards/d5/actions/a2/run')!.body).toMatchObject({
        inputs: {}, row: { _row_id: rowId, _updated_at: '2026-11-04 10:00:00+00:00', item: 'laptop' } });
});

it('schedules a dashboard export and sends it now, each recipient their own copy', async () => {
    const calls: MockCall[] = [];
    const board = { id: 'd7', owner: 'urn:li:corpuser:ana', name: 'Weekly orders', description: '', shared: true, updated_ms: 1,
                    controls: [], tiles: [] };
    const { client } = makeClient({
        'GET /ui/config': UI_CONFIG, 'GET /sql/saved': [], 'GET /sql/workbooks': [], 'GET /sql/metrics': [],
        'GET /sql/dashboards': [board], 'GET /sql/dashboards/d7/render': board, 'GET /sql/dashboards/d7/actions': [],
        'POST /sql/reports': { id: 'r1', owner: board.owner, name: 'Weekly orders', target_kind: 'dashboard', target_id: 'd7',
                               format: 'pdf', every_minutes: 1440, recipients: [board.owner, 'urn:li:corpuser:bob'], next_ms: 1,
                               last_ms: null, last_status: null, enabled: true },
        'POST /sql/reports/r1/run': { sent: [{ recipient: board.owner, file: 'f1' }], skipped: [{ recipient: 'urn:li:corpuser:bob', reason: 'no' }] },
    }, calls);
    render(<MemoryRouter><CairnProvider client={client} cache={new QueryCache()}><DashboardsPage /></CairnProvider></MemoryRouter>);
    fireEvent.mouseDown((await screen.findAllByRole('combobox'))[0]);
    fireEvent.click(await screen.findByText('Weekly orders (shared)'));
    fireEvent.click(await screen.findByRole('button', { name: 'Schedule export' }));
    fireEvent.mouseDown(await screen.findByRole('combobox', { name: 'Export format' }));
    fireEvent.click((await screen.findAllByTitle('PDF')).slice(-1)[0]);
    fireEvent.mouseDown(screen.getByRole('combobox', { name: 'How often' }));
    fireEvent.click((await screen.findAllByTitle('Every day')).slice(-1)[0]);
    const people = screen.getByRole('combobox', { name: 'Recipients' });
    fireEvent.change(people, { target: { value: 'bob' } });
    fireEvent.keyDown(people, { key: 'Enter', keyCode: 13 });
    fireEvent.click(screen.getByRole('button', { name: 'Schedule and send now' }));
    await waitFor(() => expect(calls.some((c) => c.path === '/sql/reports/r1/run')).toBe(true));
    expect(calls.find((c) => c.path === '/sql/reports' && c.method === 'POST')!.body).toEqual({
        name: 'Weekly orders', target: { dashboard: 'd7' }, format: 'pdf', every_minutes: 1440, recipients: ['urn:li:corpuser:bob'] });
});
