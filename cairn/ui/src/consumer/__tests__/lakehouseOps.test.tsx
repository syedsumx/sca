import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { CairnProvider } from '../context/CairnProvider';
import { PipelinesPage } from '../pages/PipelinesPage';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

it('shows pipes, streams, derived tables and maintenance next to dbt runs', async () => {
    const calls: MockCall[] = [];
    const { client } = makeClient(
        {
            'GET /ui/config': UI_CONFIG,
            'GET /pipelines/dbt/runs': [],
            'GET /v1/pipes': [{ id: 'p1', name: 'orders-files', bucket: 'landing', prefix: 'orders/', format: 'csv', target: 'raw.orders',
                                create_table: true, enabled: true, created_by: 'urn:li:corpuser:ana', created_ms: 1 }],
            'GET /v1/pipes/p1/files': [{ pipe_id: 'p1', key: 'orders/bad.csv', version: '1-1', status: 'error', rows: null,
                                         error: 'the data has columns raw.orders does not: x', loaded_ms: 1 }],
            'POST /v1/pipes/p1/sweep': [],
            'GET /v1/streams': [{ id: 's1', name: 'customers-cdc', topic: 'pg.public.customers', format: 'debezium', mode: 'upsert',
                                  key: ['id'], target: 'cdc.customers', source: null, enabled: true,
                                  status: { status: 'running', rows: 1200, lag: 3, last_commit_ms: 1, error: null, updated_ms: 1 } }],
            'POST /v1/streams/s1/enabled': {},
            'GET /v1/derived': [{ id: 'd1', name: 'by_region', sql: 'SELECT 1', target: 'sales.by_region', mode: 'full', watermark: null,
                                  key: [], every_minutes: null, on_change: true, upstreams: [], owner: 'urn:li:corpuser:ana',
                                  enabled: true, last_run_ms: null }],
            'POST /v1/derived/d1/refresh': { id: 'r1', kind: 'full', status: 'ok', trigger: 'manual', started_ms: 1, finished_ms: 2, error: null },
            'GET /maintenance/policies': [{ scope: '*', optimize: true, optimize_threshold: '128MB', expire_days: 7, orphan_days: null,
                                            every_hours: 24, enabled: true, updated_by: 'urn:li:corpuser:ana', updated_ms: 1 }],
            'GET /maintenance/runs': [{ id: 'm1', table_name: 'raw.clicks', operation: 'optimize', status: 'ok', started_ms: 1,
                                        finished_ms: 2, detail: '' }],
        },
        calls,
    );
    render(
        <MemoryRouter>
            <CairnProvider client={client} cache={new QueryCache()}>
                <PipelinesPage />
            </CairnProvider>
        </MemoryRouter>,
    );
    fireEvent.click(await screen.findByRole('tab', { name: 'Pipes' }));
    expect(await screen.findByText('orders-files')).toBeTruthy();
    expect(screen.getByText('s3://landing/orders/')).toBeTruthy();
    fireEvent.click(screen.getByText('Sweep now'));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path === '/v1/pipes/p1/sweep')).toBe(true));

    fireEvent.click(screen.getByRole('tab', { name: 'Streams' }));
    expect(await screen.findByText('customers-cdc')).toBeTruthy();
    expect(screen.getByText('debezium, upsert on id')).toBeTruthy();
    expect(screen.getByText('1,200')).toBeTruthy();
    fireEvent.click(screen.getByText('Pause'));
    await waitFor(() => expect(calls.some((c) => c.path === '/v1/streams/s1/enabled' && (c.body as { enabled: boolean }).enabled === false)).toBe(true));

    fireEvent.click(screen.getByRole('tab', { name: 'Derived tables' }));
    expect(await screen.findByText('sales.by_region')).toBeTruthy();
    expect(screen.getByText('full · when upstream changes')).toBeTruthy();
    fireEvent.click(screen.getByText('Rebuild'));
    await waitFor(() => expect(calls.some((c) => c.path === '/v1/derived/d1/refresh' && (c.body as { full: boolean }).full)).toBe(true));

    fireEvent.click(screen.getByRole('tab', { name: 'Maintenance' }));
    expect(await screen.findByText('files under 128MB')).toBeTruthy();
    expect(screen.getByText('7 days')).toBeTruthy();
    expect(screen.getByText('off')).toBeTruthy();
    expect(await screen.findByText('raw.clicks')).toBeTruthy();
});
