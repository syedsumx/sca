import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import type { LicenceState } from '../api/types';
import { LicenceBanner, LicenceSettings, bannerFor } from '../components/Licence';
import { CairnProvider } from '../context/CairnProvider';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const base: LicenceState = { status: 'evaluation', read_only: false, days_left: null, over_seats: false };
const VALID: LicenceState = {
    ...base,
    status: 'valid',
    days_left: 200,
    seats_used: 3,
    seats_limit: 50,
    licence: {
        id: 'lic_1',
        customer: 'Acme',
        tenant: 'acme',
        edition: 'enterprise',
        features: ['ai', 'automations'],
        max_users: 50,
        issued_at: '2026-11-01',
        expires_at: '2027-05-20',
        grace_days: 14,
        development: false,
    },
};

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

it('the banner speaks only when the licence needs attention', () => {
    expect(bannerFor(base)).toBeNull();
    expect(bannerFor(VALID)).toBeNull();
    expect(bannerFor({ ...VALID, days_left: 9 })?.text).toBe('The licence expires in 9 days.');
    expect(bannerFor({ ...base, status: 'expiring', days_left: -3 })).toMatchObject({ type: 'warning' });
    expect(bannerFor({ ...base, status: 'expired', read_only: true })?.text).toMatch(/read-only until it is renewed/);
    expect(bannerFor({ ...base, status: 'missing', read_only: true })?.type).toBe('error');
    expect(bannerFor({ ...VALID, over_seats: true })?.text).toMatch(/newest can read/);
});

it('shows the banner from the API state', async () => {
    wrap(<LicenceBanner />, { 'GET /admin/licence': { ...base, status: 'expired', read_only: true, days_left: -30 } });
    expect((await screen.findByRole('status')).textContent).toMatch('The licence has expired');
});

it('admins see the licence and install a new key', async () => {
    const calls: MockCall[] = [];
    let state: LicenceState = base;
    wrap(
        <LicenceSettings />,
        {
            'GET /admin/licence': () => state,
            'PUT /admin/licence': (body: { key: string }) => {
                state = VALID;
                return body.key === 'CAIRN1.good' ? VALID : new Response('{"detail":"bad"}', { status: 422 });
            },
        },
        calls,
    );
    expect(await screen.findByText(/evaluation mode/)).toBeTruthy();
    const install = screen.getByRole('button', { name: 'Install licence' });
    expect((install as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText('Licence key'), { target: { value: ' CAIRN1.good \n' } });
    fireEvent.click(install);
    await waitFor(() => expect(calls.find((c) => c.method === 'PUT')?.body).toEqual({ key: 'CAIRN1.good' }));
    expect(await screen.findByText('Acme')).toBeTruthy();
    expect(screen.getByText('ai, automations')).toBeTruthy();
    expect(screen.getByText(/3 of 50 active in 30 days/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Remove' })).toBeTruthy();
});
