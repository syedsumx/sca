import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { CairnProvider } from '../context/CairnProvider';
import { HUBS } from '../resources/hubs';
import { HubPage } from '../resources/HubPage';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

// L149: the old Pipelines page is gone; each of its tabs is a tab of its hub, with its richer parts kept.
it('shows pipes, streams, derived tables, warehouses and shares in their hubs, with what the Pipelines page added', async () => {
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
            'POST /v1/streams': { id: 's2' },
            'GET /v1/derived': [{ id: 'd1', name: 'by_region', sql: 'SELECT 1', target: 'sales.by_region', mode: 'full', watermark: null,
                                  key: [], every_minutes: null, on_change: true, upstreams: [], owner: 'urn:li:corpuser:ana',
                                  enabled: true, last_run_ms: null }],
            'POST /v1/derived/d1/refresh': { id: 'r1', kind: 'full', status: 'ok', trigger: 'manual', started_ms: 1, finished_ms: 2, error: null },
            'GET /maintenance/policies': [{ scope: '*', optimize: true, optimize_threshold: '128MB', expire_days: 7, orphan_days: null,
                                            every_hours: 24, enabled: true, updated_by: 'urn:li:corpuser:ana', updated_ms: 1 }],
            'POST /v1/warehouses/reporting/start': { name: 'reporting', starting: true },
            'GET /v1/warehouses': [{ name: 'finance', port: 8443, state: 'running', starts: 2, stops: 1, inflight: 1, idle_minutes: 15,
                                     idle_seconds: 4, queries_running: 3, queries_queued: 1,
                                     workers: { wanted: 2, ready: 1, min: 0, max: 4, scale_ups: 2, scale_downs: 0 } },
                                   { name: 'ops', port: 8444, state: 'unreachable', error: 'connection refused' },
                                   { name: 'reporting', port: 8445, state: 'stopped', starts: 1, stops: 1, inflight: 0, idle_minutes: 0,
                                     idle_seconds: 7200 }],
            'GET /v1/shares': { shares: [{ id: 'sh1', name: 'partner-a', recipient: 'Partner A', tables: ['analytics.products'],
                                           created_by: 'urn:li:corpuser:ana', created_at: 1, expires_at: 4102444800, revoked_at: null,
                                           revoked_by: null, last_used_at: null, state: 'active' }] },
            'POST /v1/shares': { id: 'sh2', name: 'partner-b', recipient: 'Partner B', tables: ['analytics.products', 'sales.daily'],
                                 created_by: 'urn:li:corpuser:ana', created_at: 1, expires_at: 2, revoked_at: null, revoked_by: null,
                                 last_used_at: null, state: 'active', credential: 'sh2:secret-once',
                                 connect: { uri: 'https://lake.example', credential: 'sh2:secret-once' } },
            'POST /v1/shares/sh1/revoke': {},
            'GET /v1/derived/d1/runs': [{ id: 'r1', kind: 'full', status: 'ok', trigger: 'manual', started_ms: 1, finished_ms: 2, error: null }],
            'GET /v1/streams/s1/rejects': [],
            'GET /maintenance/runs': [{ id: 'm1', table_name: 'raw.clicks', operation: 'optimize', status: 'ok', started_ms: 1,
                                        finished_ms: 2, detail: '' }],
        },
        calls,
    );
    const cache = new QueryCache();
    const hub = (route: string) => HUBS.find((h) => h.route === route)!;
    const open = (route: string, tab: string) => render(
        <MemoryRouter initialEntries={[`${route}?tab=${tab}`]}>
            <CairnProvider client={client} cache={cache}>
                <HubPage hub={hub(route)} />
            </CairnProvider>
        </MemoryRouter>,
    );

    // Ingestion › Pipes: the area's table and actions; a pipe's details add its files (what the Pipelines page showed).
    let view = open('/ingestion', 'pipes');
    fireEvent.click(await screen.findByRole('button', { name: 'orders-files' }));
    expect(await screen.findByText('the data has columns raw.orders does not: x')).toBeTruthy();
    view.unmount();

    // Ingestion › Streams: the Kafka form and enable/disable.
    view = open('/ingestion', 'streams');
    expect(await screen.findByText('customers-cdc')).toBeTruthy();
    fireEvent.click(screen.getByText('New stream'));
    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'accounts' } });
    fireEvent.change(screen.getByLabelText('Kafka topic'), { target: { value: 'pg.public.accounts' } });
    fireEvent.change(screen.getByLabelText('Key columns'), { target: { value: 'id, region' } });
    fireEvent.change(screen.getByLabelText('Into table'), { target: { value: 'cdc.accounts' } });
    fireEvent.click(screen.getByText('Create'));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path === '/v1/streams')).toBe(true));
    const sent = calls.find((c) => c.method === 'POST' && c.path === '/v1/streams')!.body as Record<string, unknown>;
    expect(sent).toMatchObject({ name: 'accounts', topic: 'pg.public.accounts', key: ['id', 'region'], target: 'cdc.accounts',
                                 encoding: 'json', mode: 'append', create_table: true });
    view.unmount();

    // Lakehouse › Derived tables: a table's details add its runs.
    view = open('/lakehouse', 'derived');
    fireEvent.click(await screen.findByRole('button', { name: 'by_region' }));
    expect(await screen.findByText('manual')).toBeTruthy();
    view.unmount();

    // Compute › Warehouses: the purpose-built view (state, workers, idle time, start now).
    view = open('/compute', 'warehouses');
    expect(await screen.findByText('finance')).toBeTruthy();
    expect(screen.getByText('running')).toBeTruthy();
    expect(screen.getByText('unreachable')).toBeTruthy();
    expect(screen.getByText('15 min idle')).toBeTruthy();
    expect(screen.getByText('never')).toBeTruthy();
    expect(screen.getByText('2.0 h')).toBeTruthy();
    expect(screen.getByText('2 / 1')).toBeTruthy();
    expect(screen.getByText('3 running, 1 queued')).toBeTruthy();
    expect(screen.getByText('1 of 2 (0–4)')).toBeTruthy();
    fireEvent.click(screen.getByText('Start now'));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path === '/v1/warehouses/reporting/start')).toBe(true));

    view.unmount();

    // External sharing › Shares: the purpose-built view (new share with its one-time credential, revoke).
    open('/sharing', 'shares');
    expect(await screen.findByText('partner-a')).toBeTruthy();
    fireEvent.click(screen.getByText('Revoke'));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path === '/v1/shares/sh1/revoke')).toBe(true));
    fireEvent.click(screen.getByText('New share'));
    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'partner-b' } });
    fireEvent.change(screen.getByLabelText('Recipient'), { target: { value: 'Partner B' } });
    fireEvent.change(screen.getByLabelText('Tables'), { target: { value: 'analytics.products, sales.daily' } });
    fireEvent.click(screen.getAllByText('Create').at(-1)!);
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path === '/v1/shares')).toBe(true));
    expect(calls.find((c) => c.method === 'POST' && c.path === '/v1/shares')!.body).toEqual({
        name: 'partner-b', recipient: 'Partner B', tables: ['analytics.products', 'sales.daily'], expires_days: 30,
    });
    expect(((await screen.findByLabelText('Connection settings')) as HTMLTextAreaElement).value).toContain('sh2:secret-once');
});
