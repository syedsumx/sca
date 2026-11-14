import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { CairnProvider } from '../context/CairnProvider';
import { WorkbooksPage } from '../pages/WorkbooksPage';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

it('builds an element from formulas, grouping and filters, runs it as the viewer and saves it', async () => {
    const calls: MockCall[] = [];
    const { client } = makeClient({
        'GET /ui/config': UI_CONFIG,
        'GET /data/sources': [{ connection: 'lakehouse', platform: 'iceberg', database: null }],
        'GET /sql/schema': [{ urn: 'u1', name: 'sales.orders', columns: [{ name: 'region', type: 'varchar' }, { name: 'amount', type: 'double' }] }],
        'GET /sql/workbooks': [],
        'POST /sql/workbooks/run': { columns: ['region', 'Revenue'], rows: [['EU', 250], ['US', 130]], masked_columns: [],
                                     truncated: false, elapsed_ms: 9, tables: [], sql: 'SELECT "region", sum("Net") AS "Revenue" FROM …' },
        'POST /sql/workbooks': { id: 'w1', owner: 'urn:li:corpuser:ana', name: 'Revenue by region', description: '', connection: 'lakehouse',
                                 shared: true, spec: { elements: [] }, created_ms: 1, updated_ms: 1 },
    }, calls);
    render(<MemoryRouter><CairnProvider client={client} cache={new QueryCache()}><WorkbooksPage /></CairnProvider></MemoryRouter>);
    await waitFor(() => expect(calls.some((c) => c.path === '/sql/schema')).toBe(true));
    // Choose the table (antd Select: open it and click the option).
    fireEvent.mouseDown(screen.getByRole('combobox', { name: 'Source table' }));
    fireEvent.click(await screen.findByTitle('sales.orders'));
    fireEvent.click(screen.getByRole('button', { name: 'Add column' }));
    fireEvent.change(screen.getByLabelText('Columns name 1'), { target: { value: 'Net' } });
    fireEvent.change(screen.getByLabelText('Columns formula 1'), { target: { value: '[amount] * 0.9' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add aggregate' }));
    fireEvent.change(screen.getByLabelText('Aggregates name 1'), { target: { value: 'Revenue' } });
    fireEvent.change(screen.getByLabelText('Aggregates formula 1'), { target: { value: 'Sum([Net])' } });
    fireEvent.mouseDown(screen.getByRole('combobox', { name: 'Group by' }));
    fireEvent.click((await screen.findAllByTitle('region')).slice(-1)[0]);
    fireEvent.click(screen.getByRole('button', { name: 'Add filter' }));
    fireEvent.change(screen.getByLabelText('Filter value 1'), { target: { value: '10' } });
    fireEvent.click(screen.getByRole('button', { name: 'Run' }));
    expect(await screen.findByText('EU')).toBeTruthy();
    const run = calls.find((c) => c.path === '/sql/workbooks/run')!.body as { connection: string; element: Record<string, unknown> };
    expect(run.connection).toBe('lakehouse');
    expect(run.element).toMatchObject({
        source: { table: 'sales.orders' }, columns: [{ name: 'Net', formula: '[amount] * 0.9' }],
        aggregates: [{ name: 'Revenue', formula: 'Sum([Net])' }], group_by: ['region'],
        filters: [{ column: 'region', op: '=', value: 10, on: 'rows' }],
    });
    fireEvent.click(screen.getByText('SQL'));
    expect(await screen.findByText(/sum\("Net"\)/)).toBeTruthy();           // the SQL it ran
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    fireEvent.change(await screen.findByLabelText('Workbook name'), { target: { value: 'Revenue by region' } });
    fireEvent.click(screen.getByRole('checkbox', { name: /Share with everyone/ }));
    fireEvent.click(screen.getAllByRole('button', { name: 'Save' }).slice(-1)[0]);
    await waitFor(() => expect(calls.some((c) => c.path === '/sql/workbooks' && c.method === 'POST')).toBe(true));
    const saved = calls.find((c) => c.path === '/sql/workbooks' && c.method === 'POST')!.body as { name: string; shared: boolean; spec: { elements: unknown[] } };
    expect(saved).toMatchObject({ name: 'Revenue by region', shared: true, connection: 'lakehouse' });
    expect(saved.spec.elements).toHaveLength(1);
});
