import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { ExtensionsAdmin, ExtensionsPage, extensionAssetTabs } from '../components/Extensions';
import { CairnProvider } from '../context/CairnProvider';
import { DS, MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const WS = {
    extension: 'owner-coverage',
    publisher: 'Acme',
    label: 'Owner coverage',
    url: 'https://ext.example/ui',
    placement: 'workspace' as const,
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

it('workspace sections render in a sandboxed frame without same-origin access', async () => {
    wrap(<ExtensionsPage />, { 'GET /extensions/ui': [WS, { ...WS, label: 'On assets', placement: 'asset' }] });
    const frame = (await screen.findByTitle('Owner coverage (Acme)')) as HTMLIFrameElement;
    expect(frame.getAttribute('src')).toBe('https://ext.example/ui');
    expect(frame.getAttribute('sandbox')).toBe('allow-scripts allow-forms allow-popups');
    expect(screen.queryByText('On assets')).toBeNull(); // asset sections live on asset pages
});

it('asset sections get the asset urn', () => {
    const [tab] = extensionAssetTabs([{ ...WS, placement: 'asset', url: 'https://ext.example/a?x=1' }], DS);
    render(<>{tab.children}</>);
    expect(screen.getByTitle('Owner coverage (Acme)').getAttribute('src')).toBe(
        `https://ext.example/a?x=1&urn=${encodeURIComponent(DS)}`,
    );
});

it('admins install a manifest and see the token once', async () => {
    const calls: MockCall[] = [];
    wrap(
        <ExtensionsAdmin />,
        {
            'GET /admin/extensions': [],
            'PUT /admin/extensions': {
                name: 'owner-coverage',
                version: '1.0.0',
                actor: 'urn:li:corpuser:cairn-ext-owner-coverage',
                token: 'cx_owner-coverage.abc',
                signing_secret: 'deadbeef',
            },
        },
        calls,
    );
    expect(await screen.findByText('No extensions installed.')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('Extension manifest'), { target: { value: 'name: owner-coverage' } });
    fireEvent.click(screen.getByRole('button', { name: 'Install or upgrade' }));
    expect(await screen.findByText('cx_owner-coverage.abc')).toBeTruthy();
    expect(screen.getByText(/not shown again/)).toBeTruthy();
    const put = calls.find((c) => c.method === 'PUT');
    expect(put?.body).toBe('name: owner-coverage');
    await waitFor(() =>
        expect(calls.filter((c) => c.path === '/admin/extensions' && c.method === 'GET').length).toBe(2),
    );
});
