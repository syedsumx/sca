import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import React from 'react';

import { RunLogs } from '../../agents/AgentFleet';
import { CairnClientProvider } from '../../api/hooks';
import { CairnProvider } from '../context/CairnProvider';
import { SettingsPage } from '../pages/SettingsPage';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const RUN = {
    run_id: 'run_1',
    task_id: 'task_1',
    pool: 'finance',
    agent_id: 'agt_1',
    status: 'RUNNING',
    attempts: 1,
    started_at: '2026-10-27T03:00:00Z',
    finished_at: null,
    mcps_forwarded: 12,
    mcps_rejected: 1,
    execution_request_urn: null,
};

function settingsRoutes(canManage: boolean, calls: MockCall[] = []) {
    return makeClient(
        {
            'GET /ui/config': { ...UI_CONFIG, can_manage_agents: canManage, gateway_url: 'https://gw.example.com' },
            'GET /notify/settings': {
                channels: { slack: { enabled: false, user_id: null }, teams: { enabled: false, webhook_url: null }, email: { enabled: false }, in_app: { enabled: true } },
                events: {},
            },
            'GET /notify/inbox': [],
            'GET /agents/pools': [{ name: 'finance', description: '', allowed_platforms: ['postgres'], max_concurrency: 2, agents_online: 1 }],
            'GET /agents': [],
            'GET /agents/runs': [RUN],
            'POST /agents/tasks/task_1/cancel': { task_id: 'task_1', status: 'CANCELLED', was: 'RUNNING' },
            'GET /agents/tasks/task_1/logs': { task_id: 'task_1', run_id: 'run_1', status: 'RUNNING', lines: ['started'], next: 1, done: false },
        },
        calls,
    );
}

describe('Remote agents in workspace settings', () => {
    it('is offered only to people who manage ingestion', async () => {
        const { client, cache } = settingsRoutes(false);
        render(
            <CairnProvider client={client} cache={cache}>
                <SettingsPage />
            </CairnProvider>,
        );
        await screen.findByRole('tab', { name: 'Embeds' });
        await waitFor(() => expect(cache.get('/ui/config')?.data).toBeTruthy());
        expect(screen.queryByRole('tab', { name: 'Remote agents' })).toBeNull();
    });

    it('lists runs, cancels one and opens its live log', async () => {
        const calls: MockCall[] = [];
        const { client, cache } = settingsRoutes(true, calls);
        render(
            <CairnProvider client={client} cache={cache}>
                <SettingsPage />
            </CairnProvider>,
        );
        fireEvent.click(await screen.findByRole('tab', { name: 'Remote agents' }));
        fireEvent.click(await screen.findByRole('tab', { name: 'Runs' }));
        const row = (await screen.findByText('task_1')).closest('tr') as HTMLElement;
        fireEvent.click(within(row).getByRole('button', { name: 'Cancel' }));
        fireEvent.click(await screen.findByRole('button', { name: 'OK' }));
        await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path === '/agents/tasks/task_1/cancel')).toBe(true));
        await waitFor(() => expect(within(row).getByText('CANCELLED')).toBeTruthy());
        expect(within(row).queryByRole('button', { name: 'Cancel' })).toBeNull();
        fireEvent.click(within(row).getByRole('button', { name: 'Logs' }));
        expect((await screen.findByLabelText('Run log')).textContent).toContain('started');
    });
});

describe('RunLogs', () => {
    it('appends new lines and stops polling when the run is done', async () => {
        const pages = [
            { status: 'RUNNING', lines: ['--- attempt 1 ---', 'connecting'], next: 2, done: false },
            { status: 'RUNNING', lines: ['read 10 tables'], next: 3, done: false },
            { status: 'SUCCESS', lines: ['finished: SUCCESS'], next: 4, done: true },
        ];
        const afters: string[] = [];
        const { client } = makeClient({
            'GET /agents/tasks/t1/logs': (_b: unknown, url: URL) => {
                afters.push(url.searchParams.get('after') ?? '');
                return { task_id: 't1', run_id: 'r', ...(pages.shift() ?? pages[0]) };
            },
        });
        render(
            <CairnClientProvider client={client}>
                <RunLogs taskId="t1" pollIntervalMs={5} />
            </CairnClientProvider>,
        );
        await waitFor(() => expect(screen.getByLabelText('Run log').textContent).toContain('finished: SUCCESS'));
        const text = screen.getByLabelText('Run log').textContent ?? '';
        expect(text.indexOf('connecting')).toBeLessThan(text.indexOf('read 10 tables'));
        expect(afters).toEqual(['0', '2', '3']);
        await new Promise((r) => setTimeout(r, 30));
        expect(afters).toHaveLength(3);                       // done: no more polling
        expect(screen.getByText('SUCCESS')).toBeTruthy();
    });
});
