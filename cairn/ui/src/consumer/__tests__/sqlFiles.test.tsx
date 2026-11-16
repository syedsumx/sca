import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import type { SavedQuery } from '../api/types';
import { CairnProvider } from '../context/CairnProvider';
import { SqlWorkspacePage } from '../pages/SqlWorkspacePage';
import { installLayoutStubs, sqlOf, typeSql } from './codemirror';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);
beforeAll(installLayoutStubs);
beforeEach(() => window.localStorage.clear());

const ANA = 'urn:li:corpuser:ana';
const q = (id: string, name: string, sql: string, extra: Partial<SavedQuery> = {}): SavedQuery => ({
    id, owner: ANA, role: 'OWNER', name, description: '', connection: 'trino', sql, shared: false, chart: { type: 'table' },
    created_ms: 1, updated_ms: 1, ...extra,
});
const SAVED = [
    q('q1', 'Revenue', 'SELECT 1 AS revenue'),
    q('q2', 'Orders', 'SELECT 2 AS orders'),
    q('q3', 'Team KPIs', 'SELECT 3 AS kpi', { owner: 'urn:li:corpuser:bob', role: 'VIEWER', shared: true }),
];

function page(routes: Record<string, unknown>, calls: MockCall[]) {
    const { client } = makeClient({
        'GET /ui/config': UI_CONFIG,
        'GET /data/sources': [{ connection: 'trino', platform: 'trino', database: null }],
        'GET /sql/schema': [], 'GET /sql/history': [], 'GET /sql/saved': SAVED,
        'GET /sql/folders': [{ id: 'f1', name: 'Finance', parent_id: null, owner: ANA, shared: false, created_ms: 1 }],
        'GET /sql/folders/f1': { id: 'f1', name: 'Finance', parent_id: null, owner: ANA, shared: false, created_ms: 1,
            items: [{ kind: 'query', id: 'q1' }], folders: [] },
        'GET /v1/git-folders': [{ id: 'g1', owner: ANA, name: 'reports', repo: null, url: 'https://git.example/r.git',
            branch: 'main', path: 'reports', every_minutes: null, head: 'abc', synced_ms: 2, error: null, created_ms: 1 }],
        'GET /v1/git-folders/g1/files': [
            { file: 'orders.sql', kind: 'query', ref: 'q2', name: 'Orders', state: 'modified' },
            { file: 'old.sql', kind: 'query', ref: 'gone', name: 'old.sql', state: 'missing' }],
        ...routes,
    }, calls);
    return render(
        <MemoryRouter>
            <CairnProvider client={client} cache={new QueryCache()}>
                <SqlWorkspacePage />
            </CairnProvider>
        </MemoryRouter>,
    );
}

const files = () => screen.getByRole('region', { name: 'Files' });
const tabNames = () => screen.getAllByRole('tab').filter((t) => t.closest('.ant-tabs-card')).map((t) => t.textContent?.replace('×', ''));

it('opens files in tabs, saves as you type, and a draft into a folder (L163)', async () => {
    const calls: MockCall[] = [];
    page({
        'PUT /sql/saved/q1': (body: unknown) => ({ ...SAVED[0], ...(body as object) }),
        'POST /sql/saved': (body: unknown) => q('q9', (body as { name: string }).name, (body as { sql: string }).sql),
        'PUT /sql/folders/f1/items/query/q9': { id: 'f1', items: [], folders: [] },
    }, calls);
    await waitFor(() => expect(tabNames()).toEqual(['Untitled 1']));            // a fresh start: one draft
    fireEvent.click(await within(files()).findByText('Revenue'));
    await waitFor(() => expect(tabNames()).toEqual(['Untitled 1', 'Revenue']));
    expect(sqlOf(screen.getByLabelText('SQL'))).toBe('SELECT 1 AS revenue');
    fireEvent.click(within(files()).getByText('Revenue'));                       // open already: no second tab
    expect(tabNames()).toHaveLength(2);

    // Typing in a saved file: autosaved a moment later, marked as an autosave (its versions coalesce).
    typeSql(screen.getByLabelText('SQL'), 'SELECT 11 AS revenue');
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT' && c.path === '/sql/saved/q1')).toBe(true), { timeout: 4000 });
    const put = calls.find((c) => c.method === 'PUT' && c.path === '/sql/saved/q1')!.body as { sql: string; autosave: boolean };
    expect(put).toMatchObject({ sql: 'SELECT 11 AS revenue', autosave: true });
    await waitFor(() => expect(screen.queryByLabelText('unsaved')).toBeNull());

    // The draft: saved with a name, into the Finance folder; the tab becomes the file.
    fireEvent.click(screen.getAllByRole('tab', { name: /Untitled 1/ })[0]);
    typeSql(screen.getByLabelText('SQL'), 'SELECT 9 AS draft');
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'Draft nine' } });
    fireEvent.mouseDown(screen.getByRole('combobox', { name: 'Folder' }));
    fireEvent.click(await screen.findByTitle('Finance'));
    fireEvent.click(screen.getAllByRole('button', { name: 'Save' }).slice(-1)[0]);
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT' && c.path === '/sql/folders/f1/items/query/q9')).toBe(true));
    await waitFor(() => expect(tabNames()).toEqual(['Draft nine', 'Revenue']));
});

