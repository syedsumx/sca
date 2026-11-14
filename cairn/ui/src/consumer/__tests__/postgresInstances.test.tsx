import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { RESOURCE_SAMPLES } from '../../../demo/resourceSamples';
import { CairnProvider } from '../context/CairnProvider';
import { I18nProvider } from '../i18n';
import { HUBS, RESOURCE_SPECS } from '../resources/catalog';
import { HubPage } from '../resources/HubPage';
import { bytes, createBody, toIso, type PgList } from '../resources/PostgresInstances';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const LIST = RESOURCE_SAMPLES['/v1/databases/instances'] as PgList;
const STATUS = {
    name: 'loyalty-pg', state: 'ready', runtime: 'kubernetes', pending: [], health: 'degraded',
    problems: ['standby cairn-pg-loyalty-pg-2 is 300 MB behind'], server_version: '16.6', in_recovery: false,
    connections: { total: 12, active: 3, idle_in_transaction: 0, max: 200, percent: 6 },
    storage: { databases_bytes: 5 * 1024 ** 3, wal_bytes: 512 * 1024 ** 2, limit_bytes: 100 * 1024 ** 3, percent: 5.5 },
    wal_archiving: { enabled: true, ok: true, last_archived_wal: '000000010000000000000042' },
    replication: [{ standby: 'cairn-pg-loyalty-pg-2', state: 'streaming', lag_bytes: 300 * 1024 ** 2 }],
    last_backup: { id: 'b1', finished_ms: 1794200000000 }, runtime_state: { primary: 'cairn-pg-loyalty-pg-1' },
};

function wrap(routes: Record<string, unknown>, calls: MockCall[], at: string) {
    const { client, cache } = makeClient({ 'GET /ui/config': UI_CONFIG, 'GET /notify/inbox': [], ...routes }, calls);
    const hub = HUBS.find((h) => h.route === '/compute')!;
    return render(
        <MemoryRouter initialEntries={[at]}>
            <I18nProvider initial="en">
                <CairnProvider client={client} cache={cache}><HubPage hub={hub} /></CairnProvider>
            </I18nProvider>
        </MemoryRouter>,
    );
}

it('is a tab of Compute & jobs, answered by the demo, with friendly helpers', () => {
    expect(HUBS.find((h) => h.route === '/compute')!.tabs).toContain('postgres-instances');
    expect(RESOURCE_SPECS['postgres-instances'].view).toBe('postgres-instances');
    expect(LIST.instances.map((i) => i.name)).toEqual(['default', 'loyalty-pg']);
    expect(bytes(4096 * 1024 * 1024)).toBe('4.0 GB');
    expect(toIso('2026-11-10T12:00:00')).toMatch(/^2026-11-10T\d\d:00:00\.000Z$/);
    expect(createBody({ name: 'a', replicas: 1, pooling: true, window_day: 'sun' })).toEqual({
        name: 'a', version: '16', cpu: 1, memory_mb: 1024, storage_gb: 10, replicas: 1,
        pooler: { mode: 'transaction', instances: 2 }, maintenance_window: { day: 'sun', start: '02:00', hours: 2 } });
});

it('lists instances, shows one\'s health and backs it up', async () => {
    const calls: MockCall[] = [];
    wrap({
        'GET /v1/databases/instances': LIST,
        'GET /v1/databases/instances/loyalty-pg/status': STATUS,
        'GET /v1/databases/instances/loyalty-pg/backups': { backups: [
            { id: '20261110t020000-ab12cd', schedule: 'daily', state: 'completed', started_ms: 1794200000000,
              finished_ms: 1794200060000, bytes: 3 * 1024 ** 3, verified: 'passed', error: null }] },
        'POST /v1/databases/instances/loyalty-pg/backups': { id: 'b2', state: 'completed' },
    }, calls, '/compute?tab=postgres-instances');
    fireEvent.click(await screen.findByText('loyalty-pg'));
    expect(await screen.findByText('degraded')).toBeTruthy();
    expect(screen.getByText('standby cairn-pg-loyalty-pg-2 is 300 MB behind')).toBeTruthy();
    expect(screen.getByText(/12 of 200/)).toBeTruthy();
    fireEvent.click(screen.getByRole('tab', { name: 'Backups & restore' }));
    expect(await screen.findByText('20261110t020000-ab12cd')).toBeTruthy();
    expect(screen.getByText('passed')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Back up now' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST'
        && c.path === '/v1/databases/instances/loyalty-pg/backups')).toBe(true));
});

it('the command palette\'s action opens the create form, which posts the instance', async () => {
    const calls: MockCall[] = [];
    wrap({ 'GET /v1/databases/instances': LIST, 'POST /v1/databases/instances': { name: 'orders-pg', state: 'creating' } },
        calls, '/compute?tab=postgres-instances&action=New%20instance');
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText('Name'), { target: { value: 'orders-pg' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create' }));
    await waitFor(() => expect(calls.find((c) => c.method === 'POST' && c.path === '/v1/databases/instances')).toBeTruthy());
    const call = calls.find((c) => c.method === 'POST' && c.path === '/v1/databases/instances')!;
    expect(call.body).toMatchObject({ name: 'orders-pg', version: '16', replicas: 0 });
});
