import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { CairnProvider } from '../context/CairnProvider';
import { MetricsPage } from '../pages/MetricsPage';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const REVENUE = { id: 'm1', name: 'revenue', label: 'Revenue', description: '', connection: 'lakehouse', table: 'sales.orders',
                  measure: 'Sum([amount])', filters: [{ column: 'status', op: '=', value: 'paid' }], time_column: 'ordered_at',
                  dimensions: ['region', 'country'], owner: 'urn:li:corpuser:ana', certified: true, version: 3, updated_ms: 1 };

it('lists metrics with their definitions, explores them by dimension and grain, and defines a new one', async () => {
    const calls: MockCall[] = [];
    const { client } = makeClient({
        'GET /ui/config': UI_CONFIG,
        'GET /data/sources': [{ connection: 'lakehouse', platform: 'iceberg', database: null }],
        'GET /sql/metrics': [REVENUE],
        'POST /sql/metrics/query': { columns: ['region', 'period', 'revenue'], rows: [['EU', '2026-01-01', 100], ['EU', '2026-02-01', 30]],
                                     masked_columns: [], truncated: false, elapsed_ms: 4, tables: [], metrics: [] },
        'POST /sql/metrics': { ...REVENUE, name: 'orders', label: 'Orders', measure: 'Count()', filters: [], certified: false, version: 1 },
    }, calls);
    render(<MemoryRouter><CairnProvider client={client} cache={new QueryCache()}><MetricsPage /></CairnProvider></MemoryRouter>);
    expect(await screen.findByText('certified')).toBeTruthy();
    expect(screen.getByText('Sum([amount]) where status = "paid"')).toBeTruthy();
    fireEvent.mouseDown(screen.getByRole('combobox', { name: 'Metrics to show' }));
    fireEvent.click((await screen.findAllByTitle('Revenue')).slice(-1)[0]);
    fireEvent.mouseDown(screen.getByRole('combobox', { name: 'Break down by' }));
    fireEvent.click((await screen.findAllByTitle('region')).slice(-1)[0]);
    fireEvent.mouseDown(screen.getByRole('combobox', { name: 'Time grain' }));
    fireEvent.click((await screen.findAllByTitle('month')).slice(-1)[0]);
    fireEvent.click(screen.getByRole('button', { name: 'Run' }));
    expect(await screen.findByRole('img', { name: /line chart of revenue by period/ })).toBeTruthy();
    expect(calls.find((c) => c.path === '/sql/metrics/query')!.body).toEqual({ metrics: ['revenue'], dimensions: ['region'], grain: 'month' });

    fireEvent.click(screen.getByRole('button', { name: 'New metric' }));
    fireEvent.change(await screen.findByLabelText('Metric name'), { target: { value: 'orders' } });
    fireEvent.change(screen.getByLabelText('Metric label'), { target: { value: 'Orders' } });
    fireEvent.change(screen.getByLabelText('Metric table'), { target: { value: 'sales.orders' } });
    fireEvent.change(screen.getByLabelText('Measure'), { target: { value: 'Count()' } });
    fireEvent.change(screen.getByLabelText('Time column'), { target: { value: 'ordered_at' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path === '/sql/metrics')).toBe(true));
    expect(calls.find((c) => c.method === 'POST' && c.path === '/sql/metrics')!.body).toMatchObject({
        name: 'orders', label: 'Orders', connection: 'lakehouse', table: 'sales.orders', measure: 'Count()', time_column: 'ordered_at', filters: [] });
});
