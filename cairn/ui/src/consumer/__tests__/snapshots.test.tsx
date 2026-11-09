import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import type { TableSnapshot } from '../api/types';
import { TableSnapshots } from '../components/Snapshots';
import { CairnProvider } from '../context/CairnProvider';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const BASE = '/v1/lakehouse/tables/sales/orders';
// 64-bit ids: the UI must use the string id, since these do not survive a JavaScript number.
const OLD = '4051833344726828130';
const NEW = '7265973765961580192';

it('lists snapshots, shows a past one, and restores it with a reason', async () => {
    const calls: MockCall[] = [];
    let snaps: TableSnapshot[] = [
        { id: OLD, snapshot: Number(OLD), parent: null, committed_at: '2026-11-04T10:00:00Z', operation: 'append',
          records: 3, added: 3, deleted: 0, current: false },
        { id: NEW, snapshot: Number(NEW), parent: Number(OLD), committed_at: '2026-11-05T09:00:00Z', operation: 'delete',
          records: 1, added: 0, deleted: 2, current: true },
    ];
    const { client } = makeClient({
        'GET /ui/config': UI_CONFIG,
        [`GET ${BASE}/snapshots`]: () => snaps,
        [`GET ${BASE}/snapshots/${OLD}/rows`]: { id: OLD, snapshot: 0, columns: ['id', 'region'],
                                                 rows: [[1, 'EU'], [2, 'US'], [3, 'EU']], truncated: false },
        [`POST ${BASE}/restore`]: (b: { snapshot: string; reason: string }) => {
            snaps = snaps.map((s) => ({ ...s, current: s.id === b.snapshot }));
            return { table: 'sales.orders', from_snapshot: 0, to_snapshot: 0, from_id: NEW, to_id: b.snapshot, records: 3 };
        },
    }, calls);
    render(<MemoryRouter><CairnProvider client={client} cache={new QueryCache()}><TableSnapshots table="sales.orders" /></CairnProvider></MemoryRouter>);
    expect(await screen.findByText(NEW)).toBeTruthy();
    expect(screen.getByText('current')).toBeTruthy();
    // The past snapshot's rows, as the person sees them.
    const oldRow = screen.getByText(OLD).closest('tr') as HTMLElement;
    fireEvent.click(within(oldRow).getByRole('button', { name: 'View rows' }));
    expect(await screen.findByText('3 row(s), as you see them')).toBeTruthy();
    expect(calls.some((c) => c.method === 'GET' && c.path.includes(`/snapshots/${OLD}/rows`))).toBe(true);
    fireEvent.click(within(screen.getByRole('dialog', { name: /sales\.orders at/ })).getByRole('button', { name: 'Close' }));
    // Restoring needs a reason; the exact id is sent.
    fireEvent.click(within(oldRow).getByRole('button', { name: 'Restore' }));
    // (by its title: in tests both dialogs get the same label id, so their accessible names collide)
    const dialog = (await screen.findByText('Restore sales.orders')).closest('[role="dialog"]') as HTMLElement;
    const ok = within(dialog).getByRole('button', { name: 'Restore', hidden: true });
    expect((ok as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(within(dialog).getByPlaceholderText('Why (kept in the audit log)'), { target: { value: 'deleted by mistake' } });
    fireEvent.click(ok);
    await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true));
    const post = calls.find((c) => c.method === 'POST') as MockCall;
    expect(post.body).toEqual({ snapshot: OLD, reason: 'deleted by mistake' });
    await waitFor(() => expect(within(screen.getByText(OLD).closest('tr') as HTMLElement).getByText('current')).toBeTruthy());
});

it('shows why a restore was refused', async () => {
    const { client } = makeClient({
        'GET /ui/config': UI_CONFIG,
        [`GET ${BASE}/snapshots`]: [
            { id: OLD, snapshot: 1, parent: null, committed_at: '2026-11-04T10:00:00Z', operation: 'append', records: 3,
              added: 3, deleted: 0, current: false },
            { id: NEW, snapshot: 2, parent: 1, committed_at: '2026-11-05T09:00:00Z', operation: 'delete', records: 1,
              added: 0, deleted: 2, current: true }],
        [`POST ${BASE}/restore`]: () => new Response(JSON.stringify({ detail: 'Trino refused the query: Access Denied' }), { status: 403 }),
    });
    render(<MemoryRouter><CairnProvider client={client} cache={new QueryCache()}><TableSnapshots table="sales.orders" /></CairnProvider></MemoryRouter>);
    fireEvent.click(within((await screen.findByText(OLD)).closest('tr') as HTMLElement).getByRole('button', { name: 'Restore' }));
    const dialog = await screen.findByRole('dialog', { name: 'Restore sales.orders' });
    fireEvent.change(within(dialog).getByLabelText('Reason'), { target: { value: 'try' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Restore' }));
    expect(await within(dialog).findByText('You don’t have permission for “Restore”')).toBeTruthy();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Details' }));
    expect(within(dialog).getByText(/Access Denied/)).toBeTruthy();
});
