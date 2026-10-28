import { render, screen } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { ContractPanel } from '../components/Contract';
import { TrustBadges } from '../components/TrustBadges';
import { CairnProvider } from '../context/CairnProvider';
import { DS, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

function wrap(routes: Record<string, unknown>) {
    const { client } = makeClient({ 'GET /ui/config': UI_CONFIG, ...routes });
    return render(
        <MemoryRouter>
            <CairnProvider client={client} cache={new QueryCache()}>
                <ContractPanel urn={DS} />
            </CairnProvider>
        </MemoryRouter>,
    );
}

it('shows each promise and which one is broken', async () => {
    wrap({
        'GET /quality/contracts/one': {
            dataset: DS,
            version: '2.0.0',
            owner: 'urn:li:corpGroup:orders-team',
            status: 'BROKEN',
            checked_ms: Date.now(),
            spec: {},
            created_by: 'x',
            updated_ms: 1,
            parts: [
                { part: 'schema', status: 'BROKEN', detail: 'column email is missing' },
                {
                    part: 'freshness',
                    name: 'orders freshness (contract)',
                    monitor_id: 'm1',
                    status: 'MET',
                    detail: 'last update 600s ago',
                },
            ],
            versions: [
                { version: '2.0.0', created_by: 'x', created_ms: 2 },
                { version: '1.0.0', created_by: 'x', created_ms: 1 },
            ],
        },
    });
    expect(await screen.findByText('column email is missing')).toBeTruthy();
    expect(screen.getByText('orders freshness')).toBeTruthy();
    expect(screen.getByText('Broken')).toBeTruthy();
    expect(screen.getByText(/Versions: 2.0.0/)).toBeTruthy();
});

it('explains how to publish when there is no contract', async () => {
    wrap({
        'GET /quality/contracts/one': () => new Response(JSON.stringify({ detail: 'no contract' }), { status: 404 }),
    });
    expect(await screen.findByText(/No data contract/)).toBeTruthy();
});

it('trust badges show a broken contract', () => {
    render(<TrustBadges contract="BROKEN" qualityStatus="PASS" />);
    expect(screen.getByText('Contract broken')).toBeTruthy();
});
