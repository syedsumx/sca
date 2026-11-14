import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { AssetHistory } from '../components/History';
import { CairnProvider } from '../context/CairnProvider';
import { DS, MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const TAGS = {
    id: 'c1',
    urn: DS,
    aspect: 'globalTags',
    actor: 'urn:li:corpuser:bob',
    ts_ms: Date.now() - 60_000,
    summary: 'added PII (tags[urn:li:tag:PII])',
    changes: [{ field: 'tags[urn:li:tag:PII]', op: 'added', before: null, after: { tag: 'urn:li:tag:PII' } }],
    revert_of: null,
    reverted_by: null,
    revertible: true,
};
const DOCS = {
    ...TAGS,
    id: 'c2',
    aspect: 'editableDatasetProperties',
    summary: 'description changed',
    changes: [{ field: 'description', op: 'changed', before: 'Orders', after: 'One row per order' }],
};

function wrap(routes: Record<string, unknown>, calls: MockCall[] = []) {
    const { client } = makeClient({ 'GET /ui/config': UI_CONFIG, ...routes }, calls);
    render(
        <MemoryRouter>
            <CairnProvider client={client} cache={new QueryCache()}>
                <AssetHistory urn={DS} />
            </CairnProvider>
        </MemoryRouter>,
    );
}

it('shows each change as a diff and reverts one', async () => {
    const calls: MockCall[] = [];
    wrap({ 'GET /history': [DOCS, TAGS], 'POST /history/c1/revert': { ...TAGS, id: 'c3', revert_of: 'c1' } }, calls);
    expect(await screen.findByText('Description')).toBeTruthy();
    expect(screen.getByText('Orders').tagName).toBe('DEL');
    expect(screen.getByText('One row per order').tagName).toBe('INS');
    expect(screen.getByText('PII')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: `Revert ${TAGS.summary}` }));
    fireEvent.click(await screen.findByRole('button', { name: 'OK' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path === '/history/c1/revert')).toBe(true));
    expect(calls.find((c) => c.method === 'POST')!.body).toEqual({ force: false });
});

it('offers to overwrite when the aspect changed again since', async () => {
    const calls: MockCall[] = [];
    let n = 0;
    wrap(
        {
            'GET /history': [TAGS],
            'POST /history/c1/revert': () =>
                n++ === 0
                    ? new Response(
                          JSON.stringify({ detail: 'globalTags has changed since; revert the later change first' }),
                          {
                              status: 409,
                          },
                      )
                    : { ...TAGS, id: 'c3', revert_of: 'c1' },
        },
        calls,
    );
    fireEvent.click(await screen.findByRole('button', { name: `Revert ${TAGS.summary}` }));
    fireEvent.click(await screen.findByRole('button', { name: 'OK' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Overwrite' }));
    await waitFor(() => expect(calls.filter((c) => c.method === 'POST').length).toBe(2));
    expect(calls.filter((c) => c.method === 'POST')[1].body).toEqual({ force: true });
});

it('says when nothing was recorded', async () => {
    wrap({ 'GET /history': [] });
    expect(await screen.findByText('No changes recorded yet.')).toBeTruthy();
});
