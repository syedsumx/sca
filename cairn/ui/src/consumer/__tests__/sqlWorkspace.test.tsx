import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { toCsv } from '../components/ResultChart';
import { CairnProvider } from '../context/CairnProvider';
import { SqlWorkspacePage } from '../pages/SqlWorkspacePage';
import { currentCompletions, acceptCompletion, moveCompletionSelection, startCompletion } from '@codemirror/autocomplete';

import { editorOf, installLayoutStubs, selectSql, sqlOf, typeSql } from './codemirror';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);
beforeAll(installLayoutStubs);

it('browses, runs as the caller, keeps history, saves and shares', async () => {
    const calls: MockCall[] = [];
    const { client } = makeClient(
        {
            'GET /ui/config': UI_CONFIG,
            'GET /data/sources': [{ connection: 'trino', platform: 'trino', database: null }],
            'GET /sql/schema': [
                { urn: 'urn:li:dataset:(urn:li:dataPlatform:trino,lake.sales.orders,PROD)', name: 'lake.sales.orders',
                  columns: [{ name: 'region', type: 'varchar' }, { name: 'amount', type: 'double' }] },
            ],
            'GET /sql/history': [],
            'GET /sql/saved': [],
            'POST /sql/run': { columns: ['region', 'orders'], rows: [['EU', 2], ['US', 1]], masked_columns: ['region'],
                               truncated: false, elapsed_ms: 12, tables: [] },
            'POST /sql/saved': { id: 'q1', owner: 'urn:li:corpuser:ana', name: 'Orders by region', description: '',
                                 connection: 'trino', sql: 'x', shared: true, chart: { type: 'table' }, created_ms: 1, updated_ms: 1 },
        },
        calls,
    );
    render(
        <MemoryRouter>
            <CairnProvider client={client} cache={new QueryCache()}>
                <SqlWorkspacePage />
            </CairnProvider>
        </MemoryRouter>,
    );
    expect(await screen.findByText('lake.sales.orders')).toBeTruthy();
    typeSql(screen.getByLabelText('SQL'), 'SELECT region, count(*) AS orders FROM lake.sales.orders GROUP BY region');
    fireEvent.click(screen.getByRole('button', { name: 'Run' }));
    expect(await screen.findByText('EU')).toBeTruthy();
    expect(screen.getByText(/masked for you: region/)).toBeTruthy();
    const run = calls.find((c) => c.path === '/sql/run')!.body as { connection: string; sql: string };
    expect(run.connection).toBe('trino');
    await waitFor(() => expect(calls.filter((c) => c.path === '/sql/history').length).toBeGreaterThan(1));
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'Orders by region' } });
    fireEvent.click(screen.getByRole('checkbox'));
    fireEvent.click(screen.getAllByRole('button', { name: 'Save' }).slice(-1)[0]);
    await waitFor(() => expect(calls.some((c) => c.path === '/sql/saved' && c.method === 'POST')).toBe(true));
    const saved = calls.find((c) => c.path === '/sql/saved' && c.method === 'POST')!.body as { shared: boolean; name: string };
    expect(saved).toMatchObject({ shared: true, name: 'Orders by region' });
});

it('lists the system tables when they are set up, and inserts one on double-click', async () => {
    const { client } = makeClient({
        'GET /ui/config': UI_CONFIG,
        'GET /data/sources': [{ connection: 'trino', platform: 'trino', database: null }],
        'GET /sql/schema': [],
        'GET /sql/history': [],
        'GET /sql/saved': [],
        'GET /v1/system-tables': {
            catalog: 'cairn_system', interval_seconds: 60, freshness_seconds: 120,
            tables: [{ name: 'cairn_system.query.history', schema: 'query', table: 'history', retention_days: 90,
                       description: 'Queries Trino ran.', row_filtered: true, masked_columns: ['statement'],
                       columns: [{ name: 'user_name', type: 'string', description: 'The person.' },
                                 { name: 'statement', type: 'string', description: 'The SQL.' }] }],
        },
    });
    render(
        <MemoryRouter>
            <CairnProvider client={client} cache={new QueryCache()}>
                <SqlWorkspacePage />
            </CairnProvider>
        </MemoryRouter>,
    );
    const name = await screen.findByText('cairn_system.query.history');
    expect(screen.getByText('System tables')).toBeTruthy();
    expect(screen.getByText(/fresh within 2 min/)).toBeTruthy();
    fireEvent.doubleClick(name);
    expect(sqlOf(screen.getByLabelText('SQL'))).toBe('cairn_system.query.history');      // at the cursor
});

