import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { PlaybooksAdmin } from '../components/Playbooks';
import { CairnProvider } from '../context/CairnProvider';
import { DS, MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const RUN = {
    id: 'pbr_1',
    playbook: 'incident_triage',
    subject: DS,
    event: { type: 'quality.incident.raised' },
    state: 'PARTIAL',
    created_ms: 1,
    steps: [
        { name: 'root causes', status: 'ok', summary: 'raw.orders (row count dropped 41%)', data: {}, ms: 12 },
        { name: 'people to contact', status: 'ok', summary: 'ana, bob', data: {}, ms: 3 },
        {
            name: 'downstream impact',
            status: 'skipped',
            summary: 'lineage needs the catalog connection',
            data: {},
            ms: 0,
        },
    ],
};
const TRIAGE = {
    id: 'incident_triage',
    name: 'Incident triage',
    description: 'Likely root causes, people to contact and downstream impact.',
    trigger: 'quality.incident.raised',
    enabled: false,
    steps: ['root causes', 'people to contact', 'downstream impact', 'discussion', 'notify owners'],
    last_run: null,
};

it('turns a playbook on, tries it on an asset and shows each step', async () => {
    const calls: MockCall[] = [];
    const { client } = makeClient(
        {
            'GET /ui/config': UI_CONFIG,
            'GET /governance/playbooks': [TRIAGE],
            'GET /governance/playbooks/runs': [RUN],
            'POST /governance/playbooks/incident_triage/enabled': { ...TRIAGE, enabled: true },
            'POST /governance/playbooks/incident_triage/run': RUN,
        },
        calls,
    );
    render(
        <MemoryRouter>
            <CairnProvider client={client} cache={new QueryCache()}>
                <PlaybooksAdmin />
            </CairnProvider>
        </MemoryRouter>,
    );
    expect((await screen.findAllByText('Incident triage')).length).toBeGreaterThan(0);
    expect(screen.getByText(/when a quality incident is raised/)).toBeTruthy();
    fireEvent.click(screen.getByLabelText('Turn on Incident triage'));
    await waitFor(() => expect(calls.find((c) => c.path.endsWith('/enabled'))?.body).toEqual({ enabled: true }));
    fireEvent.change(screen.getByLabelText('Try Incident triage on'), { target: { value: DS } });
    fireEvent.click(screen.getByRole('button', { name: 'Run now' }));
    expect(await screen.findByText('raw.orders (row count dropped 41%)')).toBeTruthy();
    expect(screen.getAllByText('skipped').length).toBeGreaterThan(0);
    expect(calls.find((c) => c.path.endsWith('/run'))?.body).toEqual({ urn: DS });
});
