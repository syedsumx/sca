import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { CairnProvider } from '../context/CairnProvider';
import { SqlWorkspacePage } from '../pages/SqlWorkspacePage';
import { bytes, columnStats, matching, shownColumns, toTsv } from '../sql/resultStats';
import { installLayoutStubs, typeSql } from './codemirror';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);
beforeAll(installLayoutStubs);
beforeEach(() => window.localStorage.clear());

describe('a result in the editor (L163)', () => {
    const cols = ['id', 'region', 'amount', 'day'];
    const rows = [[1, 'EU', 9.5, '2026-11-01'], [2, 'US', null, '2026-11-02'], [3, 'EU', 3, '2026-11-02'], [4, null, 7.5, null]];

    it('filters rows by any column, null finding empty cells, and finds columns by name', () => {
        expect(matching(cols, rows, { region: 'eu' })).toEqual([0, 2]);
        expect(matching(cols, rows, { region: 'eu', amount: '3' })).toEqual([2]);
        expect(matching(cols, rows, { amount: 'null' })).toEqual([1]);
        expect(matching(cols, rows, { nosuch: 'x', region: ' ' })).toEqual([0, 1, 2, 3]);
        expect(shownColumns(cols, 'AM')).toEqual(['amount']);
        expect(shownColumns(cols, '')).toEqual(cols);
    });

    it('summarises a column: kind, empties, distinct, range, mean and the most common values', () => {
        expect(columnStats('amount', 2, rows)).toEqual({ column: 'amount', kind: 'number', count: 4, nulls: 1, distinct: 3,
            min: 3, max: 9.5, mean: (9.5 + 3 + 7.5) / 3, top: [{ value: '3', count: 1 }, { value: '7.5', count: 1 }, { value: '9.5', count: 1 }] });
        const region = columnStats('region', 1, rows);
        expect(region).toMatchObject({ kind: 'text', nulls: 1, distinct: 2, min: 'EU', max: 'US' });
        expect(region.top[0]).toEqual({ value: 'EU', count: 2 });
        expect(columnStats('day', 3, rows)).toMatchObject({ kind: 'date', min: '2026-11-01', max: '2026-11-02' });
        const mixed = columnStats('x', 0, [[1], ['a']]);
        expect(mixed.kind).toBe('mixed');
        expect(mixed.min).toBeUndefined();
        expect(columnStats('x', 0, [[null]])).toMatchObject({ kind: 'empty', nulls: 1 });
    });

    it('copies as tab-separated text that keeps each value in its cell', () => {
        expect(toTsv(['a', 'b'], [[1, 'x\ty'], [null, 'two\nlines']])).toBe('a\tb\n1\tx y\n\ttwo lines');
        expect(bytes(1_234_567)).toBe('1.2 MB');
        expect(bytes(12)).toBe('12 B');
    });
});

function editor(routes: Record<string, unknown>, calls: MockCall[]) {
    const { client } = makeClient({
        'GET /ui/config': UI_CONFIG,
        'GET /data/sources': [{ connection: 'lakehouse', platform: 'trino', database: null }],
        'GET /sql/schema': [], 'GET /sql/history': [], 'GET /sql/saved': [], 'GET /sql/folders': [],
        ...routes,
    }, calls);
    render(
        <MemoryRouter>
            <CairnProvider client={client} cache={new QueryCache()}>
                <SqlWorkspacePage />
            </CairnProvider>
        </MemoryRouter>,
    );
}

const page = (rows: unknown[][], next: string | null) => ({
    id: 'q_1', state: next ? 'RUNNING' : 'FINISHED', next, columns: [{ name: 'id', type: 'bigint' }, { name: 'region', type: 'varchar' }],
    rows, masked_columns: ['region'], stats: { processedRows: 30, processedBytes: 2048, elapsedTimeMillis: 41, queuedTimeMillis: 2 },
});

