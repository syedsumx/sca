import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { toCsv } from '../components/ResultChart';
import { CairnProvider } from '../context/CairnProvider';
import { SqlWorkspacePage } from '../pages/SqlWorkspacePage';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

it('browses, runs as the caller, keeps history, saves and shares', async () => {
    const calls: MockCall[] = [];
    const { client } = makeClient(
        {
            'GET /ui/config': UI_CONFIG,
            'GET /data/sources': [{ connection: 'trino', platform: 'trino', database: null }],
            'GET /sql/schema': [
                { urn: 'urn:li:dataset:(urn:li:dataPlatform:trino,lake.sales.orders,PROD)', name: 'lake.sales.orders',
                  columns: [{ name: 'region', type: 'varchar' }, { name: 'amount', type: 'double' }] },
            ],
            'GET /sql/history': [],
            'GET /sql/saved': [],
            'POST /sql/run': { columns: ['region', 'orders'], rows: [['EU', 2], ['US', 1]], masked_columns: ['region'],
                               truncated: false, elapsed_ms: 12, tables: [] },
            'POST /sql/saved': { id: 'q1', owner: 'urn:li:corpuser:ana', name: 'Orders by region', description: '',
                                 connection: 'trino', sql: 'x', shared: true, chart: { type: 'table' }, created_ms: 1, updated_ms: 1 },
        },
        calls,
    );
    render(
        <MemoryRouter>
            <CairnProvider client={client} cache={new QueryCache()}>
                <SqlWorkspacePage />
            </CairnProvider>
        </MemoryRouter>,
    );
    expect(await screen.findByText('lake.sales.orders')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('SQL'), {
        target: { value: 'SELECT region, count(*) AS orders FROM lake.sales.orders GROUP BY region' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Run' }));
    expect(await screen.findByText('EU')).toBeTruthy();
    expect(screen.getByText(/masked for you: region/)).toBeTruthy();
    const run = calls.find((c) => c.path === '/sql/run')!.body as { connection: string; sql: string };
    expect(run.connection).toBe('trino');
    await waitFor(() => expect(calls.filter((c) => c.path === '/sql/history').length).toBeGreaterThan(1));
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'Orders by region' } });
    fireEvent.click(screen.getByRole('checkbox'));
    fireEvent.click(screen.getAllByRole('button', { name: 'Save' }).slice(-1)[0]);
    await waitFor(() => expect(calls.some((c) => c.path === '/sql/saved' && c.method === 'POST')).toBe(true));
    const saved = calls.find((c) => c.path === '/sql/saved' && c.method === 'POST')!.body as { shared: boolean; name: string };
    expect(saved).toMatchObject({ shared: true, name: 'Orders by region' });
});

it('lists the system tables when they are set up, and inserts one on double-click', async () => {
    const { client } = makeClient({
        'GET /ui/config': UI_CONFIG,
        'GET /data/sources': [{ connection: 'trino', platform: 'trino', database: null }],
        'GET /sql/schema': [],
        'GET /sql/history': [],
        'GET /sql/saved': [],
        'GET /v1/system-tables': {
            catalog: 'cairn_system', interval_seconds: 60, freshness_seconds: 120,
            tables: [{ name: 'cairn_system.query.history', schema: 'query', table: 'history', retention_days: 90,
                       description: 'Queries Trino ran.', row_filtered: true, masked_columns: ['statement'],
                       columns: [{ name: 'user_name', type: 'string', description: 'The person.' },
                                 { name: 'statement', type: 'string', description: 'The SQL.' }] }],
        },
    });
    render(
        <MemoryRouter>
            <CairnProvider client={client} cache={new QueryCache()}>
                <SqlWorkspacePage />
            </CairnProvider>
        </MemoryRouter>,
    );
    const name = await screen.findByText('cairn_system.query.history');
    expect(screen.getByText('System tables')).toBeTruthy();
    expect(screen.getByText(/fresh within 2 min/)).toBeTruthy();
    fireEvent.doubleClick(name);
    expect((screen.getByLabelText('SQL') as HTMLTextAreaElement).value).toBe('cairn_system.query.history');
});

it('writes CSV with quoting', () => {
    expect(toCsv(['a', 'b'], [['x,y', 'he said "hi"'], [null, 2]])).toBe('a,b\r\n"x,y","he said ""hi"""\r\n,2');
});

it('suggests the tables and columns the person may query as they type, and takes one with the keyboard', async () => {
    const { client } = makeClient({
        'GET /ui/config': UI_CONFIG,
        'GET /data/sources': [{ connection: 'trino', platform: 'trino', database: null }],
        'GET /sql/schema': [
            { urn: 'u1', name: 'lake.sales.orders', columns: [{ name: 'region', type: 'varchar' }, { name: 'amount', type: 'double' }] },
            { urn: 'u2', name: 'lake.sales.customers', columns: [{ name: 'email', type: 'varchar' }] },
        ],
        'GET /sql/history': [],
        'GET /sql/saved': [],
    });
    render(
        <MemoryRouter>
            <CairnProvider client={client} cache={new QueryCache()}>
                <SqlWorkspacePage />
            </CairnProvider>
        </MemoryRouter>,
    );
    expect(await screen.findByText('lake.sales.orders')).toBeTruthy();
    const box = screen.getByLabelText('SQL') as HTMLTextAreaElement;
    const type = (value: string) => {
        box.setSelectionRange(value.length, value.length);
        fireEvent.change(box, { target: { value, selectionStart: value.length } });
    };
    type('SELECT * FROM lake.sales.o');
    const list = await screen.findByRole('listbox', { name: 'Suggestions' });
    expect(Array.from(list.querySelectorAll('[role=option]')).map((o) => o.firstChild?.textContent)).toEqual(['orders', 'customers']); // prefix first, then substring
    fireEvent.keyDown(box, { key: 'Enter' });
    expect(box.value).toBe('SELECT * FROM lake.sales.orders');
    expect(screen.queryByRole('listbox')).toBeNull();
    // Columns of the table read, after the select list is reopened with Ctrl+Space.
    fireEvent.change(box, { target: { value: 'SELECT  FROM lake.sales.orders' } });
    box.setSelectionRange(7, 7);
    fireEvent.keyDown(box, { key: ' ', ctrlKey: true });
    const cols = await screen.findByRole('listbox', { name: 'Suggestions' });
    expect(Array.from(cols.querySelectorAll('[role=option]')).map((o) => o.firstChild?.textContent)).toEqual(['amount', 'region']);
    fireEvent.keyDown(box, { key: 'ArrowDown' });
    fireEvent.keyDown(box, { key: 'Tab' });
    expect(box.value).toBe('SELECT region FROM lake.sales.orders');
    fireEvent.keyDown(box, { key: ' ', ctrlKey: true });
    fireEvent.keyDown(box, { key: 'Escape' });
    expect(screen.queryByRole('listbox')).toBeNull();
});
