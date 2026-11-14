import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

import { CairnClient } from '../api/client';
import { CairnClientProvider } from '../api/hooks';
import type { Evaluation, Monitor } from '../api/types';
import { MonitorDetail } from '../quality/MonitorDetail';
import { MONITOR_CONFIG_FIELDS, cleanConfig, partitioned } from '../quality/MonitorEditor';
import { installMatchMedia } from '../consumer/__tests__/setup';

beforeAll(installMatchMedia);

const defaults = (type: keyof typeof MONITOR_CONFIG_FIELDS) =>
    cleanConfig(Object.fromEntries(MONITOR_CONFIG_FIELDS[type].filter((f) => f.defaultValue !== undefined).map((f) => [f.name, f.defaultValue])));

it('new volume and metric monitors default to learned thresholds the API accepts', () => {
    expect(defaults('volume')).toEqual({ source: 'catalog', model: 'learned' });
    expect(defaults('field_metric')).toEqual({ source: 'catalog', metric: 'null_pct', model: 'learned' });
    expect(defaults('freshness')).toEqual({ source: 'catalog' });
    expect(MONITOR_CONFIG_FIELDS.freshness.some((f) => f.name === 'model')).toBe(false);
    // the API reads `connection`, not `connection_id`
    expect(Object.values(MONITOR_CONFIG_FIELDS).flat().some((f) => f.name === 'connection_id')).toBe(false);
    // "Recent baseline" sends no model at all, so the API uses its default
    expect(cleanConfig({ model: '', band: 4 })).toEqual({ band: 4 });
});

it('an alert can be marked as the new normal', async () => {
    const monitor = { id: 'm1', name: 'orders volume' } as Monitor;
    const fail: Evaluation = {
        id: 'e1', monitor_id: 'm1', ts: '2026-10-01T00:00:00Z', metric_value: 300, expected_low: 90, expected_high: 110,
        status: 'FAIL', score: 9, message: 'value 300, expected 90 – 110', feedback: null,
    };
    const posted: unknown[] = [];
    const fetchImpl = (async (url: string, init?: RequestInit) => {
        if (init?.method === 'POST') {
            posted.push([String(url), JSON.parse(String(init.body))]);
            return new Response(JSON.stringify({ ...fail, feedback: 'NEW_NORMAL' }), { status: 200 });
        }
        return new Response(JSON.stringify([fail]), { status: 200 });
    }) as typeof fetch;
    render(
        <CairnClientProvider client={new CairnClient({ fetchImpl })}>
            <MonitorDetail monitor={monitor} />
        </CairnClientProvider>,
    );
    fireEvent.click(await screen.findByRole('button', { name: 'New normal' }));
    await waitFor(() => expect(posted).toEqual([['/cairn/api/quality/evaluations/e1/feedback', { feedback: 'NEW_NORMAL' }]]));
    expect(await screen.findByText('New normal', { selector: '.ant-tag' })).toBeTruthy();
});

describe('a volume monitor counting per partition', () => {
    it('drops the learned model and total-row bounds, and defaults to days', () => {
        expect(partitioned('volume', { source: 'catalog', model: 'learned', band: 3, min_rows: 5, partition_column: 'ts', table: 't', connection: 'wh' }))
            .toEqual({ source: 'sql', partition_column: 'ts', table: 't', connection: 'wh', grain: 'day' });
        expect(partitioned('volume', { model: 'learned', grain: 'hour', min_rows_per_partition: 3 })).toEqual({ model: 'learned' });
        expect(partitioned('freshness', { timestamp_column: 'ts', grain: 'x' })).toEqual({ timestamp_column: 'ts', grain: 'x' });
    });
});
