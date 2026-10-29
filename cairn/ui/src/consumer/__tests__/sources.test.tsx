import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { SourcesSettings } from '../components/Sources';
import { CairnProvider } from '../context/CairnProvider';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const POSTGRES = {
    id: 'postgres',
    name: 'PostgreSQL',
    source_type: 'postgres',
    category: 'database',
    description: 'Schemas, tables and views from PostgreSQL.',
    fields: [
        {
            key: 'host_port',
            label: 'Host and port',
            kind: 'text',
            path: 'host_port',
            required: true,
            default: null,
            help: '',
            options: [],
        },
        {
            key: 'username',
            label: 'User',
            kind: 'text',
            path: 'username',
            required: true,
            default: null,
            help: '',
            options: [],
        },
        {
            key: 'password',
            label: 'Password secret',
            kind: 'secret',
            path: 'password',
            required: true,
            default: null,
            help: '',
            options: [],
        },
    ],
};
const FAILING = {
    urn: 'urn:li:dataHubIngestionSource:s1',
    name: 'Finance DB',
    type: 'postgres',
    connector: 'postgres',
    values: { host_port: 'finance-db:5432', username: 'reader', password: 'FINANCE_PG' },
    advanced: {},
    recipe: {},
    secrets: ['FINANCE_PG'],
    executor: 'finance',
    schedule: { interval: '0 2 * * *', timezone: 'UTC' },
    owners: [],
    created_by: null,
    last_run: {
        urn: 'urn:li:dataHubExecutionRequest:r1',
        id: 'r1',
        status: 'FAILURE',
        requested_ms: 1,
        executor: 'finance',
        started_ms: 1,
        duration_ms: 2000,
        events: 0,
        failures: 1,
        warnings: 0,
    },
    health: { state: 'failing', reason: 'the last run failed' },
};

function wrap(routes: Record<string, unknown>, calls: MockCall[] = []) {
    const { client } = makeClient({ 'GET /ui/config': UI_CONFIG, ...routes }, calls);
    render(
        <MemoryRouter>
            <CairnProvider client={client} cache={new QueryCache()}>
                <SourcesSettings />
            </CairnProvider>
        </MemoryRouter>,
    );
}

it('lists sources with their health and shows a run log', async () => {
    wrap({
        'GET /sources': [FAILING],
        'GET /sources/connectors': [POSTGRES],
        'GET /sources/urn:li:dataHubIngestionSource:s1/detail': { ...FAILING, runs: [FAILING.last_run] },
        'GET /sources/runs/urn:li:dataHubExecutionRequest:r1': {
            ...FAILING.last_run,
            log: 'connection refused by finance-db',
        },
    });
    expect(await screen.findByText('Finance DB')).toBeTruthy();
    expect(screen.getByText('failing')).toBeTruthy();
    expect(screen.getByText('1 source needs attention')).toBeTruthy();
    expect(screen.getByText('pool finance')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Runs' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Log' }));
    expect(await screen.findByText('connection refused by finance-db')).toBeTruthy();
});

it('adds a source from the gallery after testing its connection', async () => {
    const calls: MockCall[] = [];
    wrap(
        {
            'GET /sources': [],
            'GET /sources/connectors': [POSTGRES],
            'GET /sources/executors': [
                { id: 'default', name: 'Cairn (central executor)', online: null },
                { id: 'finance', name: 'Agent pool finance', online: 1 },
            ],
            'POST /sources/test': {
                id: 'task:t1',
                status: 'SUCCESS',
                connected: false,
                error: null,
                report: { basic_connectivity: { capable: false, failure_reason: 'password authentication failed' } },
            },
            'POST /sources': { ...FAILING, health: { state: 'never_run', reason: 'no run yet' } },
        },
        calls,
    );
    fireEvent.click(await screen.findByRole('button', { name: 'Add source' }));
    fireEvent.click(await screen.findByText('PostgreSQL'));
    fireEvent.change(await screen.findByLabelText('Source name'), { target: { value: 'Finance DB' } });
    fireEvent.change(screen.getByLabelText('Host and port'), { target: { value: 'finance-db:5432' } });
    fireEvent.change(screen.getByLabelText('User'), { target: { value: 'reader' } });
    fireEvent.change(screen.getByLabelText('Password secret'), { target: { value: 'FINANCE_PG' } });
    fireEvent.click(screen.getByRole('button', { name: 'Test connection' }));
    expect(await screen.findByText('password authentication failed')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Save source' }));
    await waitFor(() => expect(calls.find((c) => c.method === 'POST' && c.path === '/sources')).toBeTruthy());
    const body = calls.find((c) => c.method === 'POST' && c.path === '/sources')?.body as Record<string, unknown>;
    expect(body).toMatchObject({
        name: 'Finance DB',
        connector: 'postgres',
        values: { host_port: 'finance-db:5432', username: 'reader', password: 'FINANCE_PG' },
        schedule: { interval: '0 2 * * *', timezone: 'UTC' },
        executor: 'default',
    });
});
