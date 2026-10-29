import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { AutomationsAdmin, draftToAutomation } from '../components/Automations';
import { CairnProvider } from '../context/CairnProvider';
import { DS, MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const AUTO = {
    id: 'auto_1',
    name: 'Finance domain',
    enabled: true,
    created_by: 'x',
    identity: 'urn:li:corpuser:cairn-automation-auto_1',
    spec: {
        name: 'Finance domain',
        select: {},
        every_minutes: 60,
        actions: [{ type: 'set_domain', value: 'urn:li:domain:finance' }],
    },
    last_run: null,
};
const RUN = {
    id: 'arun_1',
    automation_id: 'auto_1',
    started_ms: 1,
    trigger: 'manual',
    matched: 3,
    changed: 2,
    batch_id: 'b',
    state: 'APPLIED',
    detail: {},
};

function wrap(routes: Record<string, unknown>, calls: MockCall[]) {
    const { client } = makeClient({ 'GET /ui/config': UI_CONFIG, ...routes }, calls);
    return render(
        <MemoryRouter>
            <CairnProvider client={client} cache={new QueryCache()}>
                <AutomationsAdmin />
            </CairnProvider>
        </MemoryRouter>,
    );
}

it('turns the builder into one condition and actions', () => {
    const spec = draftToAutomation({
        name: ' Fin ',
        nameContains: 'finance.',
        platform: 'snowflake',
        domain: '',
        classification: '',
        tag: '',
        advanced: '',
        everyMinutes: 30,
        actions: [
            { type: 'set_domain', value: 'finance' },
            { type: 'notify', value: 'ana, bob' },
            { type: 'add_tag', value: ' ' },
        ],
    });
    expect(spec).toEqual({
        name: 'Fin',
        triggers: ['schedule'],
        every_minutes: 30,
        select: {
            all: [
                { attr: 'resource.urn', op: 'contains', value: 'finance.' },
                { attr: 'resource.platform', op: 'eq', value: 'snowflake' },
            ],
        },
        actions: [
            { type: 'set_domain', value: 'finance' },
            { type: 'notify', recipients: ['ana', 'bob'] },
        ],
    });
    const advanced = draftToAutomation({
        name: 'x',
        nameContains: 'ignored',
        platform: '',
        domain: '',
        classification: '',
        tag: '',
        advanced: '{"attr": "resource.platform", "op": "eq", "value": "dbt"}',
        everyMinutes: 60,
        actions: [],
    });
    expect(advanced.select).toEqual({ attr: 'resource.platform', op: 'eq', value: 'dbt' });
    const onEvent = draftToAutomation({
        name: 'PII restricted',
        nameContains: '',
        platform: '',
        domain: '',
        classification: '',
        tag: '',
        advanced: '',
        everyMinutes: 60,
        triggers: ['pii.detected', 'dataset.created'],
        actions: [
            { type: 'add_tag', value: 'restricted' },
            { type: 'ai_steward', value: '' },
        ],
    });
    expect(onEvent.triggers).toEqual(['pii.detected', 'dataset.created']);
    expect(onEvent.actions).toEqual([{ type: 'add_tag', value: 'restricted' }, { type: 'ai_steward' }]);
});

it('previews matches, creates, runs and undoes', async () => {
    const calls: MockCall[] = [];
    wrap(
        {
            'GET /governance/automations': [AUTO],
            'POST /governance/automations/preview': {
                matched: 3,
                would_change: 1,
                truncated: false,
                sample_matched: [DS],
                changes: [{ urn: DS, field: '', what: 'domain', from: null, to: 'urn:li:domain:finance' }],
            },
            'POST /governance/automations': AUTO,
            'POST /governance/automations/auto_1/run': RUN,
            'GET /governance/automations/auto_1/runs': [RUN],
            'POST /governance/automations/runs/arun_1/undo': { ...RUN, state: 'UNDONE' },
        },
        calls,
    );
    fireEvent.click(await screen.findByRole('button', { name: 'New automation' }));
    fireEvent.change(screen.getByLabelText('Automation name'), { target: { value: 'Finance domain' } });
    fireEvent.change(screen.getByLabelText('Name contains'), { target: { value: 'finance.' } });
    fireEvent.change(screen.getByLabelText('Action 1 value'), { target: { value: 'finance' } });
    fireEvent.click(screen.getByRole('button', { name: 'Preview matches' }));
    expect(await screen.findByText(/3 dataset\(s\) match/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Create automation' }));
    await waitFor(() =>
        expect(calls.some((c) => c.method === 'POST' && c.path === '/governance/automations')).toBe(true),
    );
    const created = calls.find((c) => c.method === 'POST' && c.path === '/governance/automations')!.body as {
        actions: unknown[];
    };
    expect(created.actions).toEqual([{ type: 'set_domain', value: 'finance' }]);

    const row = (await screen.findAllByText('Finance domain'))
        .map((el) => el.closest('tr'))
        .find(Boolean) as HTMLElement;
    fireEvent.click(within(row).getByRole('button', { name: 'Run now' }));
    await waitFor(() => expect(calls.some((c) => c.path === '/governance/automations/auto_1/run')).toBe(true));
    fireEvent.click(within(row).getByRole('button', { name: 'Runs' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Undo' })); // in the runs table
    const buttons = await screen.findAllByRole('button', { name: 'Undo' });
    fireEvent.click(buttons[buttons.length - 1]); // the confirmation
    await waitFor(() => expect(calls.some((c) => c.path === '/governance/automations/runs/arun_1/undo')).toBe(true));
});
