import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { SignedInDevices } from '../components/SignedInDevices';
import { CairnProvider } from '../context/CairnProvider';
import { DevicePage, formatCode } from '../pages/DevicePage';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const INFO = { client: 'Cairn CLI', name: 'cairn CLI on ana-laptop (Darwin)', address: '10.0.0.7', requested_ms: 1,
    expires_ms: 2, access_token_seconds: 3600, refresh_token_days: 30 };

it('formats codes as people type them', () => {
    expect(formatCode('bcdf ghjk')).toBe('BCDF-GHJK');
    expect(formatCode('bc-d')).toBe('BCD');
    expect(formatCode('BCDF-GHJKLM')).toBe('BCDF-GHJK');
});

it('shows which device asks, then approves the code from the link', async () => {
    const calls: MockCall[] = [];
    const { client } = makeClient({
        'GET /ui/config': UI_CONFIG,
        'GET /oauth/device': INFO,
        'POST /oauth/device': { approved: true, name: INFO.name },
    }, calls);
    render(<MemoryRouter initialEntries={['/device?code=BCDF-GHJK']}><CairnProvider client={client} cache={new QueryCache()}>
        <DevicePage /></CairnProvider></MemoryRouter>);
    expect(await screen.findByText(INFO.name)).toBeTruthy();
    expect(screen.getByText('10.0.0.7')).toBeTruthy();
    expect(calls.find((c) => c.method === 'GET' && c.path === '/oauth/device')!.url.searchParams.get('code')).toBe('BCDF-GHJK');
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
    expect(await screen.findByText('Device signed in')).toBeTruthy();
    expect(calls.find((c) => c.method === 'POST')!.body).toEqual({ user_code: 'BCDF-GHJK', decision: 'approve' });
});

it('takes a typed code, and says so when it is not valid', async () => {
    const { client } = makeClient({
        'GET /ui/config': UI_CONFIG,
        'GET /oauth/device': () => new Response(JSON.stringify({ detail: 'this code is not valid' }), { status: 404 }),
    });
    render(<MemoryRouter initialEntries={['/device']}><CairnProvider client={client} cache={new QueryCache()}>
        <DevicePage /></CairnProvider></MemoryRouter>);
    const input = screen.getByLabelText('The code your terminal shows');
    fireEvent.change(input, { target: { value: 'zzzzzzzz' } });
    expect((input as HTMLInputElement).value).toBe('ZZZZ-ZZZZ');
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
    expect(await screen.findByRole('alert')).toBeTruthy();
});

it('lists signed-in devices and signs one out', async () => {
    const calls: MockCall[] = [];
    const { client } = makeClient({
        'GET /ui/config': UI_CONFIG,
        'GET /v1/me/devices': { devices: [{ id: 'abc123abc123abc123abc123', name: 'cairn CLI on ana-laptop', client: 'Cairn CLI',
            signed_in_ms: 1_700_000_000_000, last_used_ms: 1_700_000_100_000, expires_ms: 1_702_000_000_000 }] },
        'DELETE /v1/me/devices/abc123abc123abc123abc123': null,
    }, calls);
    render(<MemoryRouter><CairnProvider client={client} cache={new QueryCache()}><SignedInDevices /></CairnProvider></MemoryRouter>);
    expect(await screen.findByText('cairn CLI on ana-laptop')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Sign out cairn CLI on ana-laptop' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Sign out' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'DELETE')).toBe(true));
    expect(await screen.findByText('No devices are signed in with the command line')).toBeTruthy();
});
