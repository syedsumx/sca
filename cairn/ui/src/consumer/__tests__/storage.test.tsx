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

const KEYS = [
    { access_key: 'cairnadmin', source: 'configuration', admin: true, read_only: false, buckets: [] },
    {
        access_key: 'ingest',
        source: 'api',
        admin: false,
        read_only: false,
        buckets: ['lake-*'],
        description: 'nightly load',
        created: '2026-10-31T10:00:00.000Z',
    },
];

const CLUSTER = {
    enabled: true,
    node: 'store-0',
    copies: 2,
    erasure_coding: null,
    nodes: [
        {
            name: 'store-0',
            url: 'http://store-0:8333',
            state: 'active',
            alive: true,
            heartbeat_age_ms: 900,
            version: '0.1.0',
            copies: 40,
            bytes: 4 * 1024 ** 3,
            this_node: true,
        },
        {
            name: 'store-1',
            url: 'http://store-1:8333',
            state: 'active',
            alive: false,
            heartbeat_age_ms: 120000,
            version: '0.1.0',
            copies: 38,
            bytes: 3 * 1024 ** 3,
            this_node: false,
        },
    ],
    blobs: { total: 40, healthy: 2, degraded: 38, lost: 0 },
};

function wrap(calls: MockCall[], extra: Record<string, unknown> = {}) {
    const { client } = makeClient(
        {
            'GET /storage/v1/cluster': { enabled: false },
            'GET /ui/config': { ...UI_CONFIG, is_admin: true, storage_enabled: true },
            'GET /storage/v1/buckets': [LAKE, RAW],
            'GET /storage/v1/audit': AUDIT,
            'GET /storage/v1/keys': KEYS,
            'POST /storage/v1/keys': (body: unknown) => ({
                ...KEYS[1],
                ...(body as object),
                access_key: 'CKNEW',
                secret_key: 'the-new-secret-shown-once',
            }),
            'POST /storage/v1/keys/ingest/rotate': { ...KEYS[1], secret_key: 'rotated-secret' },
            'DELETE /storage/v1/keys/ingest': null,
            'GET /storage/v1/audit/verify': {
                valid: true,
                entries_checked: 2,
                head: 'h2',
            },
            'PUT /storage/v1/buckets/raw/quota': (body: unknown) => ({
                ...RAW,
                quota: body,
            }),
            ...extra,
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

it('lists access keys, creates one and shows its secret once, rotates and deletes', async () => {
    const calls: MockCall[] = [];
    wrap(calls);
    const row = (await screen.findByText('ingest')).closest('tr') as HTMLElement;
    expect(within(row).getByText('Created here')).toBeTruthy();
    expect(within(row).getByText('lake-*')).toBeTruthy();
    const fixed = screen.getByText('cairnadmin').closest('tr') as HTMLElement;
    expect(within(fixed).getByText('Configuration')).toBeTruthy();
    expect(within(fixed).queryByText('Rotate secret')).toBeNull(); // configuration keys are changed there
    fireEvent.click(screen.getByText('Create key'));
    fireEvent.change(await screen.findByLabelText('Buckets (comma-separated; * allowed; empty for all)'), {
        target: { value: 'lake-a, lake-b' },
    });
    fireEvent.click(screen.getByText('Read only'));
    fireEvent.click(screen.getByText('Create'));
    expect(await screen.findByText('the-new-secret-shown-once')).toBeTruthy();
    expect(calls.find((c) => c.method === 'POST' && c.path === '/storage/v1/keys')?.body).toEqual({
        buckets: ['lake-a', 'lake-b'],
        read_only: true,
        admin: false,
    });
    fireEvent.click(screen.getByText('Close'));
    fireEvent.click(within(row).getByText('Rotate secret'));
    fireEvent.click(await screen.findByText('OK'));
    expect(await screen.findByText('rotated-secret')).toBeTruthy();
});

it('shows the cluster with node health only when the store runs as one, and drains a node', async () => {
    const calls: MockCall[] = [];
    wrap(calls, {
        'GET /storage/v1/cluster': CLUSTER,
        'POST /storage/v1/cluster/nodes/store-1/drain': { name: 'store-1', state: 'draining' },
        'POST /storage/v1/cluster/repair': { repaired: 38 },
    });
    expect(await screen.findByText('Cluster')).toBeTruthy();
    expect(screen.getByText(/38 stored blobs have fewer copies than configured and 0 cannot be read/)).toBeTruthy();
    const down = screen.getByText('store-1').closest('tr') as HTMLElement;
    expect(within(down).getByText('Down')).toBeTruthy();
    expect(within(screen.getByText('store-0').closest('tr') as HTMLElement).getByText('answering')).toBeTruthy();
    fireEvent.click(within(down).getByText('Drain'));
    fireEvent.click(await screen.findByText('OK'));
    await waitFor(() =>
        expect(calls.some((c) => c.method === 'POST' && c.path === '/storage/v1/cluster/nodes/store-1/drain')).toBe(
            true,
        ),
    );
    fireEvent.click(screen.getByText('Repair now'));
    await waitFor(() => expect(calls.some((c) => c.path === '/storage/v1/cluster/repair')).toBe(true));
});

it('shows no cluster card for a single store', async () => {
    wrap([]);
    expect(await screen.findByText('Access keys')).toBeTruthy();
    expect(screen.queryByText('Cluster')).toBeNull();
});
