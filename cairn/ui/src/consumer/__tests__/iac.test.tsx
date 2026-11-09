import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { RESOURCE_SAMPLES } from '../../../demo/resourceSamples';
import { CairnProvider } from '../context/CairnProvider';
import { I18nProvider } from '../i18n';
import { RESOURCE_SPECS } from '../resources/catalog';
import { IacPanel } from '../resources/IacPanel';
import { ResourceView } from '../resources/ResourceView';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

function wrap(ui: React.ReactElement, routes: Record<string, unknown>, calls: MockCall[] = []) {
    const { client, cache } = makeClient({ 'GET /ui/config': UI_CONFIG, 'GET /notify/inbox': [], ...routes }, calls);
    return render(
        <MemoryRouter>
            <I18nProvider initial="en">
                <CairnProvider client={client} cache={cache}>{ui}</CairnProvider>
            </I18nProvider>
        </MemoryRouter>,
    );
}

const SCHEMA = RESOURCE_SAMPLES['/v1/iac/schema'];
const INVENTORY = RESOURCE_SAMPLES['/v1/iac/inventory'];
const EXPORT = RESOURCE_SAMPLES['/v1/iac/export'] as string;

it('lists every resource type with what this workspace holds, and exports the workspace', async () => {
    const calls: MockCall[] = [];
    wrap(<IacPanel />, { 'GET /v1/iac/schema': SCHEMA, 'GET /v1/iac/inventory': INVENTORY, 'GET /v1/iac/export': EXPORT }, calls);
    expect(await screen.findByText('cairn_pipe')).toBeTruthy();
    expect(screen.getByText('cairn_secret')).toBeTruthy();
    expect(screen.getByText('write-only secret')).toBeTruthy();                // a secret's value never comes back
    await waitFor(() => expect(screen.getAllByText('off here').length).toBe(1));   // data apps are off in the sample
    expect(screen.getAllByRole('link', { name: 'Docs' })[0].getAttribute('href')).toContain('#infrastructure-as-code');
    fireEvent.click(screen.getByRole('button', { name: 'Export as Terraform' }));
    const text = await screen.findByLabelText('Exported code');
    await waitFor(() => expect((text as HTMLTextAreaElement).value).toContain('resource "cairn_pipe"'));
    const call = calls.find((c) => c.path === '/v1/iac/export');
    expect(call?.url.searchParams.get('format')).toBe('terraform');
});

it('a resource\'s details copy it as Terraform or YAML, for the person asking', async () => {
    const calls: MockCall[] = [];
    const spec = RESOURCE_SPECS.pipes;
    expect(spec.iac).toEqual({ kind: 'pipes', id: 'id' });
    const pipe = { id: 'p1', name: 'orders-files', bucket: 'landing', format: 'csv', target: 'raw.orders' };
    wrap(<ResourceView spec={spec} />, { 'GET /v1/pipes': [pipe], 'GET /v1/iac/export': 'bundle: exported\npipes: []\n' }, calls);
    fireEvent.click(await screen.findByText('orders-files'));
    fireEvent.click(await screen.findByRole('button', { name: 'Copy as YAML' }));
    const text = await screen.findByLabelText('Exported code');
    await waitFor(() => expect((text as HTMLTextAreaElement).value).toContain('bundle: exported'));
    const call = calls.find((c) => c.path === '/v1/iac/export');
    expect(Object.fromEntries(call!.url.searchParams)).toEqual({ kind: 'pipes', id: 'p1', format: 'yaml' });
});

it('every area marked as a registry type names one the sample registry or the server knows', () => {
    const marked = Object.values(RESOURCE_SPECS).filter((s) => s.iac);
    expect(marked.length).toBeGreaterThan(20);
    for (const s of marked) expect(s.iac!.kind).toMatch(/^[a-z_]+$/);
});
