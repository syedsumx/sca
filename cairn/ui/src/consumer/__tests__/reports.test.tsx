import { fireEvent, render, screen, within } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { CairnProvider } from '../context/CairnProvider';
import { CostReports, ReportsPage } from '../pages/ReportsPage';
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
    // Costs moved to Usage & cost (L149): the report links there, and the cost views are the same.
    expect(screen.getByRole('link', { name: 'Usage & cost' }).getAttribute('href')).toBe('/usage?tab=compute-cost');
    render(
        <MemoryRouter>
            <CairnProvider client={client} cache={new QueryCache()}>
                <CostReports />
            </CairnProvider>
        </MemoryRouter>,
    );
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

it('shows what each Spark job cost', async () => {
    const { client } = makeClient({
        'GET /ui/config': UI_CONFIG,
        'GET /reports': SNAP,
        'GET /reports/costs/idle': [],
        'GET /reports/costs/expensive': [],
        'GET /reports/costs/jobs': [
            { job: 'urn:li:dataJob:(urn:li:dataFlow:(spark,orders,PROD),orders_nightly)', engine: 'spark', runs: 30,
              seconds: 36000, usd: 80.15 },
        ],
    });
    render(
        <MemoryRouter>
            <CairnProvider client={client} cache={new QueryCache()}>
                <CostReports />
            </CairnProvider>
        </MemoryRouter>,
    );
    fireEvent.click(await screen.findByRole('tab', { name: 'Spark jobs' }));
    const link = await screen.findByRole('link', { name: 'orders_nightly' });
    const row = link.closest('tr')!;
    expect(within(row).getByText('10.0 h')).toBeTruthy();
    expect(within(row).getByText('30')).toBeTruthy();
    expect(within(row).getByText(/80/)).toBeTruthy();
});

it('shows what Trino queries cost per person, team and table', async () => {
    const calls: MockCall[] = [];
    const report = (by: string) => ({
        by, days: 30, priced: true, scope: 'all', total_usd: 22,
        rows: by === 'user'
            ? [{ key: 'urn:li:corpuser:bob', queries: 2, cpu_hours: 0.5, scanned_bytes: 2e12, usd: 11 },
               { key: 'urn:li:corpuser:ana', queries: 2, cpu_hours: 3, scanned_bytes: 1e12, usd: 11 }]
            : by === 'team'
                ? [{ key: 'urn:li:corpGroup:finance', queries: 2, cpu_hours: 1.5, scanned_bytes: 5e11, usd: 5.5 }]
                : [{ key: DS, name: 'orders', queries: 2, cpu_hours: 2, scanned_bytes: 1e12, usd: 9 }],
    });
    const { client } = makeClient({
        'GET /ui/config': UI_CONFIG,
        'GET /reports': SNAP,
        'GET /reports/costs/idle': [],
        'GET /reports/costs/expensive': [],
        'GET /reports/costs/jobs': [],
        'GET /reports/costs/trino': (_b: unknown, url: URL) => report(url.searchParams.get('by') ?? 'user'),
    }, calls);
    render(
        <MemoryRouter>
            <CairnProvider client={client} cache={new QueryCache()}>
                <CostReports />
            </CairnProvider>
        </MemoryRouter>,
    );
    fireEvent.click(await screen.findByRole('tab', { name: 'Trino' }));
    const ana = (await screen.findByText('ana')).closest('tr')!;
    expect(within(ana).getByText('3.0 h')).toBeTruthy();
    expect(within(ana).getByText('1.0 TB')).toBeTruthy();
    expect(within(ana).getByText('$11.00')).toBeTruthy();
    expect(screen.getByText(/All queries, last 30 days · \$22.00/)).toBeTruthy();
    fireEvent.click(screen.getByText('Teams'));
    expect(await screen.findByText('finance')).toBeTruthy();
    fireEvent.click(screen.getByText('Tables'));
    expect(await screen.findByRole('link', { name: 'orders' })).toBeTruthy();
    expect(calls.filter((c) => c.path === '/reports/costs/trino').map((c) => c.url.searchParams.get('by')))
        .toEqual(['user', 'team', 'table']);
});
