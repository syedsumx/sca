import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { AiAgentsSettings } from '../components/AiAgents';
import { CairnProvider } from '../context/CairnProvider';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const DESKTOP = {
    id: 'agt_desktop',
    name: 'Claude Desktop',
    kind: 'oauth',
    owner: null,
    scopes: ['read', 'query_data', 'propose'],
    granted_scopes: ['read', 'propose'],
    redirect_uris: ['http://127.0.0.1:33418/callback'],
    ai_system: null,
    enabled: true,
    created_by: 'dynamic-registration',
    created_ms: 1,
    since_ms: 1,
    last_used_ms: null,
};
const BOT = {
    ...DESKTOP,
    id: 'agt_bot',
    name: 'Nightly doc bot',
    kind: 'key',
    owner: 'urn:li:corpuser:ana',
    scopes: ['read'],
};

function wrap(isAdmin: boolean, routes: Record<string, unknown>, calls: MockCall[] = []) {
    const { client } = makeClient({ 'GET /ui/config': UI_CONFIG, ...routes }, calls);
    return render(
        <MemoryRouter>
            <CairnProvider client={client} cache={new QueryCache()}>
                <AiAgentsSettings isAdmin={isAdmin} />
            </CairnProvider>
        </MemoryRouter>,
    );
}

it('lists the agents acting for me with what they may do, and disconnects one', async () => {
    const calls: MockCall[] = [];
    wrap(false, { 'GET /me/agents': [DESKTOP], 'DELETE /me/agents/agt_desktop': { revoked: 2 } }, calls);
    expect(await screen.findByText('Claude Desktop')).toBeTruthy();
    expect(screen.getByText('propose')).toBeTruthy();
    expect(screen.queryByText('query data')).toBeNull(); // granted scopes, not the client's maximum
    expect(screen.queryByText('All agents')).toBeNull(); // not an administrator
    fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }));
    fireEvent.click(await screen.findByRole('button', { name: 'OK' }));
    await waitFor(() =>
        expect(calls.some((c) => c.method === 'DELETE' && c.path === '/me/agents/agt_desktop')).toBe(true),
    );
});

it('administrators create an automation key shown once, and change scopes', async () => {
    const calls: MockCall[] = [];
    wrap(
        true,
        {
            'GET /me/agents': [],
            'GET /admin/agents': [BOT],
            'POST /admin/agents': { ...BOT, id: 'agt_new', name: 'Steward bot', key: 'ca_agt_new.secret' },
            'PATCH /admin/agents/agt_bot': { ...BOT, scopes: ['read', 'write'] },
        },
        calls,
    );
    expect(await screen.findByText('Nightly doc bot')).toBeTruthy();
    expect(screen.getByText(/acts as urn:li:corpuser:ana/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText('Agent name'), { target: { value: 'Steward bot' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create key' }));
    expect(await screen.findByText('ca_agt_new.secret')).toBeTruthy();
    expect(calls.find((c) => c.method === 'POST' && c.path === '/admin/agents')?.body).toEqual({
        name: 'Steward bot',
        scopes: ['read'],
    });
    const group = screen.getByLabelText('Scopes of Nightly doc bot');
    fireEvent.click(group.querySelectorAll('input[type=checkbox]')[3]);
    await waitFor(() => expect(calls.find((c) => c.method === 'PATCH')?.body).toEqual({ scopes: ['read', 'write'] }));
});
