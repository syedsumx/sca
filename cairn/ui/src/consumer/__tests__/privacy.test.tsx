import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { MyPrivacyParts, PrivacyAdmin } from '../components/Privacy';
import { CairnProvider } from '../context/CairnProvider';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const ORDERS = 'urn:li:dataset:(urn:li:dataPlatform:snowflake,shop.orders,PROD)';
const REQUEST = {
    id: 'psr_1',
    kind: 'deletion',
    identifier_kind: 'email',
    identifier: 'j•••@acme.com',
    state: 'OPEN',
    created_by: 'urn:li:corpuser:dpo',
    created_ms: 1,
    due_ms: 2,
    overdue: false,
    reference: 'DSAR-17',
    findings: [
        {
            dataset: ORDERS,
            columns: ['buyer_email'],
            owners: ['urn:li:corpuser:bob'],
            domains: [],
            rows: 2,
            searched: true,
            reason: null,
        },
    ],
    parts: [
        {
            dataset: ORDERS,
            owners: ['urn:li:corpuser:bob'],
            state: 'PENDING',
            statement: 'DELETE FROM shop.orders …',
            note: null,
            done_by: null,
        },
    ],
};

function wrap(node: React.ReactNode, routes: Record<string, unknown>, calls: MockCall[] = []) {
    const { client } = makeClient({ 'GET /ui/config': UI_CONFIG, ...routes }, calls);
    render(
        <MemoryRouter>
            <CairnProvider client={client} cache={new QueryCache()}>
                {node}
            </CairnProvider>
        </MemoryRouter>,
    );
}

it('opens a deletion request and shows where the person is', async () => {
    const calls: MockCall[] = [];
    wrap(
        <PrivacyAdmin />,
        { 'GET /governance/privacy/requests': [], 'POST /governance/privacy/requests': REQUEST },
        calls,
    );
    fireEvent.mouseDown(await screen.findByRole('combobox', { name: 'Request kind' }));
    fireEvent.click(await screen.findByText('Deletion'));
    fireEvent.change(screen.getByLabelText('Identifier'), { target: { value: 'jane@acme.com' } });
    fireEvent.click(screen.getByRole('button', { name: /Find this person/ }));
    await waitFor(() =>
        expect(calls.find((c) => c.method === 'POST')?.body).toMatchObject({
            kind: 'deletion',
            identifier: 'jane@acme.com',
        }),
    );
    expect(await screen.findByText(/1 dataset owner\(s\) were asked/)).toBeTruthy();
    expect(screen.getByText('buyer_email')).toBeTruthy();
});

it('lets an owner mark their part done', async () => {
    const calls: MockCall[] = [];
    wrap(
        <MyPrivacyParts />,
        {
            'GET /governance/privacy/my-parts': [
                { ...REQUEST.parts[0], request: 'psr_1', identifier: 'j•••@acme.com', due_ms: 2, overdue: true },
            ],
            'POST /governance/privacy/requests/psr_1/parts': REQUEST,
        },
        calls,
    );
    expect(await screen.findByText('overdue')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Done' }));
    await waitFor(() =>
        expect(calls.find((c) => c.method === 'POST')?.body).toMatchObject({ dataset: ORDERS, state: 'DONE' }),
    );
});
