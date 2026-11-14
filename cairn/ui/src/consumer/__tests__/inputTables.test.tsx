import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { CairnProvider } from '../context/CairnProvider';
import { InputTablesPage } from '../pages/InputTablesPage';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const TARGETS = {
    id: 't1', connection: 'lakehouse', table_name: 'sales.targets', description: 'Quarterly targets',
    columns: [{ name: 'region', type: 'text' }, { name: 'target', type: 'number' }, { name: 'approved', type: 'boolean' }],
    created_by: 'urn:li:corpuser:ana', created_ms: 1,
};
const ROWS = {
    columns: ['_row_id', 'region', 'target', 'approved', '_updated_by', '_updated_at'],
    rows: [['a'.repeat(32), 'EU', 1200.5, true, 'urn:li:corpuser:ana', '2026-11-04 10:00:00.123000+00:00']],
    masked_columns: [], truncated: false, elapsed_ms: 5, tables: [], table: TARGETS,
};

it('edits a cell with the row as read, adds rows and creates a table', async () => {
    const calls: MockCall[] = [];
    const { client } = makeClient({
        'GET /ui/config': UI_CONFIG,
        'GET /data/sources': [{ connection: 'lakehouse', platform: 'iceberg', database: null }],
        'GET /sql/input-tables': [TARGETS],
        [`GET /sql/input-tables/t1/rows`]: ROWS,
        [`PATCH /sql/input-tables/t1/rows/${'a'.repeat(32)}`]: { changed: 'a'.repeat(32) },
        'POST /sql/input-tables/t1/rows': { added: ['b'.repeat(32)] },
        'POST /sql/input-tables': { ...TARGETS, id: 't2', table_name: 'sales.plan' },
    }, calls);
    render(<MemoryRouter><CairnProvider client={client} cache={new QueryCache()}><InputTablesPage /></CairnProvider></MemoryRouter>);
    const cell = await screen.findByLabelText('target of row 1') as HTMLInputElement;
    expect(cell.value).toBe('1200.5');
    fireEvent.change(cell, { target: { value: '1300' } });
    fireEvent.click(await screen.findByRole('button', { name: 'Save row 1' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'PATCH')).toBe(true));
    // The edit carries the row's _updated_at as read, so a change made since is refused instead of overwritten.
    expect(calls.find((c) => c.method === 'PATCH')!.body).toEqual({
        values: { target: 1300 }, expected_updated_at: '2026-11-04 10:00:00.123000+00:00' });

    fireEvent.click(screen.getByRole('button', { name: 'Add row' }));
    fireEvent.change(screen.getByLabelText('New region 1'), { target: { value: 'US' } });
    fireEvent.change(screen.getByLabelText('New target 1'), { target: { value: '900' } });
    fireEvent.click(screen.getByLabelText('New approved 1'));
    fireEvent.click(screen.getByRole('button', { name: 'Save new rows' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path === '/sql/input-tables/t1/rows')).toBe(true));
    expect(calls.find((c) => c.method === 'POST' && c.path === '/sql/input-tables/t1/rows')!.body)
        .toEqual({ rows: [{ region: 'US', target: 900, approved: true }] });

    fireEvent.click(screen.getByRole('button', { name: 'New input table' }));
    fireEvent.change(await screen.findByLabelText('Table'), { target: { value: 'sales.plan' } });
    fireEvent.change(screen.getByLabelText('Column name 1'), { target: { value: 'owner' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add column' }));
    fireEvent.change(screen.getByLabelText('Column name 2'), { target: { value: 'budget' } });
    fireEvent.mouseDown(screen.getByRole('combobox', { name: 'Column type 2' }));
    fireEvent.click((await screen.findAllByTitle('number')).slice(-1)[0]);
    fireEvent.click(screen.getByRole('button', { name: 'Create' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path === '/sql/input-tables')).toBe(true));
    expect(calls.find((c) => c.method === 'POST' && c.path === '/sql/input-tables')!.body).toEqual({
        connection: 'lakehouse', table: 'sales.plan', description: '',
        columns: [{ name: 'owner', type: 'text' }, { name: 'budget', type: 'number' }] });
});
