import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { ConsumerLayout, NAV } from '../components/Layout';
import { CairnProvider } from '../context/CairnProvider';
import { I18nProvider } from '../i18n';
import capabilities from '../resources/capabilities.json';
import { HUBS, RESOURCE_SPECS } from '../resources/catalog';
import { CUSTOM_TABS } from '../resources/customTabs';
import { HubPage } from '../resources/HubPage';
import { ResourceView } from '../resources/ResourceView';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';
// @ts-expect-error - a plain ES module script, used here to check the generated file is current
import { parseRoadmap } from '../../../scripts/capabilities.mjs';

beforeAll(installMatchMedia);

function wrap(ui: React.ReactElement, routes: Record<string, unknown>, calls: MockCall[] = [], at = '/') {
    const { client, cache } = makeClient({ 'GET /ui/config': UI_CONFIG, 'GET /notify/inbox': [], ...routes }, calls);
    return render(
        <MemoryRouter initialEntries={[at]}>
            <I18nProvider initial="en">
                <CairnProvider client={client} cache={cache}>{ui}</CairnProvider>
            </I18nProvider>
        </MemoryRouter>,
    );
}

it('the capabilities page data is current with ROADMAP.md', () => {
    const md = readFileSync(join(__dirname, '../../../../ROADMAP.md'), 'utf8');
    expect(parseRoadmap(md)).toEqual(capabilities); // run `node scripts/capabilities.mjs` after editing the roadmap
    expect(capabilities.items.length).toBeGreaterThan(90);
});

it('the roadmap parser keeps a pipe inside code and an escaped pipe in their cell', () => {
    const parse = parseRoadmap;
    const md = ['# Roadmap', '', '## Next', '', '| # | Item | Done | Status |', '|---|---|---|---|',
        '| L1 | **One.** Uses `compute: customer | managed` | flags `a|b` and c \\| d | 🟡 partly `x | y` |', ''].join('\n');
    const [row] = parse(md).items;
    expect(row.status).toBe('partial');
    expect(row.summary).toBe('Uses compute: customer | managed');
    expect(row.done).toBe('flags a|b and c | d');
    expect(row.note).toBe('partly x | y');
});

it('every hub tab names a known area or view, and every hub is in the sidebar', () => {
    const routes = new Set(NAV.map((n) => n.to));
    for (const hub of HUBS) {
        expect(routes.has(hub.route), hub.route).toBe(true);
        for (const tab of hub.tabs) {
            const custom = CUSTOM_TABS[tab];
            const spec = RESOURCE_SPECS[tab];
            expect(custom || spec, `${hub.route}: ${tab}`).toBeTruthy();
            if (custom) continue;
            // An area with no list (a generator, clean rooms opened by name) says so; the rest list from a GET.
            if (spec.path === null) expect(spec.notes, tab).toBeTruthy();
            else expect(spec.path, tab).toMatch(/^\//);
            expect(spec.path ?? '', tab).not.toMatch(/\{/);
        }
    }
});

it('the sidebar groups every section and collapses', async () => {
    wrap(<ConsumerLayout />, {});
    const nav = screen.getByRole('navigation', { name: 'Workspace sections' });
    // Every group is listed; the closed ones open with their heading (L150).
    for (const b of within(nav).getAllByRole('button', { expanded: false })) fireEvent.click(b);
    for (const label of ['Agents', 'Ingestion', 'Security & compliance', 'Platform map', 'Settings']) {
        expect(within(nav).getByText(label)).toBeTruthy();
    }
    expect(within(nav).getByRole('button', { name: 'AI' }).getAttribute('aria-expanded')).toBe('true');
    fireEvent.click(within(nav).getByRole('button', { name: 'Build' }));
    expect(within(nav).queryByRole('link', { name: 'Ingestion' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Collapse sidebar' }));
    expect(screen.getByRole('button', { name: 'Expand sidebar' })).toBeTruthy();
    expect(within(nav).queryByText('Ingestion')).toBeNull();
    expect(within(nav).getByRole('link', { name: 'Ingestion' })).toBeTruthy();
});

it('a resource view lists an area from the API, shows a row, and runs a row action', async () => {
    const calls: MockCall[] = [];
    const spec = RESOURCE_SPECS['outbox'];
    const item = { id: 'ob_1', subject: 'Weekly revenue', kind: 'email', target: ['cfo@northwind.example'], status: 'pending',
        created_ms: Date.now() - 3_600_000, decided_ms: null };
    wrap(<ResourceView spec={spec} />, {
        [`GET ${spec.path}`]: [item],
        'POST /ai/outbox/ob_1/approve': { ...item, status: 'approved' },
    }, calls);
    expect(await screen.findByText('Weekly revenue')).toBeTruthy();
    expect(screen.getByText('Pending').closest('[data-tone]')?.getAttribute('data-state')).toBe('pending');
    fireEvent.click(screen.getByText('Weekly revenue'));
    expect(await screen.findByText('cfo@northwind.example', { selector: '.ant-drawer *' })).toBeTruthy();
    fireEvent.click(within(document.querySelector('.ant-drawer') as HTMLElement).getByRole('button', { name: 'Approve and send' }));
    const dialog = (await screen.findAllByRole('dialog')).slice(-1)[0];
    fireEvent.click(within(dialog).getByRole('button', { name: 'Approve and send' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path === '/ai/outbox/ob_1/approve')).toBe(true));
    expect(await screen.findByText('Cairn answered')).toBeTruthy();
});

it('a refused list says so instead of failing the page', async () => {
    const spec = RESOURCE_SPECS['limits'];
    wrap(<ResourceView spec={spec} />, { [`GET ${spec.path}`]: new Response(JSON.stringify({ detail: 'needs MANAGE_SYSTEM_OPERATIONS' }), { status: 403 }) });
    expect(await screen.findByText('You don’t have permission to see Request limits')).toBeTruthy();
    expect(screen.getByText(/Ask an owner of Request limits, or an admin, to give you access\. It needs MANAGE_SYSTEM_OPERATIONS\./)).toBeTruthy();
});

it('a hub page shows its areas as tabs and loads the open one', async () => {
    const hub = HUBS.find((h) => h.route === '/agents')!;
    const calls: MockCall[] = [];
    wrap(<HubPage hub={hub} />, { 'GET /v1/agent-apps': [], 'GET /ai/scheduled-tasks': [] }, calls, '/agents?tab=scheduled-tasks');
    expect(screen.getByRole('heading', { name: 'Agents' })).toBeTruthy();
    expect(screen.getByRole('tab', { name: 'Agent apps' })).toBeTruthy();
    expect(screen.getByRole('tab', { name: 'Scheduled tasks', selected: true })).toBeTruthy();
    await waitFor(() => expect(calls.map((c) => c.path)).toContain('/ai/scheduled-tasks'));
    expect(calls.map((c) => c.path)).not.toContain('/v1/agent-apps'); // only the open tab loads
});

it('the phone menu takes focus when opened, and Escape closes it and gives focus back to the menu button', async () => {
    wrap(<ConsumerLayout />, {});
    await screen.findByRole('link', { name: 'Cairn home', hidden: true });
    const menu = screen.getAllByRole('button', { hidden: true }).find((b) => b.getAttribute('aria-label') === 'Menu')!;
    expect(menu).toBeTruthy();
    fireEvent.click(menu);
    expect(menu.getAttribute('aria-expanded')).toBe('true');
    await waitFor(() => expect(document.activeElement?.closest('aside')).toBeTruthy());
    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => expect(menu.getAttribute('aria-expanded')).toBe('false'));
    expect(document.activeElement).toBe(menu);
});
