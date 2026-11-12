import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import type { UpdatePlan, UpdatesOverview, UpdatesState } from '../api/types';
import { UpdatesSettings, describeWindows, nextAction } from '../components/Updates';
import { CairnProvider } from '../context/CairnProvider';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const STATE: UpdatesState = {
    current: '2.12.0', channel: 'stable', feed: 'https://releases.example.com/cairn/', checked_at: '2026-11-12T17:00:00Z',
    last_good_at: '2026-11-12T17:00:00Z', error: null,
    available: [
        { version: '2.14.0', released_at: '2026-11-10T09:00:00Z', severity: 'normal', summary: 'Cairn 2.14',
            notes_url: 'https://cairn.example/2.14', min_from: '2.13.0', breaking: [] },
        { version: '2.13.0', released_at: '2026-11-01T09:00:00Z', severity: 'security', summary: 'Cairn 2.13',
            notes_url: '', min_from: '2.12.0',
            breaking: [{ id: 'trino-490', text: 'Trino 490', action: 're-run the catalog sync' }] },
    ],
    path: ['2.13.0', '2.14.0'], target: '2.14.0', severity: 'security', blocked: null,
    breaking: [{ id: 'trino-490', text: 'Trino 490', action: 're-run the catalog sync', version: '2.13.0' }],
};
const POLICY = { mode: 'manual' as const, auto_severities: ['security', 'critical'], timezone: 'Europe/Berlin',
    windows: [{ days: ['sat', 'sun'], start: '02:00', hours: 4 }], require_approval: true, separate_approver: false,
    require_backup: true, check_every_hours: 6 };
const PLAN: UpdatePlan = { id: 'upd_1', from: '2.12.0', target: '2.14.0', path: ['2.13.0', '2.14.0'],
    severity: 'security', status: 'pending_approval', auto: false, not_before: null, requested_by: 'urn:li:corpuser:ops',
    approved_by: null, error: null, log: [{ t: '2026-11-12T17:01:00Z', step: 'plan', msg: 'planned 2.13.0 -> 2.14.0' }],
    created_at: '2026-11-12T17:01:00Z', started_at: null, finished_at: null };

function wrap(routes: Record<string, unknown>, calls: MockCall[] = []) {
    const { client } = makeClient({ 'GET /ui/config': UI_CONFIG, ...routes }, calls);
    return render(
        <MemoryRouter>
            <CairnProvider client={client} cache={new QueryCache()}>
                <UpdatesSettings />
            </CairnProvider>
        </MemoryRouter>,
    );
}

it('says what the operator is asked to do', () => {
    const over: UpdatesOverview = { policy: POLICY, window_open: false, next_window: '2026-11-14T01:00:00Z',
        updater: true, plans: [] };
    expect(describeWindows(over)).toBe('Sat, Sun 02:00 for 4 h (Europe/Berlin)');
    expect(describeWindows({ ...over, policy: { ...POLICY, windows: [] } })).toBe('any time');
    expect(nextAction(STATE, over)).toBe('Cairn 2.14.0 is available.');
    expect(nextAction(STATE, { ...over, plans: [PLAN] })).toMatch(/waiting for approval/);
    expect(nextAction(STATE, { ...over, plans: [{ ...PLAN, status: 'scheduled' }] })).toMatch(/next maintenance window \(2026-11-14/);
    expect(nextAction(STATE, { ...over, updater: false, plans: [{ ...PLAN, status: 'scheduled' }] }))
        .toMatch(/apply it with your deployment tooling/);
    expect(nextAction({ ...STATE, target: null, path: [] }, over)).toBeNull();
});

it('shows the way up, plans an update and approves it', async () => {
    const calls: MockCall[] = [];
    let plans: UpdatePlan[] = [];
    wrap({
        'GET /v1/system/updates': STATE,
        'GET /v1/system/updates/plans': () => ({ policy: POLICY, window_open: false,
            next_window: '2026-11-14T01:00:00Z', updater: true, plans }),
        'POST /v1/system/updates/plans': () => { plans = [PLAN]; return PLAN; },
        'POST /v1/system/updates/plans/upd_1/approve': () => {
            plans = [{ ...PLAN, status: 'scheduled', approved_by: 'urn:li:corpuser:ops2' }];
            return plans[0];
        },
    }, calls);
    expect(await screen.findByText('2.12.0 → 2.13.0 → 2.14.0')).toBeTruthy();
    expect(screen.getByText(/re-run the catalog sync/)).toBeTruthy();
    expect(screen.getByText('Sat, Sun 02:00 for 4 h (Europe/Berlin)')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /Plan update to 2.14.0/ }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path === '/v1/system/updates/plans')).toBe(true));
    fireEvent.click(await screen.findByRole('button', { name: 'Approve' }));
    expect(await screen.findByText(/starts in the next maintenance window/)).toBeTruthy();
});

it('a feed that does not verify offers nothing and says why', async () => {
    wrap({
        'GET /v1/system/updates': { ...STATE, error: 'the index expired', available: [], path: [], target: null,
            severity: null, breaking: [] },
        'GET /v1/system/updates/plans': { policy: POLICY, window_open: false, next_window: null, updater: false, plans: [] },
    });
    expect(await screen.findByText('the index expired')).toBeTruthy();
    expect((screen.getByRole('button', { name: /Plan update/ }) as HTMLButtonElement).disabled).toBe(true);
});
