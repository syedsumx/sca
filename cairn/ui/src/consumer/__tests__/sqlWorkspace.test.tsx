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

it('writes CSV with quoting', () => {
    expect(toCsv(['a', 'b'], [['x,y', 'he said "hi"'], [null, 2]])).toBe('a,b\r\n"x,y","he said ""hi"""\r\n,2');
});
