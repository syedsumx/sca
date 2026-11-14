import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { RESOURCE_SAMPLES } from '../../../demo/resourceSamples';
import { CairnApiError } from '../../api/client';
import { CairnProvider } from '../context/CairnProvider';
import { I18nProvider, translate } from '../i18n';
import { HUBS, RESOURCE_SPECS } from '../resources/catalog';
import { formFor } from '../resources/forms';
import { AreaView } from '../resources/HubPage';
import { landsIn, sizeText, type NiFiList } from '../resources/NiFiConnectors';
import { presentError } from '../utils/errors';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const LIST = RESOURCE_SAMPLES['/v1/nifi/deployments'] as NiFiList;
const FLOWS = RESOURCE_SAMPLES['/v1/nifi/flows'] as { flows: Record<string, unknown>[] };

function wrap(routes: Record<string, unknown>, calls: MockCall[], at = '/ingestion?tab=nifi-connectors', locale: 'en' | 'de' = 'en') {
    const { client, cache } = makeClient({ 'GET /ui/config': UI_CONFIG, 'GET /notify/inbox': [], ...routes }, calls);
    return render(
        <MemoryRouter initialEntries={[at]}>
            <I18nProvider initial={locale}>
                <CairnProvider client={client} cache={cache}><AreaView tab="nifi-connectors" /></CairnProvider>
            </I18nProvider>
        </MemoryRouter>,
    );
}

it('is a tab of Ingestion (one home), with generated forms for deployments and flows, answered by the demo', () => {
    expect(HUBS.find((h) => h.route === '/ingestion')!.tabs).toContain('nifi-connectors');
    expect(HUBS.filter((h) => h.tabs.includes('nifi-connectors'))).toHaveLength(1);
    const spec = RESOURCE_SPECS['nifi-connectors'];
    expect(spec.view).toBe('nifi-connectors');
    expect(formFor(spec.actions![0])!.source).toBe('registry:nifi_deployments');
    const flow = formFor(RESOURCE_SPECS['nifi-flows'].actions![0])!;
    expect(flow.source).toBe('registry:nifi_flows');
    expect(flow.fields.map((f) => f.name)).toEqual(expect.arrayContaining(['name', 'deployment', 'definition', 'secrets', 'sink']));
    expect(LIST.deployments.map((d) => d.name)).toEqual(['ingest-dev']);
    expect(landsIn({ sink: { type: 'lakehouse', namespace: 'crm', table: 'contacts' } })).toBe('crm.contacts');
    expect(sizeText({ cpu: 2, memory_mb: 8192, heap_mb: 4096, storage_gb: 50 })).toBe('2 CPU · 8 GB (heap 4 GB) · 50 GB');
});

it('shows deployments with the canvas through Cairn, flows with their state, and stops one and deletes a deployment after asking', async () => {
    const calls: MockCall[] = [];
    wrap({
        'GET /v1/nifi/deployments': LIST, 'GET /v1/nifi/flows': FLOWS,
        'POST /v1/nifi/flows/crm_contacts_to_lake/stop': { name: 'crm_contacts_to_lake', state: 'stopped' },
        'DELETE /v1/nifi/deployments/ingest-dev': {},
    }, calls);
    const card = await screen.findByRole('article', { name: 'ingest-dev' });
    const open = within(card).getByRole('link', { name: 'Open NiFi' });
    expect(open.getAttribute('href')).toBe('https://cairn.example.com/cairn/api/v1/nifi/deployments/ingest-dev/ui/nifi/');
    expect(within(card).getByText('Ready').closest('[data-tone]')?.getAttribute('data-tone')).toBe('ok');
    const flows = await screen.findByRole('region', { name: 'Flows' });
    expect(await within(flows).findByText('crm.contacts')).toBeTruthy();
    expect(within(flows).getByText('Failed').closest('[data-tone]')?.getAttribute('data-tone')).toBe('error');
    fireEvent.click(within(flows).getByRole('button', { name: 'Stop: crm_contacts_to_lake' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path === '/v1/nifi/flows/crm_contacts_to_lake/stop')).toBe(true));
    // Deleting a deployment is in its menu, and asks first.
    fireEvent.click(within(card).getByRole('button', { name: 'More actions for ingest-dev' }));
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Delete' }));
    const ask = await screen.findByRole('dialog', { name: 'Delete ingest-dev and its flows?' });
    expect(calls.some((c) => c.method === 'DELETE')).toBe(false);
    fireEvent.click(within(ask).getByRole('button', { name: 'Delete' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'DELETE' && c.path === '/v1/nifi/deployments/ingest-dev')).toBe(true));
});

it('reads in German, and a Cairn without managed NiFi says it is not connected', async () => {
    wrap({ 'GET /v1/nifi/deployments': LIST, 'GET /v1/nifi/flows': FLOWS }, [], '/ingestion?tab=nifi-connectors', 'de');
    const card = await screen.findByRole('article', { name: 'ingest-dev' });
    expect(within(card).getByRole('link', { name: 'NiFi öffnen' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Neues Deployment' })).toBeTruthy();
    const t = (key: Parameters<typeof translate>[1], vars?: Record<string, string | number>) => translate('en', key, vars);
    const p = presentError(new CairnApiError('HTTP 404', 404, '/v1/nifi/deployments', { detail: 'Not Found' }), t);
    expect(p.kind).toBe('notConnected');
    expect(p.title).toBe('NiFi isn’t connected yet');
    expect(p.connect?.to).toBe('/docs/administration.html#managed-nifi');
});

it('the command palette\'s action opens the generated create form, which posts the deployment', async () => {
    const calls: MockCall[] = [];
    wrap({ 'GET /v1/nifi/deployments': LIST, 'GET /v1/nifi/flows': FLOWS, 'POST /v1/nifi/deployments': { name: 'ops-dev', state: 'ready' } },
        calls, '/ingestion?tab=nifi-connectors&action=New%20deployment');
    const dialog = (await screen.findAllByRole('dialog')).slice(-1)[0];
    fireEvent.change(await within(dialog).findByLabelText(/^Name/), { target: { value: 'ops-dev' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'New deployment' }));
    await waitFor(() => expect(calls.find((c) => c.method === 'POST' && c.path === '/v1/nifi/deployments')).toBeTruthy());
    expect((calls.find((c) => c.method === 'POST')!.body as Record<string, unknown>).name).toBe('ops-dev');
});
