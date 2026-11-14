import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { CairnProvider } from '../context/CairnProvider';
import { GitFoldersPage } from '../pages/GitFoldersPage';
import { QueryInsightPage, bytes, seconds } from '../pages/QueryInsightPage';
import { SqlWorkflowsPage, toBody, toDraft } from '../pages/SqlWorkflowsPage';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

function mount(node: React.ReactElement, routes: Record<string, unknown>, calls: MockCall[]) {
    const { client } = makeClient({ 'GET /ui/config': UI_CONFIG, ...routes }, calls);
    return render(
        <MemoryRouter>
            <CairnProvider client={client} cache={new QueryCache()}>
                {node}
            </CairnProvider>
        </MemoryRouter>,
    );
}

const QUERY = {
    query_id: '20261103_1', user: 'ana', source: 'cairn-sql', state: 'FINISHED', error_code: null, error: null,
    create_ms: 1_762_000_000_000, end_ms: 1_762_000_042_000, wall_ms: 42_000, cpu_ms: 30_000, queued_ms: 20_000,
    input_bytes: 5 * 1024 ** 3, input_rows: 9_000_000, output_rows: 12, peak_memory_bytes: 2 * 1024 ** 2, splits: 40,
    sql: 'SELECT * FROM lake.sales.orders ORDER BY amount', tables: ['lake.sales.orders'],
};

it('lists queries, filters them and opens a profile with its hints', async () => {
    const calls: MockCall[] = [];
    mount(<QueryInsightPage />, {
        'GET /v1/queries': { queries: [QUERY] },
        'GET /v1/queries/20261103_1': {
            ...QUERY,
            hints: [{ kind: 'queued', text: 'Waited 20 s of 42 s in the queue: the warehouse was busy.' },
                    { kind: 'columns', text: 'SELECT * reads every column: name the columns you need.' }],
        },
    }, calls);
    expect(await screen.findByText(QUERY.sql)).toBeTruthy();
    expect(screen.getByText('5.0 GB')).toBeTruthy();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Slowest first' }));
    await waitFor(() => expect(calls.some((c) => c.path === '/v1/queries' && c.url.searchParams.get('slowest') === 'true')).toBe(true));
    fireEvent.change(screen.getByLabelText('Search SQL'), { target: { value: 'orders' } });
    fireEvent.keyDown(screen.getByLabelText('Search SQL'), { key: 'Enter', keyCode: 13 });
    await waitFor(() => expect(calls.some((c) => c.url.searchParams.get('text') === 'orders')).toBe(true));
    fireEvent.click((await screen.findAllByText(QUERY.sql))[0]);
    expect(await screen.findByText(/Waited 20 s of 42 s/)).toBeTruthy();
    expect(screen.getByText(/SELECT \* reads every column/)).toBeTruthy();
    expect(screen.getByText('2.0 MB')).toBeTruthy();
});

it('formats durations and sizes', () => {
    expect(seconds(1500)).toBe('1.50 s');
    expect(seconds(90_000)).toBe('1.5 min');
    expect(bytes(512)).toBe('512 B');
    expect(bytes(1536)).toBe('1.5 KB');
});

const WF = {
    id: 'w1', owner: 'urn:li:corpuser:ana', name: 'Nightly revenue', cron: '0 6 * * *', recipients: [], notify: 'failure',
    enabled: true, next_run_ms: 1_762_000_000_000, last_status: 'failed', updated_ms: 1,
    steps: [
        { name: 'refresh', type: 'derived', after: [], retries: 0, table: 'daily_revenue', full: false },
        { name: 'non-empty', type: 'check', after: ['refresh'], retries: 0, query: 'q1', measure: 'rows', op: '>', value: 0 },
        { name: 'report', type: 'query', after: ['non-empty'], retries: 1, query: 'q2' },
    ],
};

