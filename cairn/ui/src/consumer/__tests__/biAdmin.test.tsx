import { vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { BiAssetButtons, BiImpactPanel } from '../components/BiAdmin';
import { CairnProvider } from '../context/CairnProvider';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const wrap = (client: ReturnType<typeof makeClient>['client'], el: React.ReactElement) =>
    render(<MemoryRouter><CairnProvider client={client} cache={new QueryCache()}>{el}</CairnProvider></MemoryRouter>);

it('shares an asset with a person or a group, and stops sharing it', async () => {
    const calls: MockCall[] = [];
    let shares = [{ principal: 'urn:li:corpGroup:finance', role: 'VIEWER', granted_by: 'urn:li:corpuser:ana', granted_ms: 1 }];
    const { client } = makeClient({
        'GET /ui/config': UI_CONFIG,
        'GET /sql/shares/dashboard/d1': () => ({ kind: 'dashboard', id: 'd1', owner: 'urn:li:corpuser:ana', shares }),
        'PUT /sql/shares/dashboard/d1': (body: { principal: string; role: string | null }) => {
            shares = body.role ? [...shares.filter((s) => s.principal !== body.principal),
                { principal: body.principal, role: body.role, granted_by: 'urn:li:corpuser:ana', granted_ms: 2 }]
                : shares.filter((s) => s.principal !== body.principal);
            return { kind: 'dashboard', id: 'd1', owner: 'urn:li:corpuser:ana', shares };
        },
    }, calls);
    wrap(client, <BiAssetButtons kind="dashboard" id="d1" name="Sales" role="OWNER" />);
    fireEvent.click(screen.getByRole('button', { name: 'Permissions' }));   // "share with people" is Permissions (L149)
    expect(await screen.findByText('group:finance')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('Share with'), { target: { value: 'bob' } });
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Add' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT' && (c.body as { principal: string }).principal === 'urn:li:corpuser:bob'
        && (c.body as { role: string }).role === 'VIEWER')).toBe(true));
    expect(await screen.findByText('bob')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Stop sharing with group:finance' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT' && (c.body as { role: null }).role === null)).toBe(true));
});

it('only owners and editors see Share; viewers see the history but cannot restore', async () => {
    const { client } = makeClient({
        'GET /ui/config': UI_CONFIG,
        'GET /sql/versions/query/q1': [
            { version: 2, name: 'Revenue', saved_by: 'urn:li:corpuser:ana', saved_ms: 2 },
            { version: 1, name: 'Revenue', saved_by: 'urn:li:corpuser:ana', saved_ms: 1 }],
        'GET /sql/versions/query/q1/diff': { a: 1, b: 2, changed: ['sql'], diff: '--- version 1\n+++ version 2\n-  "sql": "SELECT 1"\n+  "sql": "SELECT 2"' },
    });
    wrap(client, <BiAssetButtons kind="query" id="q1" name="Revenue" role="VIEWER" />);
    expect(screen.queryByRole('button', { name: 'Permissions' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'History' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Changes' }));
    const diff = await screen.findByTestId('version-diff');
    expect(within(diff).getByText(/Version 1 → 2: sql/)).toBeTruthy();
    expect(within(diff).getByText(/^\+.*SELECT 2/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Restore version 1' })).toBeNull();
});

it('an editor restores an earlier version', async () => {
    const calls: MockCall[] = [];
    const restored = vi.fn();
    const { client } = makeClient({
        'GET /ui/config': UI_CONFIG,
        'GET /sql/versions/workbook/w1': [
            { version: 2, name: 'Orders (broken)', saved_by: 'urn:li:corpuser:bob', saved_ms: 2 },
            { version: 1, name: 'Orders', saved_by: 'urn:li:corpuser:ana', saved_ms: 1 }],
        'POST /sql/versions/workbook/w1/1/restore': { restored: 1 },
    }, calls);
    wrap(client, <BiAssetButtons kind="workbook" id="w1" name="Orders" role="EDITOR" onRestored={restored} />);
    fireEvent.click(screen.getByRole('button', { name: 'History' }));
    expect(screen.queryByRole('button', { name: 'Restore version 2' })).toBeNull();          // the latest is current
    fireEvent.click(await screen.findByRole('button', { name: 'Restore version 1' }));
    await waitFor(() => expect(calls.some((c) => c.path === '/sql/versions/workbook/w1/1/restore')).toBe(true));
    await waitFor(() => expect(restored).toHaveBeenCalled());
});

it('shows what would break if a column of a dataset went away', async () => {
    const calls: MockCall[] = [];
    const ds = 'urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.orders,PROD)';
    const { client } = makeClient({
        'GET /ui/config': UI_CONFIG,
        'GET /sql/impact': (_: unknown, url: URL) => (url.searchParams.get('column') === 'amount'
            ? { dataset: ds, column: 'amount', others: [{ owner: 'urn:li:corpuser:bob', assets: 2 }], assets: [
                { kind: 'query', id: 'q1', name: 'Revenue', owner: 'urn:li:corpuser:ana', columns: ['amount'], through_queries: [] },
                { kind: 'dashboard', id: 'd1', name: 'Sales', owner: 'urn:li:corpuser:ana', columns: [], through_queries: ['q1'] }] }
            : { dataset: ds, column: null, others: [], assets: [] }),
    }, calls);
    wrap(client, <BiImpactPanel dataset={ds} columns={['region', 'amount']} />);
    expect(await screen.findByText(/No saved query, workbook or dashboard you can see would break/)).toBeTruthy();
    fireEvent.mouseDown(screen.getByRole('combobox', { name: 'Column' }));
    fireEvent.click(await screen.findByTitle('amount'));
    expect(await screen.findByText('Revenue')).toBeTruthy();
    expect(screen.getByText('through 1 saved query')).toBeTruthy();
    expect(screen.getByText(/2 more you cannot open, owned by bob/)).toBeTruthy();
    expect(calls.some((c) => c.url.searchParams.get('dataset') === ds && c.url.searchParams.get('column') === 'amount')).toBe(true);
});
