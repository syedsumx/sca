import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { TagSyncAdmin } from '../components/TagSync';
import { CairnProvider } from '../context/CairnProvider';
import { DS, MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const TARGET = {
    id: 'snowflake-prod',
    dialect: 'snowflake',
    platform: 'snowflake',
    connection: 'wh',
    dry_run: false,
    mappings: [{ label: 'urn:li:glossaryTerm:Classification.PII.Email', tag: 'PII', value: 'email' }],
    managed_tags: 3,
    last_run: null,
};
const SQL = 'ALTER TABLE "ANALYTICS"."PUBLIC"."ORDERS" MODIFY COLUMN "EMAIL" SET TAG GOV.TAGS.PII = \'email\'';
const PLAN = {
    target: 'snowflake-prod',
    datasets: 1,
    tagged_objects: 1,
    conflicts: [],
    dry_run: true,
    applied: 0,
    failed: 0,
    operations: [{ op: 'SET', urn: DS, field: 'email', tag: 'PII', value: 'email', sql: SQL }],
};

function wrap(routes: Record<string, unknown>, calls: MockCall[]) {
    const { client } = makeClient({ 'GET /ui/config': UI_CONFIG, ...routes }, calls);
    return render(
        <MemoryRouter>
            <CairnProvider client={client} cache={new QueryCache()}>
                <TagSyncAdmin />
            </CairnProvider>
        </MemoryRouter>,
    );
}

it('previews the warehouse statements, then syncs after confirmation', async () => {
    const calls: MockCall[] = [];
    wrap(
        {
            'GET /governance/tag-sync': [TARGET],
            'POST /governance/tag-sync/snowflake-prod/sync': (body: { dry_run: boolean }) =>
                body.dry_run ? PLAN : { ...PLAN, dry_run: false, applied: 1 },
        },
        calls,
    );
    const card = await screen.findByRole('region', { name: 'Tag sync snowflake-prod' });
    expect(within(card).getByText(/Classification.PII.Email → PII = email/)).toBeTruthy();
    fireEvent.click(within(card).getByRole('button', { name: 'Preview' }));
    expect(await within(card).findByText(SQL)).toBeTruthy();
    fireEvent.click(within(card).getByRole('button', { name: 'Sync now' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Sync' }));
    await waitFor(() =>
        expect(calls.filter((c) => c.method === 'POST').map((c) => (c.body as { dry_run: boolean }).dry_run)).toEqual([
            true,
            false,
        ]),
    );
});

it('plan-only targets cannot be synced, and an empty setup explains itself', async () => {
    const { unmount } = wrap({ 'GET /governance/tag-sync': [{ ...TARGET, dry_run: true }] }, []);
    await screen.findByText('plan only');
    expect(screen.queryByRole('button', { name: 'Sync now' })).toBeNull();
    unmount();
    wrap({ 'GET /governance/tag-sync': [] }, []);
    expect(await screen.findByText(/No warehouse targets yet/)).toBeTruthy();
});
