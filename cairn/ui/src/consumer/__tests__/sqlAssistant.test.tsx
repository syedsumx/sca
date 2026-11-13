import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { CairnProvider } from '../context/CairnProvider';
import { SqlWorkspacePage } from '../pages/SqlWorkspacePage';
import { installLayoutStubs, sqlOf, typeSql } from './codemirror';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);
beforeAll(installLayoutStubs);
beforeEach(() => window.localStorage.clear());

function editor(routes: Record<string, unknown>, calls: MockCall[]) {
    const { client } = makeClient({
        'GET /ui/config': UI_CONFIG,
        'GET /data/sources': [{ connection: 'wh', platform: 'postgres', database: null }],
        'GET /sql/schema': [], 'GET /sql/history': [], 'GET /sql/saved': [], 'GET /sql/folders': [],
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

/** The assistant's drawer (a dialog titled SQL assistant). */
async function assistant(): Promise<HTMLElement> {
    const title = await screen.findByText('SQL assistant');
    return title.closest('[role="dialog"]') as HTMLElement;
}

const WRITTEN = { sql: 'SELECT region, count(*) AS n FROM public.orders GROUP BY region', explanation: 'Orders per region.',
    connection: 'wh', dialect: 'postgres', tables: [], error: null, same_connection: true };

it('writes SQL from a question into the editor, and explains the statement at the cursor (L163)', async () => {
    const calls: MockCall[] = [];
    editor({
        'POST /sql/assist/write': WRITTEN,
        'POST /sql/assist/explain': { summary: 'Orders per region.', steps: ['Reads public.orders', 'Counts per region'],
            cautions: ['Orders without a region are one group'], tables: ['public.orders'] },
    }, calls);
    await waitFor(() => expect(calls.some((c) => c.path === '/data/sources')).toBe(true));
    fireEvent.click(screen.getByRole('button', { name: 'Assistant' }));
    const drawer = await assistant();
    fireEvent.change(within(drawer).getByLabelText('Question'), { target: { value: 'orders per region' } });
    fireEvent.click(within(drawer).getByRole('button', { name: 'Write' }));
    expect((await within(drawer).findByLabelText('Proposed SQL')).textContent).toBe(WRITTEN.sql);
    expect(calls.find((c) => c.path === '/sql/assist/write')!.body).toEqual({ question: 'orders per region', connection: 'wh' });
    fireEvent.click(within(drawer).getByRole('button', { name: 'Insert at cursor' }));
    await waitFor(() => expect(sqlOf(screen.getByLabelText('SQL'))).toBe(WRITTEN.sql));
    expect(calls.some((c) => c.path === '/sql/run')).toBe(false);                       // the assistant runs nothing

    fireEvent.click(screen.getByRole('button', { name: 'Assistant' }));
    const again = await assistant();
    fireEvent.click(within(again).getByRole('button', { name: 'Explain the statement' }));
    const explained = await within(again).findByTestId('assistant-explained');
    expect(within(explained).getByText('Counts per region')).toBeTruthy();
    expect(within(explained).getByText('Orders without a region are one group')).toBeTruthy();
    expect(calls.find((c) => c.path === '/sql/assist/explain')!.body).toEqual({ sql: WRITTEN.sql, connection: 'wh' });
});

it('a statement written for another connection opens in a new tab on it', async () => {
    const calls: MockCall[] = [];
    editor({ 'POST /sql/assist/write': { ...WRITTEN, connection: 'lake', same_connection: false } }, calls);
    await waitFor(() => expect(calls.some((c) => c.path === '/data/sources')).toBe(true));
    fireEvent.click(screen.getByRole('button', { name: 'Assistant' }));
    const drawer = await assistant();
    fireEvent.change(within(drawer).getByLabelText('Question'), { target: { value: 'orders per region' } });
    fireEvent.click(within(drawer).getByRole('button', { name: 'Write' }));
    expect(await within(drawer).findByText('Written for lake, where the tables that answer it are')).toBeTruthy();
    expect(within(drawer).queryByRole('button', { name: 'Insert at cursor' })).toBeNull();
    fireEvent.click(within(drawer).getByRole('button', { name: 'Open in a new tab' }));
    await waitFor(() => expect(screen.getAllByRole('tab', { name: /Untitled 2/ }).length).toBeGreaterThan(0));
    expect(sqlOf(screen.getByLabelText('SQL'))).toBe(WRITTEN.sql);
});

it('fixes a failed statement in place, after the person sees the fix (L163)', async () => {
    const calls: MockCall[] = [];
    const broken = 'SELECT region, sum(amount_usd) FROM public.orders GROUP BY region';
    const fixed = 'SELECT region, sum(amount) FROM public.orders GROUP BY region';
    editor({
        'POST /sql/run': () => new Response(JSON.stringify({ detail: 'column "amount_usd" does not exist' }), { status: 400 }),
        'POST /sql/assist/fix': { sql: fixed, explanation: 'amount_usd is not a column; amount is.', tables: ['public.orders'] },
    }, calls);
    await waitFor(() => expect(calls.some((c) => c.path === '/data/sources')).toBe(true));
    const box = screen.getByLabelText('SQL');
    typeSql(box, `SELECT 1;\n${broken}`);
    fireEvent.click(screen.getByRole('button', { name: 'Run' }));
    const fix = await screen.findByRole('button', { name: 'Fix with AI' });
    fireEvent.click(fix);
    expect((await screen.findByLabelText('Proposed fix')).textContent).toBe(fixed);
    const body = calls.find((c) => c.path === '/sql/assist/fix')!.body as { sql: string; error: string; connection: string };
    expect(body.sql).toBe(broken);
    expect(body.error).toContain('amount_usd');
    expect(sqlOf(box)).toBe(`SELECT 1;\n${broken}`);                                    // nothing changed until asked
    fireEvent.click(screen.getByRole('button', { name: 'Replace the statement' }));
    expect(sqlOf(box)).toBe(`SELECT 1;\n${fixed}`);                                      // only that statement
});