it('lists workflows, runs one now and shows each step of its runs', async () => {
    const calls: MockCall[] = [];
    mount(<SqlWorkflowsPage />, {
        'GET /v1/sql-workflows': [WF],
        'GET /sql/saved': [],
        'GET /v1/derived': [],
        'POST /v1/sql-workflows/w1/run': { id: 'r2', workflow: 'w1', status: 'failed', steps: [] },
        'GET /v1/sql-workflows/w1/runs': [
            { id: 'r1', trigger: 'schedule', status: 'failed', started_ms: 1, finished_ms: 2, steps: [
                { name: 'refresh', status: 'ok', rows: 31 },
                { name: 'non-empty', status: 'error', error: 'rows is 0, not > 0' },
                { name: 'report', status: 'skipped', error: 'non-empty did not succeed' },
            ] },
        ],
    }, calls);
    expect(await screen.findByText('Nightly revenue')).toBeTruthy();
    expect(screen.getByText('refresh → non-empty → report')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Run now' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path === '/v1/sql-workflows/w1/run')).toBe(true));
    expect(await screen.findByText(/rows is 0, not > 0/)).toBeTruthy();
    expect(screen.getByText(/non-empty did not succeed/)).toBeTruthy();
});

it('creates a workflow from the editor with the fields each step uses', async () => {
    const calls: MockCall[] = [];
    mount(<SqlWorkflowsPage />, {
        'GET /v1/sql-workflows': [],
        'GET /sql/saved': [{ id: 'q1', owner: 'urn:li:corpuser:ana', name: 'Orders today', description: '', connection: 'trino',
                             sql: 'x', shared: false, chart: { type: 'table' }, created_ms: 1, updated_ms: 1 }],
        'GET /v1/derived': [],
        'POST /v1/sql-workflows': (body: unknown) => ({ ...WF, ...(body as object), id: 'w9' }),
        'GET /v1/sql-workflows/w9/runs': [],
    }, calls);
    fireEvent.click(await screen.findByRole('button', { name: 'New workflow' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Workflow name'), { target: { value: 'Morning report' } });
    fireEvent.change(within(dialog).getByLabelText('Schedule'), { target: { value: '30 7 * * 1-5' } });
    fireEvent.change(within(dialog).getByLabelText('Step name'), { target: { value: 'send' } });
    fireEvent.mouseDown(within(dialog).getByRole('combobox', { name: 'send saved query' }));
    fireEvent.click(await screen.findByText('Orders today'));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path === '/v1/sql-workflows')).toBe(true));
    const body = calls.find((c) => c.method === 'POST' && c.path === '/v1/sql-workflows')!.body;
    expect(body).toEqual({
        name: 'Morning report', cron: '30 7 * * 1-5', notify: 'failure', enabled: true, recipients: [],
        steps: [{ name: 'send', type: 'query', after: [], retries: 0, query: 'q1' }],
    });
});

it('turns a saved workflow back into the body it was made from', () => {
    const body = toBody(toDraft(WF as never));
    expect(body.steps).toEqual([
        { name: 'refresh', type: 'derived', after: [], retries: 0, table: 'daily_revenue', full: false },
        { name: 'non-empty', type: 'check', after: ['refresh'], retries: 0, query: 'q1', measure: 'rows', op: '>', value: 0 },
        { name: 'report', type: 'query', after: ['non-empty'], retries: 1, query: 'q2' },
    ]);
    expect(body.cron).toBe('0 6 * * *');
});

const FOLDER = {
    id: 'f1', owner: 'urn:li:corpuser:ana', name: 'finance', repo: 'analytics', url: null, branch: 'main', path: 'sql',
    every_minutes: null, head: 'abcdef1234567', synced_ms: 1, error: null, created_ms: 1,
};

it('links a folder, pulls it (conflicts shown, git can win) and pushes for review', async () => {
    const calls: MockCall[] = [];
    mount(<GitFoldersPage />, {
        'GET /v1/git-folders': [FOLDER],
        'GET /sql/saved': [],
        'POST /v1/git-folders': (body: unknown) => ({ ...FOLDER, ...(body as object), id: 'f2' }),
        'POST /v1/git-folders/f1/pull': (body: unknown) =>
            (body as { force: boolean }).force
                ? { head: 'b', created: [], updated: ['revenue.sql'], deleted: [], notebooks: [], conflicts: [], errors: [] }
                : { head: 'b', created: ['orders.sql'], updated: [], deleted: [], notebooks: ['eda.ipynb'],
                    conflicts: [{ file: 'revenue.sql', query: 'q3', why: 'changed in git and in Cairn since the last pull' }],
                    errors: [{ file: 'bad.sql', error: 'chart: not JSON' }] },
        'POST /v1/git-folders/f1/push': { changed: 2, branch: 'cairn/ana/20261103', commit: 'c', pull_request: 'https://git.example.com/pr/7' },
    }, calls);
    expect(await screen.findByText('finance')).toBeTruthy();
    expect(screen.getByText('main : sql')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Pull' }));
    expect(await screen.findByText(/orders\.sql/)).toBeTruthy();
    expect(screen.getByText(/eda\.ipynb/)).toBeTruthy();
    expect(screen.getByText(/revenue\.sql: changed in git and in Cairn/)).toBeTruthy();
    expect(screen.getByText(/bad\.sql: chart: not JSON/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: "Take git's version" }));
    fireEvent.click(await screen.findByRole('button', { name: 'OK' }));
    await waitFor(() => expect(calls.some((c) => c.path === '/v1/git-folders/f1/pull' && (c.body as { force: boolean }).force)).toBe(true));

    fireEvent.click(screen.getByRole('button', { name: 'Push' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Commit message'), { target: { value: 'Weekly review' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Push' }));
    expect(await screen.findByText(/2 files pushed to cairn\/ana\/20261103/)).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Review the pull request' }).getAttribute('href')).toBe('https://git.example.com/pr/7');
    expect(calls.find((c) => c.path === '/v1/git-folders/f1/push')!.body).toEqual({ message: 'Weekly review', add: [] });

    fireEvent.click(screen.getByRole('button', { name: 'Link a folder' }));
    const link = (await screen.findAllByRole('dialog')).slice(-1)[0];
    fireEvent.change(within(link).getByLabelText('Folder name'), { target: { value: 'ops' } });
    fireEvent.change(within(link).getByLabelText('Repository'), { target: { value: 'analytics' } });
    fireEvent.change(within(link).getByLabelText('Path'), { target: { value: 'ops' } });
    fireEvent.click(within(link).getByRole('button', { name: 'Link' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path === '/v1/git-folders')).toBe(true));
    expect(calls.find((c) => c.method === 'POST' && c.path === '/v1/git-folders')!.body).toEqual({
        name: 'ops', repo: 'analytics', branch: 'main', path: 'ops',
    });
});
