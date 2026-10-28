import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { StewardReview } from '../components/Steward';
import { CairnProvider } from '../context/CairnProvider';
import { DS, MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const PROPS = [
    {
        id: 'sp_1',
        urn: DS,
        name: 'orders',
        kind: 'owner',
        field: '',
        value: 'urn:li:corpuser:ana',
        confidence: 0.82,
        evidence: ['ran 80 of the 100 recent queries'],
        state: 'PROPOSED',
    },
    {
        id: 'sp_2',
        urn: DS,
        name: 'orders',
        kind: 'description',
        field: '',
        value: { description: 'One row per order.', columns: [] },
        confidence: 0.6,
        evidence: ['drafted from the schema'],
        state: 'PROPOSED',
    },
    {
        id: 'sp_3',
        urn: DS,
        name: 'orders',
        kind: 'term',
        field: 'email',
        value: 'urn:li:glossaryTerm:Classification.PII.Email',
        confidence: 0.95,
        evidence: ['3/3 sampled values match Email'],
        state: 'PROPOSED',
    },
];

it('accepts a selection with edits and rejects another', async () => {
    const calls: MockCall[] = [];
    const { client } = makeClient(
        {
            'GET /ui/config': UI_CONFIG,
            'GET /ai/steward/proposals': PROPS,
            'POST /ai/steward/decide': { decided: 2, batch_id: 'bulk_1' },
            'POST /ai/steward/run': {
                run_id: 'r',
                datasets_with_gaps: 4,
                examined: 4,
                proposed: 3,
                auto_accepted: 0,
                by_kind: {},
            },
        },
        calls,
    );
    render(
        <MemoryRouter>
            <CairnProvider client={client} cache={new QueryCache()}>
                <StewardReview />
            </CairnProvider>
        </MemoryRouter>,
    );
    expect(await screen.findByText('ran 80 of the 100 recent queries')).toBeTruthy();
    expect(screen.getByText('Classification.PII.Email')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('Description for orders'), { target: { value: 'Orders, one row each.' } });
    const rows = screen.getAllByRole('row').slice(1);
    fireEvent.click(within(rows[1]).getByRole('checkbox'));
    fireEvent.click(within(rows[2]).getByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: 'Accept 2' }));
    await waitFor(() =>
        expect(calls.find((c) => c.path === '/ai/steward/decide')?.body).toEqual({
            ids: ['sp_2', 'sp_3'],
            action: 'accept',
            edits: { sp_2: 'Orders, one row each.' },
        }),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Find gaps now' }));
    await waitFor(() => expect(calls.some((c) => c.path === '/ai/steward/run')).toBe(true));
});