it('writes CSV with quoting', () => {
    expect(toCsv(['a', 'b'], [['x,y', 'he said "hi"'], [null, 2]])).toBe('a,b\r\n"x,y","he said ""hi"""\r\n,2');
});

it('suggests the tables and columns the person may query as they type, and takes one with the keyboard', async () => {
    const { client } = makeClient({
        'GET /ui/config': UI_CONFIG,
        'GET /data/sources': [{ connection: 'trino', platform: 'trino', database: null }],
        'GET /sql/schema': [
            { urn: 'u1', name: 'lake.sales.orders', columns: [{ name: 'region', type: 'varchar' }, { name: 'amount', type: 'double' }] },
            { urn: 'u2', name: 'lake.sales.customers', columns: [{ name: 'email', type: 'varchar' }] },
        ],
        'GET /sql/history': [],
        'GET /sql/saved': [],
    });
    render(
        <MemoryRouter>
            <CairnProvider client={client} cache={new QueryCache()}>
                <SqlWorkspacePage />
            </CairnProvider>
        </MemoryRouter>,
    );
    expect(await screen.findByText('lake.sales.orders')).toBeTruthy();
    const box = screen.getByLabelText('SQL');
    const view = editorOf(box);
    const labels = () => (currentCompletions(view.state) ?? []).map((c) => c.label);
    const settle = () => new Promise((r) => setTimeout(r, 100));     // a list is taken only once it has been open 75 ms
    typeSql(box, 'SELECT * FROM lake.sales.o');
    startCompletion(view);
    await waitFor(() => expect(labels()).toEqual(['orders', 'customers']));            // prefix first, then substring
    await settle();
    acceptCompletion(view);
    expect(sqlOf(box)).toBe('SELECT * FROM lake.sales.orders');
    // Columns of the table read, with the list opened at the select list.
    typeSql(box, 'SELECT  FROM lake.sales.orders', 7);
    startCompletion(view);
    await waitFor(() => expect(labels()).toEqual(['amount', 'region']));
    await settle();
    moveCompletionSelection(true)(view);
    acceptCompletion(view);
    expect(sqlOf(box)).toBe('SELECT region FROM lake.sales.orders');
});


function workspace(routes: Record<string, unknown>, calls: MockCall[] = []) {
    const { client } = makeClient({
        'GET /ui/config': UI_CONFIG,
        'GET /data/sources': [{ connection: 'trino', platform: 'trino', database: null }],
        'GET /sql/schema': [], 'GET /sql/history': [], 'GET /sql/saved': [],
        ...routes,
    }, calls);
    render(
        <MemoryRouter>
            <CairnProvider client={client} cache={new QueryCache()}>
                <SqlWorkspacePage />
            </CairnProvider>
        </MemoryRouter>,
    );
}

const answer = (n: number) => ({ columns: ['n'], rows: [[n]], masked_columns: [], truncated: false, elapsed_ms: 3, tables: [] });

