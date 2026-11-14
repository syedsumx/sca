import { fireEvent, render, screen } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { CairnProvider } from '../context/CairnProvider';
import { DbtRuns } from '../components/DbtRuns';
import { MockCall, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const RUN = {
    invocation: 'inv-1',
    project: 'shop',
    environment: 'prod',
    command: 'build',
    status: 'error',
    actor: 'urn:li:corpuser:svc-dbt',
    started_ms: 1,
    finished_ms: 1_800_000_000_000,
    elapsed_s: 75,
    nodes: 3,
    errors: 0,
    tests_failed: 1,
    detail: [
        { node: 'model.shop.orders', status: 'success', seconds: 1.2 },
        { node: 'test.shop.not_null_orders_id', status: 'fail', seconds: 0.3 },
    ],
};

test('Runs › dbt runs lists dbt runs, their nodes and links to the project docs', async () => {
    const calls: MockCall[] = [];
    const { client } = makeClient({ 'GET /pipelines/dbt/runs': [RUN] }, calls);
    render(
        <MemoryRouter>
            <CairnProvider client={client} cache={new QueryCache()}>
                <DbtRuns />
            </CairnProvider>
        </MemoryRouter>,
    );
    expect((await screen.findByText('· 1 test(s) failed')).parentElement?.querySelector('[data-tone="error"]')).toBeTruthy();
    expect(screen.getByText('1m 15s')).toBeTruthy();
    expect(screen.getByText('svc-dbt')).toBeTruthy();
    // A run's nodes, one by one.
    fireEvent.click(screen.getAllByRole('button', { name: /expand row/i })[0]);
    expect(await screen.findByText('test.shop.not_null_orders_id')).toBeTruthy();
    expect(calls.some((c) => c.path === '/pipelines/dbt/runs')).toBe(true);
});
