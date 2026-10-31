import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { StorageSettings, formatBytes } from '../components/Storage';
import { CairnProvider } from '../context/CairnProvider';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const LAKE = {
    name: 'lakehouse',
    created: '2026-10-31T00:00:00.000Z',
    versioning: 'Enabled',
    usage: { bytes: 5 * 1024 ** 3, objects: 1234 },
    quota: { max_bytes: 10 * 1024 ** 3 },
    object_lock: true,
    encryption_default: true,
    lifecycle: false,
    policy: true,
};
const RAW = {
    ...LAKE,
    name: 'raw',
    versioning: 'Off',
    usage: { bytes: 10, objects: 1 },
    quota: null,
    object_lock: false,
};
const AUDIT = [
    {
        seq: 1,
        id: 'a',
        ts: '2026-10-31T10:00:00+00:00',
        category: 'store',
        action: 'bucket.create',
        actor: 'admin',
        subject: 'raw',
        details: {},
        prev_hash: '0'.repeat(64),
        hash: 'h1',
    },
    {
        seq: 2,
        id: 'b',
        ts: '2026-10-31T10:01:00+00:00',
        category: 'store',
        action: 'quota.put',
        actor: 'admin',
        subject: 'lakehouse',
        details: {},
        prev_hash: 'h1',
        hash: 'h2',
    },
];

function wrap(calls: MockCall[]) {
    const { client } = makeClient(
        {
            'GET /ui/config': { ...UI_CONFIG, is_admin: true, storage_enabled: true },
            'GET /storage/v1/buckets': [LAKE, RAW],
            'GET /storage/v1/audit': AUDIT,
            'GET /storage/v1/audit/verify': {
                valid: true,
                entries_checked: 2,
                head: 'h2',
            },
            'PUT /storage/v1/buckets/raw/quota': (body: unknown) => ({
                ...RAW,
                quota: body,
            }),
        },
        calls,
    );
    render(
        <MemoryRouter>
            <CairnProvider client={client} cache={new QueryCache()}>
                <StorageSettings />
            </CairnProvider>
        </MemoryRouter>,
    );
}

it('formats sizes', () => {
    expect(formatBytes(0)).toBe('0 B');
    expect(formatBytes(1536)).toBe('1.5 KiB');
    expect(formatBytes(5 * 1024 ** 3)).toBe('5.0 GiB');
});

it('lists buckets with usage, quota and settings, sets a quota and verifies the audit chain', async () => {
    const calls: MockCall[] = [];
    wrap(calls);
    const row = (await screen.findAllByText('lakehouse'))
        .find((el) => el.tagName === 'STRONG')
        ?.closest('tr') as HTMLElement;
    expect(within(row).getByText(/5\.0 GiB · 1,234 objects/)).toBeTruthy();
    expect(within(row).getByText(/10 GiB \(50%\)/)).toBeTruthy();
    expect(within(row).getByText('Object lock')).toBeTruthy();
    expect(within(row).getByText('Versioning: Enabled')).toBeTruthy();
    const raw = screen
        .getAllByText('raw')
        .find((el) => el.tagName === 'STRONG')
        ?.closest('tr') as HTMLElement;
    expect(within(raw).getByText('No quota')).toBeTruthy();
    fireEvent.click(within(raw).getByText('Set quota'));
    fireEvent.change(await screen.findByLabelText('Maximum objects'), {
        target: { value: '500' },
    });
    fireEvent.click(screen.getByText('Save'));
    await waitFor(() =>
        expect(calls.some((c) => c.method === 'PUT' && c.path === '/storage/v1/buckets/raw/quota')).toBe(true),
    );
    expect(calls.find((c) => c.method === 'PUT')?.body).toEqual({
        max_objects: 500,
    });
    expect(await screen.findByText('quota.put')).toBeTruthy();
    fireEvent.click(screen.getByText('Verify chain'));
    expect(await screen.findByText('The chain is intact: 2 entries checked.')).toBeTruthy();
});
