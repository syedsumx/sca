import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { WorkspacesSettings } from '../components/Workspaces';
import { CairnProvider } from '../context/CairnProvider';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const WS = {
    id: 'finance',
    name: 'Finance',
    description: 'Ledger and revenue',
    domains: ['urn:li:domain:finance'],
    admins: ['urn:li:corpuser:ana'],
    members: ['urn:li:corpGroup:risk'],
    admin_permissions: ['certify', 'edit_docs'],
    member_permissions: ['view'],
    assets: { dataset: 2, dataProduct: 1 },
    unowned: 1,
    owners: 1,
    role: 'admin',
};
const LEDGER = 'urn:li:dataset:(urn:li:dataPlatform:snowflake,fin.ledger,PROD)';

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

const routes = {
    'GET /workspaces': [{ id: 'finance', name: 'Finance', domains: WS.domains, role: 'admin' }],
    'GET /workspaces/finance': WS,
    'GET /workspaces/finance/assets': {
        total: 1,
        items: [{ urn: LEDGER, type: 'dataset', domain: 'urn:li:domain:finance', owners: [] }],
    },
};

it('shows a workspace admin their workspace, a scoped search, and lets them save members', async () => {
    const calls: MockCall[] = [];
    wrap(<WorkspacesSettings isAdmin={false} />, { ...routes, 'PUT /workspaces/finance': WS }, calls);
    expect(await screen.findByText('Ledger and revenue')).toBeTruthy();
    expect(screen.getByText('2 datasets, 1 dataProduct')).toBeTruthy();
    expect(screen.getByText('Search this workspace').getAttribute('href')).toBe(
        '/search?f.domains=urn%3Ali%3Adomain%3Afinance',
    );
    expect(await screen.findByText('no owner')).toBeTruthy();
    expect(screen.getByText('Only a global admin changes domains, admins and permissions.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Remove workspace' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Create workspace' })).toBeNull();
    const save = screen.getByRole('button', { name: 'Save workspace' });
    expect(save.hasAttribute('disabled')).toBe(true);
});

it('lets a global admin create a workspace', async () => {
    const calls: MockCall[] = [];
    wrap(<WorkspacesSettings isAdmin />, { ...routes, 'PUT /workspaces/risk-office': WS }, calls);
    await screen.findByText('Ledger and revenue');
    expect(screen.getByRole('button', { name: 'Remove workspace' })).toBeTruthy();
    fireEvent.change(screen.getByLabelText('Workspace name'), { target: { value: 'Risk Office' } });
    fireEvent.change(screen.getByLabelText('Workspace domains'), { target: { value: 'urn:li:domain:risk' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create workspace' }));
    await waitFor(() =>
        expect(calls.some((c) => c.method === 'PUT' && c.path === '/workspaces/risk-office')).toBe(true),
    );
    const put = calls.find((c) => c.method === 'PUT')!;
    expect(put.body).toEqual({ id: 'risk-office', name: 'Risk Office', domains: ['urn:li:domain:risk'], admins: [] });
});

it('says so when you are in no workspace', async () => {
    wrap(<WorkspacesSettings isAdmin={false} />, { 'GET /workspaces': [] });
    expect(await screen.findByText('You are not in a workspace yet.')).toBeTruthy();
});
