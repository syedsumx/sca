import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { DefinitionsView, PastAnswersView } from '../components/ContextLayer';
import { CairnProvider } from '../context/CairnProvider';
import { FindingView } from '../pages/ContextInboxPage';
import { DS, MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

function wrap(node: React.ReactNode, routes: Record<string, unknown>, calls: MockCall[] = []) {
    const { client } = makeClient({ 'GET /ui/config': UI_CONFIG, ...routes }, calls);
    return render(
        <MemoryRouter>
            <CairnProvider client={client} cache={new QueryCache()}>
                {node}
            </CairnProvider>
        </MemoryRouter>,
    );
}

const FINDING = {
    id: 'fnd_1',
    type: 'METRIC_DEFINITION',
    status: 'OPEN',
    confidence: 0.8,
    entities: [DS],
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    title: 'Metric “revenue” = sum(amount)',
    summary_md: 'x',
    evidence: [],
    proposed_changes: [
        {
            kind: 'create_term',
            entity_urn: null,
            field_path: null,
            value: { name: 'Revenue', expression: 'sum(amount)' },
        },
    ],
};

it('edits a metric before approving it', async () => {
    const acted: unknown[] = [];
    wrap(<FindingView finding={FINDING as never} onAct={(a, edits) => acted.push([a, edits])} />, {});
    fireEvent.click(screen.getByRole('button', { name: 'Edit & approve' }));
    fireEvent.change(await screen.findByLabelText('Expression'), { target: { value: 'sum(amount) - sum(refunds)' } });
    fireEvent.change(screen.getByLabelText('Definition description'), { target: { value: 'Net of refunds.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
    expect(acted).toEqual([
        ['accept', { name: 'Revenue', expression: 'sum(amount) - sum(refunds)', description: 'Net of refunds.' }],
    ]);
});

it('lists definitions and exports a Snowflake semantic view', async () => {
    const calls: MockCall[] = [];
    const blobs: Blob[] = [];
    URL.createObjectURL = ((b: Blob) => {
        blobs.push(b);
        return 'blob:x';
    }) as typeof URL.createObjectURL;
    URL.revokeObjectURL = () => undefined;
    wrap(
        <DefinitionsView />,
        {
            'GET /context/definitions': [
                {
                    id: 'def_1',
                    kind: 'metric',
                    name: 'revenue',
                    version: 2,
                    doc: { expression: 'sum(amount)', datasets: [DS] },
                    approved_by: 'urn:li:corpuser:ana',
                    updated_ms: 1,
                },
            ],
            'GET /context/definitions/export': () =>
                new Response('CREATE OR REPLACE SEMANTIC VIEW cairn_semantic_view', { status: 200 }),
        },
        calls,
    );
    expect(await screen.findByText('sum(amount)')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Export Snowflake semantic view' }));
    await waitFor(() => expect(blobs).toHaveLength(1));
    expect(blobs[0].type).toContain('application/sql');
    expect(calls.find((c) => c.path === '/context/definitions/export')!.url.searchParams.get('format')).toBe(
        'snowflake',
    );
});

it('finds how a question was answered before', async () => {
    wrap(<PastAnswersView />, {
        'GET /context/answers': [
            {
                urn: 'urn:li:query:q1',
                title: 'Monthly revenue by region',
                sql: 'SELECT region, SUM(amount)…',
                tables: [DS],
                score: 0.8,
                definitions: [{ name: 'revenue', kind: 'metric', version: 2 }],
            },
        ],
    });
    fireEvent.change(screen.getByLabelText('Question'), { target: { value: 'revenue by region' } });
    fireEvent.click(screen.getByRole('button', { name: /Find/ }));
    expect(await screen.findByText('Monthly revenue by region')).toBeTruthy();
    expect(screen.getByText('revenue v2')).toBeTruthy();
});
