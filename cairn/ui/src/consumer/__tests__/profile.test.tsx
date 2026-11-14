import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { CairnProvider } from '../context/CairnProvider';
import { ProfilePage } from '../pages/ProfilePage';
import { KEPT_ROUTES, NAV_GROUPS } from '../components/Layout';
import { CATALOGS } from '../i18n/messages';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const DS = 'urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.public.orders,PROD)';
const PROFILE = {
    urn: 'urn:li:corpuser:ana', role: 'Analyst', team: null, default_persona: null, language: null,
    pinned: [{ kind: 'asset', ref: DS, label: 'orders' }], instructions: 'Start with the number.',
    notifications: { channels: { in_app: true, email: false, slack: true, teams: false, push: false }, events: {} },
    personas: [{ id: 'finance-analyst', name: 'Finance analyst' }],
    push: { enabled: false, public_key: null, subscriptions: 0 }, updated_ms: 1,
};

it('is in the header (L149), not the sidebar, and named in every language', () => {
    expect(NAV_GROUPS.flatMap((g) => g.items).map((i) => i.to)).not.toContain('/profile');
    expect(KEPT_ROUTES).toContain('/profile');
    for (const catalog of Object.values(CATALOGS)) expect((catalog as Record<string, string>)['nav.profile']).toBeTruthy();
});

it('edits the profile and saves it in the API shape', async () => {
    const calls: MockCall[] = [];
    const { client } = makeClient({
        'GET /ui/config': UI_CONFIG,
        'GET /v1/me/profile': PROFILE,
        'PUT /v1/me/profile': (body: unknown) => ({ ...PROFILE, ...(body as object), notifications: PROFILE.notifications }),
    }, calls);
    render(<MemoryRouter><CairnProvider client={client} cache={new QueryCache()}><ProfilePage /></CairnProvider></MemoryRouter>);
    const role = (await screen.findByLabelText('Role')) as HTMLInputElement;
    expect(role.value).toBe('Analyst');
    expect(screen.getByRole('link', { name: 'orders' }).getAttribute('href')).toBe(`/asset/${encodeURIComponent(DS)}`);
    // Push is not set up on this Cairn: its channel cannot be turned on.
    expect((screen.getByRole('checkbox', { name: /Push notifications/ }) as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByRole('checkbox', { name: /Inbox in Cairn/ }) as HTMLInputElement).disabled).toBe(true);

    fireEvent.change(role, { target: { value: 'Financial analyst' } });
    fireEvent.change(screen.getByLabelText('Team'), { target: { value: 'FP&A' } });
    fireEvent.change(screen.getByLabelText('Standing instructions'), { target: { value: 'Use EUR.' } });
    fireEvent.click(screen.getByRole('checkbox', { name: 'Email' }));
    fireEvent.click(screen.getByRole('button', { name: 'Unpin orders' }));
    fireEvent.change(screen.getByLabelText('What to pin (an urn or id)'), { target: { value: 'net_revenue' } });
    fireEvent.mouseDown(screen.getByRole('combobox', { name: 'Kind of item to pin' }));
    fireEvent.click(await screen.findByText('Metric'));
    fireEvent.click(screen.getByRole('button', { name: 'Pin' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save profile' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true));
    expect(calls.find((c) => c.method === 'PUT')!.body).toEqual({
        role: 'Financial analyst', team: 'FP&A', default_persona: null, language: null, instructions: 'Use EUR.',
        pinned: [{ kind: 'metric', ref: 'net_revenue', label: 'net_revenue' }],
        notifications: { channels: { in_app: true, email: true, slack: true, teams: false, push: false } },
    });
});
