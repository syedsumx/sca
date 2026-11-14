import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { AISystemsAdmin } from '../components/AISystems';
import { CairnProvider } from '../context/CairnProvider';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const ASSESSMENT = {
    name: 'EU AI Act risk assessment',
    restricted_labels: ['PII'],
    questions: [
        {
            id: 'high_risk_use',
            title: 'Is the system used for any of the following?',
            type: 'multi',
            options: [{ value: 'employment', label: 'Employment', tier: 'high' }],
            tier_if_yes: null,
            required_for: [],
        },
        {
            id: 'safety_component',
            title: 'Safety component?',
            type: 'boolean',
            options: [],
            tier_if_yes: 'high',
            required_for: [],
        },
    ],
};
const SYSTEM = {
    id: 'ais_1',
    name: 'CV screening',
    purpose: 'Ranks applicants.',
    owner: 'urn:li:corpuser:bob',
    models: [],
    datasets: ['urn:li:dataset:x'],
    vendor: null,
    data_purpose_id: null,
    answers: { high_risk_use: ['employment'], safety_component: false },
    state: 'IN_REVIEW',
    tier: 'high',
    missing: [],
    violations: [{ dataset: 'urn:li:dataset:x', labels: ['PII.Email'], reason: 'no approved purpose' }],
    decided_by: null,
    decided_ms: null,
    decision_note: null,
    updated_ms: 0,
};
const REPORT = {
    assessment: 'EU AI Act risk assessment',
    total: 1,
    by_tier: { unacceptable: 0, high: 1, limited: 0, minimal: 0 },
    by_state: { IN_REVIEW: 1 },
    high_risk_not_approved: ['ais_1'],
    violations: [
        {
            system: 'ais_1',
            name: 'CV screening',
            state: 'IN_REVIEW',
            dataset: 'urn:li:dataset:x',
            labels: ['PII.Email'],
            reason: 'no approved purpose',
        },
    ],
};

function wrap(routes: Record<string, unknown>, calls: MockCall[], admin = true) {
    const { client } = makeClient({ 'GET /ui/config': { ...UI_CONFIG, is_admin: admin }, ...routes }, calls);
    return render(
        <MemoryRouter>
            <CairnProvider client={client} cache={new QueryCache()}>
                <AISystemsAdmin />
            </CairnProvider>
        </MemoryRouter>,
    );
}

it('registers a system with answers from the configured assessment', async () => {
    const calls: MockCall[] = [];
    wrap(
        {
            'GET /governance/ai-systems': [],
            'GET /governance/ai-systems/assessment': ASSESSMENT,
            'GET /governance/purposes': [],
            'POST /governance/ai-systems': { ...SYSTEM, state: 'DRAFT' },
        },
        calls,
        false,
    );
    fireEvent.click(await screen.findByRole('button', { name: 'Register an AI system' }));
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'CV screening' } });
    fireEvent.change(screen.getByLabelText('Purpose'), { target: { value: 'Ranks applicants.' } });
    fireEvent.change(screen.getByLabelText('Datasets'), { target: { value: 'urn:li:dataset:x\n urn:li:dataset:y' } });
    fireEvent.click(await screen.findByLabelText('Employment'));
    fireEvent.click(screen.getByLabelText('No'));
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() =>
        expect(calls.find((c) => c.method === 'POST')?.body).toMatchObject({
            name: 'CV screening',
            datasets: ['urn:li:dataset:x', 'urn:li:dataset:y'],
            answers: { high_risk_use: ['employment'], safety_component: false },
        }),
    );
});

it('approvers see the inventory and decide systems they do not own', async () => {
    const calls: MockCall[] = [];
    wrap(
        {
            'GET /governance/ai-systems': [SYSTEM],
            'GET /governance/ai-systems/report': REPORT,
            'POST /governance/ai-systems/ais_1/decision': { ...SYSTEM, state: 'REJECTED' },
        },
        calls,
    );
    expect(await screen.findByText(/1 use of restricted data without an approved purpose/)).toBeTruthy();
    const row = await screen.findByLabelText('AI system CV screening');
    expect(within(row).getByText('high risk')).toBeTruthy();
    expect(within(row).getByText('restricted data without purpose')).toBeTruthy();
    fireEvent.change(within(row).getByLabelText('Decision note for CV screening'), { target: { value: 'No DPIA' } });
    fireEvent.click(within(row).getByRole('button', { name: 'Reject' }));
    await waitFor(() =>
        expect(calls.find((c) => c.path.endsWith('/decision'))?.body).toEqual({ approve: false, note: 'No DPIA' }),
    );
});
