import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { DataPreview } from '../components/DataPreview';
import { CairnProvider } from '../context/CairnProvider';
import { tableNameOf } from '../pages/AssetPage';
import { DS, MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const ROWS = {
    columns: ['id', 'email'],
    rows: [[1, 'j•••@•••.com']],
    masked_columns: ['email'],
    truncated: false,
    elapsed_ms: 4,
    tables: [DS],
};

function wrap(routes: Record<string, unknown>, calls: MockCall[] = []) {
    const { client } = makeClient({ 'GET /ui/config': UI_CONFIG, ...routes }, calls);
    return render(
        <MemoryRouter>
            <CairnProvider client={client} cache={new QueryCache()}>
                <DataPreview urn={DS} tableName={tableNameOf(DS)} />
            </CairnProvider>
        </MemoryRouter>,
    );
}

it('previews rows with masked columns marked, and runs a query', async () => {
    const calls: MockCall[] = [];
    wrap({ 'GET /data/preview': ROWS, 'POST /data/query': { ...ROWS, rows: [[2, 'k•••@•••.com']] } }, calls);
    expect(await screen.findByText('🔒 email')).toBeTruthy();
    expect(screen.getByText('j•••@•••.com')).toBeTruthy();
    const sql = screen.getByLabelText('SQL') as HTMLTextAreaElement;
    expect(sql.value).toContain('FROM public.orders');
    fireEvent.change(sql, { target: { value: 'SELECT id, email FROM public.orders WHERE id = 2' } });
    fireEvent.click(screen.getByRole('button', { name: 'Run query' }));
    expect(await screen.findByText('k•••@•••.com')).toBeTruthy();
    await waitFor(() =>
        expect(calls.find((c) => c.path === '/data/query')?.body).toEqual({
            sql: 'SELECT id, email FROM public.orders WHERE id = 2',
            urn: DS,
            limit: 200,
        }),
    );
});

it('explains a missing connection and a policy refusal', async () => {
    const { unmount } = wrap({
        'GET /data/preview': () => new Response(JSON.stringify({ detail: 'no connection' }), { status: 404 }),
    });
    expect(await screen.findByText(/No warehouse connection serves this table/)).toBeTruthy();
    unmount();
    wrap({ 'GET /data/preview': () => new Response(JSON.stringify({ detail: 'no' }), { status: 403 }) });
    expect(await screen.findByText(/A data policy keeps this table's rows from you/)).toBeTruthy();
});

it('answers a question with governed SQL that can be edited', async () => {
    const calls: MockCall[] = [];
    wrap(
        {
            'GET /data/preview': ROWS,
            'POST /data/ask': {
                question: 'orders per day',
                connection: 'wh',
                dialect: 'postgres',
                tables: [DS],
                definitions: [{ name: 'revenue', kind: 'metric', version: 2 }],
                examples: [],
                attempts: 1,
                sql: 'SELECT day, count(*) FROM public.orders GROUP BY day',
                explanation: 'Counts orders per day.',
                result: { ...ROWS, columns: ['day', 'count'], rows: [['2026-10-01', 42]], masked_columns: [] },
                error: null,
            },
        },
        calls,
    );
    await screen.findByText('🔒 email');
    fireEvent.change(screen.getByLabelText('Question'), { target: { value: 'orders per day' } });
    fireEvent.click(screen.getByRole('button', { name: /Ask/ }));
    expect(await screen.findByText('Counts orders per day.')).toBeTruthy();
    expect(screen.getByText('revenue v2')).toBeTruthy();
    expect(screen.getByText('42')).toBeTruthy();
    expect(calls.find((c) => c.path === '/data/ask')?.body).toEqual({ question: 'orders per day', urns: [DS], run: true });
    fireEvent.click(screen.getByRole('button', { name: 'Edit in the SQL editor' }));
    expect((screen.getByLabelText('SQL') as HTMLTextAreaElement).value).toContain('GROUP BY day');
});

it('says when asking is not set up', async () => {
    wrap({
        'GET /data/preview': ROWS,
        'POST /data/ask': () => new Response(JSON.stringify({ detail: 'not configured' }), { status: 503 }),
    });
    await screen.findByText('🔒 email');
    fireEvent.change(screen.getByLabelText('Question'), { target: { value: 'how many orders' } });
    fireEvent.click(screen.getByRole('button', { name: /Ask/ }));
    expect(await screen.findByText(/needs an AI provider/)).toBeTruthy();
});
