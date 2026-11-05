import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import type { BiComment } from '../api/types';
import { BiAssetButtons } from '../components/BiAdmin';
import { BiFolders, BiUsageOverview } from '../components/BiCollab';
import { CairnProvider } from '../context/CairnProvider';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const wrap = (client: ReturnType<typeof makeClient>['client'], el: React.ReactElement) =>
    render(<MemoryRouter><CairnProvider client={client} cache={new QueryCache()}>{el}</CairnProvider></MemoryRouter>);
const ANA = 'urn:li:corpuser:ana';
const BOB = 'urn:li:corpuser:bob';
const assets = {
    'GET /sql/saved': [{ id: 'q1', name: 'Orders', owner: ANA }],
    'GET /sql/workbooks': [],
    'GET /sql/dashboards': [{ id: 'd1', name: 'Sales', owner: ANA }],
};

it('comments in threads, about a tile; authors edit theirs and the owner deletes any', async () => {
    const calls: MockCall[] = [];
    let list: BiComment[] = [
        { id: 'c1', kind: 'dashboard', asset_id: 'd1', tile: 1, parent_id: null, author: BOB, body: 'Why is US so high? @ana',
          deleted: false, created_ms: 1, edited_ms: null },
        { id: 'c2', kind: 'dashboard', asset_id: 'd1', tile: null, parent_id: 'c1', author: ANA, body: 'A large order',
          deleted: false, created_ms: 2, edited_ms: null },
    ];
    const { client } = makeClient({
        'GET /ui/config': UI_CONFIG,
        'GET /me': { urn: ANA, via: 'token' },
        'GET /sql/comments/dashboard/d1': () => list,
        'POST /sql/comments/dashboard/d1': (b: { body: string; tile?: number; parent?: string }) => {
            const c: BiComment = { id: `c${list.length + 1}`, kind: 'dashboard', asset_id: 'd1', tile: b.tile ?? null, parent_id: b.parent ?? null,
                        author: ANA, body: b.body, deleted: false, created_ms: 3, edited_ms: null };
            list = [...list, c];
            return c;
        },
        'PATCH /sql/comments/c2': (b: { body: string }) => {
            list = list.map((c) => (c.id === 'c2' ? { ...c, body: b.body, edited_ms: 4 } : c));
            return list[1];
        },
        'DELETE /sql/comments/c1': () => {
            list = list.map((c) => (c.id === 'c1' ? { ...c, body: null, deleted: true } : c));
            return null;
        },
    }, calls);
    wrap(client, <BiAssetButtons kind="dashboard" id="d1" name="Sales" role="OWNER" tiles={['By region', 'By month']} />);
    fireEvent.click(screen.getByRole('button', { name: 'Comments' }));
    expect(await screen.findByText('Why is US so high? @ana')).toBeTruthy();
    expect(screen.getByText('By month')).toBeTruthy();                       // the tile it is about
    // Ana replies to Bob's comment.
    fireEvent.click(screen.getAllByRole('button', { name: 'Reply' })[0]);
    fireEvent.change(screen.getByLabelText('Reply'), { target: { value: 'Checking' } });
    fireEvent.click(screen.getAllByRole('button', { name: 'Reply' }).at(-1) as HTMLElement);
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && (c.body as { parent: string }).parent === 'c1')).toBe(true));
    expect(await screen.findByText('Checking')).toBeTruthy();
    // She edits her own comment; Bob's she may only delete (she owns the dashboard).
    const comments = screen.getAllByTestId('comment');
    expect(within(comments[0]).getAllByRole('button', { name: 'Edit' })).toHaveLength(2);     // her two replies, not his
    fireEvent.click(within(comments[0]).getAllByRole('button', { name: 'Edit' })[0]);
    fireEvent.change(screen.getByLabelText('Edit comment'), { target: { value: 'A large order on 2 Nov' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findByText('A large order on 2 Nov')).toBeTruthy();
    fireEvent.click(screen.getAllByRole('button', { name: 'Delete' })[0]);
    fireEvent.click(await screen.findByRole('button', { name: 'OK' }));
    expect(await screen.findByText('(deleted)')).toBeTruthy();
    // A new comment about a tile.
    fireEvent.change(screen.getByLabelText('New comment'), { target: { value: 'EU looks low' } });
    fireEvent.mouseDown(screen.getByRole('combobox', { name: 'About tile' }));
    fireEvent.click(await screen.findByTitle('By region'));
    fireEvent.click(screen.getByRole('button', { name: 'Comment' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && (c.body as { tile: number }).tile === 0
        && (c.body as { body: string }).body === 'EU looks low')).toBe(true));
});

