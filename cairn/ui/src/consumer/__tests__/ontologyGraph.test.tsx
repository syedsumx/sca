import { render, screen } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { RESOURCE_SAMPLES } from '../../../demo/resourceSamples';
import { CairnProvider } from '../context/CairnProvider';
import { I18nProvider } from '../i18n';
import { HUBS, RESOURCE_SPECS } from '../resources/catalog';
import { HubPage } from '../resources/HubPage';
import { layout, type OntologyGraphData } from '../resources/OntologyGraph';
import { UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const graph = RESOURCE_SAMPLES['/v1/ontology/graph'] as OntologyGraphData;

it('lays terms on a circle and draws a line per relationship between their tables', () => {
    const laid = layout(graph);
    expect(laid.terms.map((t) => t.label)).toEqual(['Purchase Order', 'Customer', 'Product']);
    expect(laid.links).toHaveLength(2);
    expect(laid.links.find((l) => l.label === 'buyer_ref = id')?.observed).toBe(true);
    expect(laid.links.find((l) => l.label === 'customer_id = id')?.observed).toBe(false);
});

it('the Semantic models hub has the ontology tabs and draws the graph', async () => {
    const hub = HUBS.find((h) => h.route === '/semantic')!;
    for (const tab of ['ontology-terms', 'ontology-relationships', 'ontology-graph', 'ontology-proposals', 'ontology-runs']) {
        expect(hub.tabs).toContain(tab);
        expect(RESOURCE_SAMPLES[RESOURCE_SPECS[tab].path as string], tab).toBeTruthy();   // the demo answers every tab
    }
    const { client, cache } = makeClient({ 'GET /ui/config': UI_CONFIG, 'GET /notify/inbox': [], 'GET /v1/ontology/graph': graph });
    render(
        <MemoryRouter initialEntries={['/semantic?tab=ontology-graph']}>
            <I18nProvider initial="en">
                <CairnProvider client={client} cache={cache}><HubPage hub={hub} /></CairnProvider>
            </I18nProvider>
        </MemoryRouter>,
    );
    expect(await screen.findByRole('img', { name: 'Ontology graph of terms and relationships' })).toBeTruthy();
    expect(screen.getByText('Customer')).toBeTruthy();
    expect(screen.getByText(/3 terms, 2 relationships/)).toBeTruthy();
});