it('runs on Trino page by page as the person, then filters, finds columns and shows the details (L163)', async () => {
    const calls: MockCall[] = [];
    let n = 0;
    editor({
        'GET /v1/statements/targets': { connections: ['lakehouse'], default: 'lakehouse', warehouses: ['finance'] },
        'POST /v1/statements': page([[1, 'EU'], [2, 'US']], 'c1'),
        'POST /v1/statements/next': () => (n++ === 0 ? page([[3, 'EU']], 'c2') : page([], null)),
        'POST /sql/explain': { plan: 'Fragment 0 [SINGLE]\n  Output[id, region]', dialect: 'trino', tables: [], elapsed_ms: 9,
            statement: "SELECT id, region FROM sales.orders WHERE region = 'EU'" },
    }, calls);
    // A warehouse to run on, offered because the connection runs page by page.
    const warehouse = await screen.findByRole('combobox', { name: 'Warehouse' });
    fireEvent.mouseDown(warehouse);
    fireEvent.click(await screen.findByTitle('warehouse finance'));
    typeSql(screen.getByLabelText('SQL'), 'SELECT id, region FROM sales.orders');
    fireEvent.click(screen.getByRole('button', { name: 'Run' }));
    const grid = await screen.findByTestId('result-grid');
    await waitFor(() => expect(within(grid).getByText('3 rows')).toBeTruthy());
    const start = calls.find((c) => c.path === '/v1/statements')!;
    expect(start.body).toEqual({ sql: 'SELECT id, region FROM sales.orders', connection: 'lakehouse', warehouse: 'finance',
        limit: 200, history: true });
    expect(calls.filter((c) => c.path === '/v1/statements/next').map((c) => c.body)).toEqual([{ cursor: 'c1' }, { cursor: 'c2' }]);
    expect(calls.some((c) => c.path === '/sql/run')).toBe(false);
    expect(within(grid).getByText('masked')).toBeTruthy();                            // the region column's header says so
    // Find a column; filter one.
    fireEvent.change(within(grid).getByLabelText('Find a column'), { target: { value: 'reg' } });
    expect(within(grid).getByText(/1 of 2 columns/)).toBeTruthy();
    fireEvent.change(within(grid).getByLabelText('Find a column'), { target: { value: '' } });
    const header = within(grid).getAllByRole('columnheader').find((h) => h.textContent?.startsWith('region'))!;
    fireEvent.click(header.querySelector('.ant-table-filter-trigger')!);
    fireEvent.change(await screen.findByLabelText('Filter region'), { target: { value: 'eu' } });
    await waitFor(() => expect(within(grid).getByText('2 of 3 rows match')).toBeTruthy());
    // Details: what Trino processed, the query id, the warehouse; and the plan as the person runs it.
    fireEvent.click(screen.getByRole('tab', { name: 'Details' }));
    const details = await screen.findByTestId('query-details');
    expect(within(details).getByText('2.0 KB')).toBeTruthy();
    expect(within(details).getByText('q_1')).toBeTruthy();
    expect(within(details).getByText('finance')).toBeTruthy();
    fireEvent.click(within(details).getByRole('button', { name: 'Explain' }));
    expect((await within(details).findByLabelText('Plan')).textContent).toContain('Fragment 0');
    expect(calls.find((c) => c.path === '/sql/explain')!.body).toEqual({ sql: 'SELECT id, region FROM sales.orders',
        connection: 'lakehouse' });
});

it('Stop cancels the statement in Trino, and a plan can be had without running (L163)', async () => {
    const calls: MockCall[] = [];
    let release: (v: unknown) => void = () => undefined;
    editor({
        'GET /v1/statements/targets': { connections: ['lakehouse'], default: 'lakehouse', warehouses: [] },
        'POST /v1/statements': page([[1, 'EU']], 'c1'),
        'POST /v1/statements/next': () => new Promise((r) => { release = r; }),
        'POST /v1/statements/cancel': { id: 'q_1', cancelled: true },
        'POST /sql/explain': { plan: 'Fragment 0 [SINGLE]', dialect: 'trino', tables: [], elapsed_ms: 3, statement: 'SELECT 1' },
    }, calls);
    await screen.findByText('Trino');
    expect(screen.queryByRole('combobox', { name: 'Warehouse' })).toBeNull();          // no warehouses: no picker
    typeSql(screen.getByLabelText('SQL'), 'SELECT id, region FROM sales.orders');
    fireEvent.click(screen.getByRole('button', { name: 'Run' }));
    expect(await screen.findByRole('status')).toBeTruthy();                               // rows arriving
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }));
    release(page([[2, 'US']], 'c2'));
    await waitFor(() => expect(calls.find((c) => c.path === '/v1/statements/cancel')?.body).toEqual({ cursor: 'c2' }));
    expect(await screen.findByText('Stopped before it finished.')).toBeTruthy();
    // Explain from the toolbar: the plan of the statement at the cursor, without running it.
    const before = calls.filter((c) => c.path === '/v1/statements').length;
    fireEvent.click(screen.getByRole('button', { name: 'Explain' }));
    const dialog = await screen.findByRole('dialog');
    expect((await within(dialog).findByLabelText('Plan')).textContent).toContain('Fragment 0');
    expect(calls.filter((c) => c.path === '/v1/statements')).toHaveLength(before);
});
