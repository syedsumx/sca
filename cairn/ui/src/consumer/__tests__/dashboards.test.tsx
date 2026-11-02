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