it('keeps a shared file read-only, closes a draft only when asked, and brings tabs back (L163)', async () => {
    const calls: MockCall[] = [];
    const view = page({}, calls);
    fireEvent.click(await within(files()).findByText('Team KPIs'));              // Shared with me
    expect(await screen.findByText(/read-only: you may run it, not change it/)).toBeTruthy();
    typeSql(screen.getByLabelText('SQL'), 'SELECT 33');
    await new Promise((r) => setTimeout(r, 1800));
    expect(calls.some((c) => c.method === 'PUT')).toBe(false);                 // never saved over Bob's query
    // A draft with text: closing asks first.
    fireEvent.click(screen.getAllByRole('tab', { name: /Untitled 1/ })[0]);
    typeSql(screen.getByLabelText('SQL'), 'SELECT 7');
    fireEvent.click(screen.getByRole('button', { name: 'Close tab' }));              // the open tab: the draft
    expect(await screen.findByText('Close Untitled 1?')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    // Reloaded: the same tabs, the draft's and the unsaved text back.
    await waitFor(() => expect(window.localStorage.getItem('cairn.sql.tabs:urn:li:corpuser:ana')).toContain('SELECT 7'));
    view.unmount();
    page({}, []);
    await waitFor(() => expect(tabNames()).toEqual(['Untitled 1●', 'Team KPIs●']));         // both still unsaved
});

it('shows folders and git folders with what changed since git (L163)', async () => {
    const calls: MockCall[] = [];
    page({ 'POST /v1/git-folders/g1/pull': { head: 'def', created: [], updated: ['orders.sql'], deleted: [], notebooks: [],
        conflicts: [], errors: [] } }, calls);
    const tree = await within(await screen.findByRole('region', { name: 'Files' })).findByRole('tree');
    // Folders open on demand: Finance holds Revenue.
    fireEvent.click(await within(tree).findByText('Finance'));
    await waitFor(() => expect(calls.some((c) => c.path === '/sql/folders/f1')).toBe(true));
    // Git: the files with their state.
    const gitNode = within(tree).getByRole('treeitem', { name: /^reports/ });
    fireEvent.click(await within(tree).findByText('reports'));
    expect(gitNode.getAttribute('aria-expanded')).toBe('true');
    expect(await within(tree).findByLabelText(/Changed in Cairn since the last pull/)).toBeTruthy();
    expect(within(tree).getByLabelText(/Deleted in Cairn; still in git/)).toBeTruthy();
    expect(within(tree).getByText('orders.sql')).toBeTruthy();
    // Pull from the folder's menu.
    fireEvent.click(within(gitNode as HTMLElement).getByRole('button', { name: 'More actions for reports' }));
    fireEvent.click(await screen.findByText('Pull'));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path === '/v1/git-folders/g1/pull')).toBe(true));
});

it('is an ARIA tree people can use from the keyboard, and tabs close without a button in the tab list (L163)', async () => {
    page({}, []);
    const tree = await within(await screen.findByRole('region', { name: 'Files' })).findByRole('tree', { name: 'Files' });
    await within(tree).findByText('Revenue');
    // A tree holds tree items and groups, nothing else; only tree items carry aria-selected or aria-expanded.
    const marked = Array.from(tree.querySelectorAll(':scope li[aria-selected], :scope li[aria-expanded], :scope div[aria-selected]'));
    expect(marked.length).toBeGreaterThan(0);
    for (const el of marked) expect(el.getAttribute('role')).toBe('treeitem');
    for (const child of Array.from(tree.children)) expect(child.getAttribute('role')).toBe('treeitem');
    const items = within(tree).getAllByRole('treeitem');
    expect(items.filter((i) => i.tabIndex === 0)).toHaveLength(1);                 // one tab stop
    const mine = within(tree).getByRole('treeitem', { name: /^My queries/ });
    expect(mine.getAttribute('aria-expanded')).toBe('true');
    mine.focus();
    fireEvent.keyDown(mine, { key: 'ArrowLeft' });                                // closes the section
    await waitFor(() => expect(mine.getAttribute('aria-expanded')).toBe('false'));
    fireEvent.keyDown(mine, { key: 'ArrowRight' });                               // opens it again
    await waitFor(() => expect(mine.getAttribute('aria-expanded')).toBe('true'));
    fireEvent.keyDown(mine, { key: 'ArrowRight' });                               // into its first file
    const revenue = within(tree).getByRole('treeitem', { name: /^Revenue/ });
    expect(document.activeElement).toBe(revenue);
    fireEvent.keyDown(revenue, { key: 'Enter' });                                 // opens it in a tab
    await waitFor(() => expect(tabNames()).toEqual(['Untitled 1', 'Revenue']));
    await waitFor(() => expect(revenue.getAttribute('aria-selected')).toBe('true'));
    // The tab list holds only tabs (and their wrappers): no buttons in it.
    for (const list of screen.getAllByRole('tablist')) expect(within(list).queryAllByRole('button')).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: 'Close tab' }));            // Revenue is saved: closes at once
    await waitFor(() => expect(tabNames()).toEqual(['Untitled 1']));
});