it('a viewer comments but sees no usage; an editor sees views by day', async () => {
    const { client } = makeClient({
        'GET /ui/config': UI_CONFIG,
        'GET /sql/usage/query/q1': { kind: 'query', id: 'q1', since: '2026-10-06', views: 3, viewers: 2,
            days: [{ day: '2026-11-04', views: 1, viewers: 1 }, { day: '2026-11-05', views: 2, viewers: 2 }] },
    });
    const v = wrap(client, <BiAssetButtons kind="query" id="q1" name="Orders" role="VIEWER" />);
    expect(screen.getByRole('button', { name: 'Comments' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Usage' })).toBeNull();
    v.unmount();
    wrap(client, <BiAssetButtons kind="query" id="q1" name="Orders" role="EDITOR" />);
    fireEvent.click(screen.getByRole('button', { name: 'Usage' }));
    expect((await screen.findByTestId('usage-total')).textContent).toMatch(/3 views by 2 people since 2026-10-06/);
    expect(screen.getByText('2026-11-05')).toBeTruthy();
});

it('makes folders, files what its owner may open, and shows what is shared', async () => {
    const calls: MockCall[] = [];
    let folders: { id: string; name: string; parent_id: string | null; owner: string; shared: boolean; created_ms: number }[] = [{ id: 'f1', name: 'Sales team', parent_id: null, owner: ANA, shared: true, created_ms: 1 },
                   { id: 'f9', name: 'Bob’s', parent_id: null, owner: BOB, shared: true, created_ms: 1 }];
    let items: { kind: string; id: string }[] = [{ kind: 'query', id: 'q1' }];
    const detail = (id: string) => ({ ...folders.find((f) => f.id === id), items: id === 'f1' ? items : [{ kind: 'dashboard', id: 'd1' }],
        folders: folders.filter((f) => f.parent_id === id) });
    const { client } = makeClient({
        'GET /ui/config': UI_CONFIG,
        'GET /me': { urn: ANA, via: 'token' },
        ...assets,
        'GET /sql/folders': () => folders,
        'GET /sql/folders/f1': () => detail('f1'),
        'GET /sql/folders/f9': () => detail('f9'),
        'GET /sql/folders/f2': () => detail('f2'),
        'POST /sql/folders': (b: { name: string; parent: string | null; shared: boolean }) => {
            const f = { id: 'f2', name: b.name, parent_id: b.parent, owner: ANA, shared: b.shared, created_ms: 2 };
            folders = [...folders, f];
            return f;
        },
        'PATCH /sql/folders/f1': (b: { name: string }) => { folders = folders.map((f) => (f.id === 'f1' ? { ...f, name: b.name } : f)); return folders[0]; },
        'PUT /sql/folders/f1/items/dashboard/d1': () => { items = [...items, { kind: 'dashboard', id: 'd1' }]; return detail('f1'); },
        'DELETE /sql/folders/f1/items/query/q1': () => { items = items.filter((i) => i.id !== 'q1'); return detail('f1'); },
    }, calls);
    wrap(client, <BiFolders />);
    fireEvent.click(await screen.findByText('Sales team'));
    const folder = await screen.findByTestId('folder-item');
    expect(within(folder).getByText('Orders')).toBeTruthy();
    // File the dashboard, then take the query out.
    fireEvent.mouseDown(screen.getByRole('combobox', { name: 'Add to folder' }));
    fireEvent.click(await screen.findByTitle('Sales (dashboard)'));
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT' && c.path === '/sql/folders/f1/items/dashboard/d1')).toBe(true));
    expect((await screen.findByRole('link', { name: 'Sales' })).getAttribute('href')).toBe('/dashboards?open=d1');
    fireEvent.click(screen.getByRole('button', { name: 'Take out Orders' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'DELETE' && c.path === '/sql/folders/f1/items/query/q1')).toBe(true));
    // She renames it.
    fireEvent.click(screen.getByRole('button', { name: 'Rename' }));
    fireEvent.change(await screen.findByLabelText('New name'), { target: { value: 'Sales' } });
    fireEvent.click(within(screen.getByRole('dialog', { name: 'Rename the folder' })).getByRole('button', { name: 'Rename' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'PATCH' && (c.body as { name: string }).name === 'Sales')).toBe(true));
    // A folder inside hers.
    fireEvent.click(screen.getByRole('button', { name: 'New folder inside' }));
    fireEvent.change(await screen.findByLabelText('Folder name'), { target: { value: 'Weekly' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && (c.body as { parent: string }).parent === 'f1'
        && (c.body as { name: string }).name === 'Weekly')).toBe(true));
    // Bob's shared folder: she sees what is in it, but cannot change it.
    fireEvent.click(await screen.findByText('Bob’s'));
    expect(await screen.findByText('shared by bob')).toBeTruthy();
    expect(screen.queryByRole('combobox', { name: 'Add to folder' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Delete' })).toBeNull();
});

it('lists the most viewed and the unopened', async () => {
    const { client } = makeClient({
        'GET /ui/config': UI_CONFIG,
        ...assets,
        'GET /sql/usage/popular': [{ kind: 'dashboard', id: 'd1', views: 3, viewers: 2 }],
        'GET /sql/usage/stale': [{ kind: 'query', id: 'q1', name: 'Orders', updated_ms: 1 }],
    });
    wrap(client, <BiUsageOverview />);
    const usage = await screen.findByTestId('bi-usage');
    expect(await within(usage).findByRole('link', { name: 'Sales' })).toBeTruthy();
    expect(within(usage).getByRole('link', { name: 'Orders' }).getAttribute('href')).toBe('/sql');
});