it('runs the statement at the cursor, the selection, or all of them, each in its own tab (L163)', async () => {
    const calls: MockCall[] = [];
    workspace({ 'POST /sql/run': (body: unknown) => answer(Number(/SELECT (\d+)/.exec((body as { sql: string }).sql)![1])) }, calls);
    await waitFor(() => expect(calls.some((c) => c.path === '/data/sources')).toBe(true));
    const box = screen.getByLabelText('SQL');
    const file = "SELECT 1;\n-- the second; with a ; in a comment\nSELECT 2 WHERE 'a;b' <> '';\nSELECT 3";
    typeSql(box, file, file.indexOf('SELECT 2') + 3);
    fireEvent.click(await screen.findByRole('button', { name: 'Run' }));
    await waitFor(() => expect(calls.filter((c) => c.path === '/sql/run')).toHaveLength(1));
    expect((calls.find((c) => c.path === '/sql/run')!.body as { sql: string }).sql)
        .toBe("-- the second; with a ; in a comment\nSELECT 2 WHERE 'a;b' <> ''");
    expect(screen.queryByRole('group', { name: 'Statements' })).toBeNull();          // one statement: no tabs

    // The selection: the first and the second.
    selectSql(box, 0, file.indexOf("<> ''") + 5);
    fireEvent.click(screen.getByRole('button', { name: 'Run' }));
    await waitFor(() => expect(calls.filter((c) => c.path === '/sql/run')).toHaveLength(3));

    // All, with Ctrl+Shift+Enter: three runs, three tabs, the last one shown.
    editorOf(box).focus();
    fireEvent.keyDown(editorOf(box).contentDOM, { key: 'Enter', ctrlKey: true, shiftKey: true });
    await waitFor(() => expect(calls.filter((c) => c.path === '/sql/run')).toHaveLength(6));
    const statementTabs = () => within(screen.getByRole('group', { name: 'Statements' })).getAllByRole('tab');
    await waitFor(() => expect(statementTabs()).toHaveLength(3));
    const tabs = statementTabs();
    expect(tabs.map((t) => t.textContent)).toEqual(['1 SELECT 1', "2 SELECT 2 WHERE 'a;b' <> ''", '3 SELECT 3']);
    expect(await screen.findByRole('cell', { name: '3' })).toBeTruthy();             // the last one's result
    fireEvent.click(tabs[0]);
    expect(await screen.findByRole('cell', { name: '1' })).toBeTruthy();
});

it('stops at the first statement that fails, and Stop ends a run (L163)', async () => {
    const calls: MockCall[] = [];
    let release: (v: unknown) => void = () => undefined;
    workspace({
        'POST /sql/run': (body: unknown) => {
            const sql = (body as { sql: string }).sql;
            if (sql.includes('bad')) throw new Error('boom');
            if (sql.includes('slow')) return new Promise((r) => { release = r; });
            return answer(1);
        },
    }, calls);
    await waitFor(() => expect(calls.some((c) => c.path === '/data/sources')).toBe(true));
    const box = screen.getByLabelText('SQL');
    typeSql(box, 'SELECT 1; SELECT bad; SELECT 1');
    fireEvent.click(screen.getByRole('button', { name: 'Run all' }));
    const statementTabs = () => within(screen.getByRole('group', { name: 'Statements' })).getAllByRole('tab');
    await waitFor(() => expect(statementTabs()).toHaveLength(3));
    await waitFor(() => expect(calls.filter((c) => c.path === '/sql/run')).toHaveLength(2));   // the third never sent
    expect(editorOf(box).state.selection.main).toMatchObject({ from: 10, to: 20 });          // the failing one selected
    fireEvent.click(statementTabs()[2]);
    expect(await screen.findByText('Not run: a statement before it failed.')).toBeTruthy();

    typeSql(box, 'SELECT slow; SELECT 1');
    fireEvent.click(screen.getByRole('button', { name: 'Run all' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Stop' }));
    release(answer(7));
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull());
    fireEvent.click(statementTabs()[1]);
    expect(await screen.findByText('Stopped before it finished.')).toBeTruthy();
    expect(calls.filter((c) => c.path === '/sql/run' && (c.body as { sql: string }).sql === 'SELECT 1')).toHaveLength(1);
});

it('formats in the connection\'s dialect, and says so when it cannot (L163)', async () => {
    const calls: MockCall[] = [];
    workspace({}, calls);
    await waitFor(() => expect(calls.some((c) => c.path === '/data/sources')).toBe(true));
    const box = screen.getByLabelText('SQL');
    typeSql(box, "select region, count(*) n from sales.orders where note = 'a;b' group by region");
    fireEvent.click(await screen.findByRole('button', { name: 'Format' }));
    await waitFor(() => expect(sqlOf(box)).toBe(
        "SELECT\n  region,\n  count(*) n\nFROM\n  sales.orders\nWHERE\n  note = 'a;b'\nGROUP BY\n  region"));
    expect(editorOf(box).state.doc.lines).toBe(9);
});
