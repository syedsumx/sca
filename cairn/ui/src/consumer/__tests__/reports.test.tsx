import { fireEvent, render, screen, within } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { CairnProvider } from '../context/CairnProvider';
import { ReportsPage } from '../pages/ReportsPage';
import { DS, MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const row = (key: string | null, assets: number, pct: number) => ({
    key,
    assets,
    description: pct,
    owner: pct,
    classification: pct,
    certified: 0,
    monitored: pct,
});
const SNAP = {
    day: '2026-10-28',
    computed_ms: 1,
    assets: 3,
    coverage: {
        overall: { description: 66.7, owner: 33.3, classification: 33.3, certified: 0, monitored: 33.3 },
        by_domain: [row('urn:li:domain:finance', 2, 50), row(null, 1, 0)],
        by_platform: [row('snowflake', 2, 50)],
    },
    adoption: {
        weekly_active_users: 12,
        monthly_active_users: 30,
        asset_views_7d: 1234,
        requests_30d: 5,
        approved_30d: 4,
        pending_requests: 1,
        median_hours_to_decision: 3.5,
    },
    quality: { open_incidents: 2, monitored_pct: 33.3, resolved_30d: 6, mean_hours_to_resolve: 50 },
    governance: { personas: 3, purposes: 2, active_grants: 9, grants_expiring_7d: 1 },
};

it('shows the figures and drills into the assets behind one', async () => {
    const calls: MockCall[] = [];
    const { client } = makeClient(
        {
            'GET /ui/config': UI_CONFIG,
            'GET /reports': SNAP,
            'GET /reports/costs/idle': [
                {
                    urn: DS,
                    name: 'old_orders',
                    platform: 'snowflake',
                    storage_usd: 70,
                    compute_usd: 0,
                    cost_usd: 70,
                    reads: 0,
                    storage_bytes: 5e12,
                    last_day: '2026-10-27',
                    monthly_storage_usd: 70,
                },
            ],
            'GET /reports/costs/expensive': [],
            'POST /reports/costs/propose-deprecation': { proposed: true },
            'GET /reports/assets': [
                {
                    urn: DS,
                    name: 'orders',
                    domain: null,
                    platform: 'snowflake',
                    description: true,
                    owner: false,
                    classification: false,
                    certified: false,
                    monitored: false,
                },
            ],
        },
        calls,
    );
    render(
        <MemoryRouter>
            <CairnProvider client={client} cache={new QueryCache()}>
                <ReportsPage />
            </CairnProvider>
        </MemoryRouter>,
    );
    expect(within(await screen.findByLabelText('Weekly active users')).getByText('12')).toBeTruthy();
    expect(within(screen.getByLabelText('Median time to decision')).getByText('3.5 h')).toBeTruthy();
    expect(within(screen.getByLabelText('Mean time to resolve')).getByText('2.1 d')).toBeTruthy();
    expect(within(screen.getByLabelText('Active grants')).getByText('1 expire within 7 days')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Owned in No domain: 0%' }));
    expect(await screen.findByText('orders')).toBeTruthy();
    const drill = calls.find((c) => c.path === '/reports/assets')!;
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Propose deprecation' }));
    expect(await screen.findByText('Proposed')).toBeTruthy();
    const proposal = calls.find((c) => c.path === '/reports/costs/propose-deprecation')!.body as {
        urn: string;
        reason: string;
    };
    expect(proposal.urn).toBe(DS);
    expect(proposal.reason).toMatch(/costs \$70.00 a month/);
    expect(Object.fromEntries(drill.url.searchParams)).toEqual({ missing: 'owner', domain: 'none' });
});
