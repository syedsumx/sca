import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import path from 'node:path';
import React from 'react';
import { MemoryRouter, Route } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { ConsumerLayout, NAV_GROUPS } from '../components/Layout';
import { CairnProvider } from '../context/CairnProvider';
import { CapabilityState, holds, routeAllowed, visibleTabs } from '../guide/capabilities';
import { docsHref, helpAnchorFor, helpFor, hubTab, rememberHubTab } from '../guide/help';
import HELP from '../guide/help.generated.json';
import { arrangeNav, togglePin } from '../guide/nav';
import {
    STEPS, detectDone, normaliseUi, progress, roleFromProfile, rowsOf, stepsForEvent, stepsVisited, withDone,
} from '../guide/onboarding';
import { GetStartedPage } from '../guide/GetStarted';
import { fuzzyScore, moveActive, pushRecent, rank, readRecent } from '../guide/palette';
import { crumbsFor } from '../guide/PageHeader';
import { I18nProvider } from '../i18n';
import { RESOURCE_SPECS } from '../resources/catalog';
import { HUBS } from '../resources/hubs';
import { DS, MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);
afterEach(() => window.localStorage.clear());

const ME = 'urn:li:corpuser:ana';
const ADMIN_CAPS = { actor: ME, is_admin: true, privileges: { MANAGE_POLICIES: true, MANAGE_SYSTEM_OPERATIONS: true,
    MANAGE_GLOBAL_SETTINGS: true }, features: { onboarding: true } };
const PERSON_CAPS = { actor: ME, is_admin: false, privileges: { MANAGE_POLICIES: false, MANAGE_SYSTEM_OPERATIONS: false,
    MANAGE_GLOBAL_SETTINGS: false }, features: { onboarding: true } };
const ready = (caps: typeof ADMIN_CAPS): CapabilityState => ({ caps, loading: false, error: false });

// ---- palette ranking and keys ------------------------------------------------------------------------------
describe('palette ranking', () => {
    const items = [
        { id: 'a', kind: 'page' as const, label: 'SQL', to: '/sql' },
        { id: 'b', kind: 'page' as const, label: 'SQL workflows', to: '/sql/workflows' },
        { id: 'c', kind: 'tab' as const, label: 'Trust & security › Secret scopes', to: '/trust?tab=secret-scopes' },
        { id: 'd', kind: 'action' as const, label: 'Create scope', detail: 'Trust & security › Secret scopes', to: '/trust?tab=secret-scopes&action=x' },
        { id: 'e', kind: 'page' as const, label: 'Dashboards', to: '/dashboards', keywords: 'charts boards' },
        { id: 'f', kind: 'recent' as const, label: 'SQL', to: '/sql' },
    ];

    it('scores a whole word above letters in order, and the start of a word above the middle', () => {
        expect(fuzzyScore('sql', 'SQL')!).toBeGreaterThan(fuzzyScore('sql', 'SQL workflows')!);
        expect(fuzzyScore('work', 'SQL workflows')!).toBeGreaterThan(fuzzyScore('orkf', 'SQL workflows')!);
        expect(fuzzyScore('sqwf', 'SQL workflows')).not.toBeNull();
        expect(fuzzyScore('xyz', 'SQL workflows')).toBeNull();
        expect(fuzzyScore('', 'anything')).toBe(0);
        expect(fuzzyScore('creme', 'Crème brûlée')).not.toBeNull();         // accents don't matter
    });

    it('ranks the best match first, finds several words anywhere, matches keywords, and lists a route once', () => {
        expect(rank(items, 'sql').map((i) => i.id)).toEqual(['a', 'b']);
        expect(rank(items, 'sqwf')[0].id).toBe('b');
        expect(rank(items, 'secret scope').map((i) => i.id)).toEqual(['c', 'd']);
        expect(rank(items, 'charts')[0].id).toBe('e');
        expect(rank(items, 'nothing like it')).toEqual([]);
    });

    it('moves with the arrow keys, wrapping, and with Home and End', () => {
        expect(moveActive('ArrowDown', 0, 3)).toBe(1);
        expect(moveActive('ArrowDown', 2, 3)).toBe(0);
        expect(moveActive('ArrowUp', 0, 3)).toBe(2);
        expect(moveActive('End', 0, 3)).toBe(2);
        expect(moveActive('Home', 2, 3)).toBe(0);
        expect(moveActive('Enter', 1, 3)).toBeNull();
        expect(moveActive('ArrowDown', 0, 0)).toBeNull();
    });

    it('remembers recent pages on this device, newest first, without repeats', () => {
        pushRecent('/sql', 'SQL');
        pushRecent('/flows', 'Flows');
        pushRecent('/sql', 'SQL');
        expect(readRecent()).toEqual([{ to: '/sql', label: 'SQL' }, { to: '/flows', label: 'Flows' }]);
    });
});

// ---- onboarding -----------------------------------------------------------------------------------------------
describe('onboarding steps', () => {
    it('has a checklist per role, each step linking to a page', () => {
        expect(STEPS.analyst.map((s) => s.id)).toEqual(['find-dataset', 'run-query', 'save-query', 'build-dashboard', 'ask-question']);
        expect(STEPS.engineer.map((s) => s.id)).toEqual(['connect-source', 'create-flow', 'schedule', 'quality-check']);
        expect(STEPS.steward.map((s) => s.id)).toEqual(['review-proposals', 'glossary-term', 'set-owner', 'review-requests']);
        expect(STEPS.admin.map((s) => s.id)).toEqual(['sso', 'groups', 'policies', 'ai-gateway', 'budgets']);
        for (const steps of Object.values(STEPS)) for (const s of steps) expect(s.to).toMatch(/^\//);
    });

    it('counts progress, and completes steps by visit and by event', () => {
        const p = progress('analyst', ['run-query', 'save-query', 'unknown']);
        expect([p.done, p.total, p.complete]).toEqual([2, 5, false]);
        expect(progress('analyst', STEPS.analyst.map((s) => s.id)).complete).toBe(true);
        expect(progress(null, []).total).toBe(0);
        expect(stepsVisited('analyst', '/asset/urn%3Ali%3Adataset%3Ax')).toEqual(['find-dataset']);
        expect(stepsVisited('analyst', '/sql')).toEqual([]);
        expect(stepsVisited('explorer', '/discover')).toEqual(['discover']);
        expect(stepsForEvent('analyst', 'ask')).toEqual(['ask-question']);
        expect(stepsForEvent('admin', 'ask')).toEqual([]);
        expect(withDone(['a'], ['a'])).toEqual(['a']);
        expect(withDone(['a'], ['b', 'a'])).toEqual(['a', 'b']);
    });

    it('detects done steps from the API as the person, and a failing check is not done', async () => {
        const calls: MockCall[] = [];
        const { client } = makeClient({
            'GET /sql/history': [{ id: 'r1' }],
            'GET /sql/saved': [{ id: 'q1', owner: 'urn:li:corpuser:bob' }, { id: 'q2', owner: ME }],
            'GET /sql/dashboards': [{ id: 'd1', owner: 'urn:li:corpuser:bob' }],
        }, calls);
        expect(await detectDone(client, ME, 'analyst', [])).toEqual(['run-query', 'save-query']);
        expect(await detectDone(client, ME, 'analyst', ['run-query'])).toEqual(['save-query']);
        // Nothing answers for an engineer's checks here (404s): nothing is done.
        expect(await detectDone(client, ME, 'engineer', [])).toEqual([]);
        expect(calls.every((c) => c.method === 'GET')).toBe(true);
        expect(rowsOf({ proposals: [{ id: 1 }] })).toEqual([{ id: 1 }]);
    });

    it('reads the profile', () => {
        expect(roleFromProfile('Financial analyst')).toBe('analyst');
        expect(roleFromProfile('Senior data engineer')).toBe('engineer');
        expect(roleFromProfile('Data steward, finance')).toBe('steward');
        expect(roleFromProfile('Platform admin')).toBe('admin');
        expect(roleFromProfile('')).toBeNull();
        expect(normaliseUi(undefined).onboarding).toEqual({ role: null, done: [], dismissed: false, asked: false });
        expect(normaliseUi({ onboarding: { role: 'wizard', done: ['a', 3] } }).onboarding).toMatchObject({ role: null, done: ['a'] });
    });
});

// ---- navigation by capabilities, then role and the person's choices --------------------------------------------
describe('navigation', () => {
    const loading: CapabilityState = { caps: undefined, loading: true, error: false };
    const failed: CapabilityState = { caps: undefined, loading: false, error: true };

    it('fails closed: what needs a privilege is offered only once the person is known to hold it', () => {
        expect(holds(undefined, failed)).toBe(true);
        expect(holds(['MANAGE_POLICIES'], loading)).toBe(false);
        expect(holds(['MANAGE_POLICIES'], failed)).toBe(false);
        expect(holds(['MANAGE_POLICIES'], ready(PERSON_CAPS))).toBe(false);
        expect(holds(['MANAGE_POLICIES'], ready(ADMIN_CAPS))).toBe(true);
    });

    it('hides admin-only pages and hub tabs from people without the privilege', () => {
        const trust = HUBS.find((h) => h.route === '/trust')!;
        const usage = HUBS.find((h) => h.route === '/usage')!;
        expect(visibleTabs(trust, ready(ADMIN_CAPS))).toContain('residency');
        expect(visibleTabs(trust, ready(PERSON_CAPS))).not.toContain('residency');
        expect(visibleTabs(trust, ready(PERSON_CAPS))).toContain('secret-scopes');
        expect(visibleTabs(usage, ready(PERSON_CAPS))).toContain('my-ai-usage');
        expect(visibleTabs(usage, ready(PERSON_CAPS))).not.toContain('ai-usage');
        expect(routeAllowed('/governance', HUBS, ready(PERSON_CAPS))).toBe(false);
        expect(routeAllowed('/governance', HUBS, ready(ADMIN_CAPS))).toBe(true);
        expect(routeAllowed('/sql', HUBS, failed)).toBe(true);
        // A hub with no usable tab is left out.
        const adminOnly = { ...trust, route: '/x', tabs: ['residency', 'siem'] };
        expect(routeAllowed('/x', [adminOnly], ready(PERSON_CAPS))).toBe(false);
    });

    it('arranges the sidebar by role, folds what the role rarely needs, and applies pins and hides', () => {
        const groups = NAV_GROUPS.map((g) => ({ label: g.label as string, items: g.items }));
        const all = () => true;
        const none = { pinned: [], hidden: [], show_all: false };
        const plain = arrangeNav(groups, all, null, none);
        expect(plain.forYou).toEqual([]);
        expect(plain.folded).toBe(0);
        const analyst = arrangeNav(groups, all, 'analyst', none);
        expect(analyst.forYou.map((i) => i.to)).toEqual(['/search', '/sql', '/dashboards', '/sql/metrics']);
        expect(analyst.groups.map((g) => g.label)).not.toContain('nav.group.build');
        expect(analyst.folded).toBeGreaterThan(0);
        const mine = arrangeNav(groups, all, 'analyst', togglePin({ ...none, hidden: ['/sql/metrics'] }, '/flows'));
        expect(mine.forYou.map((i) => i.to)).toEqual(['/search', '/sql', '/dashboards', '/flows']);
        expect(mine.groups.flatMap((g) => g.items).map((i) => i.to)).not.toContain('/sql/metrics');
        const everything = arrangeNav(groups, all, 'analyst', { pinned: [], hidden: ['/sql/metrics'], show_all: true });
        expect(everything.folded).toBe(0);
        expect(everything.groups.flatMap((g) => g.items)).toHaveLength(NAV_GROUPS.flatMap((g) => g.items).length);
        // Show everything never shows what capabilities leave out.
        const noGov = arrangeNav(groups, (i) => i.to !== '/governance', 'admin', { ...none, show_all: true });
        expect(noGov.groups.flatMap((g) => g.items).map((i) => i.to)).not.toContain('/governance');
        expect(noGov.forYou.map((i) => i.to)).not.toContain('/governance');
    });

    it('builds breadcrumbs for deep pages only', () => {
        const labels = { search: 'Search', marketplace: 'Data marketplace', ontology: 'Ontology' };
        const title = (k: string) => RESOURCE_SPECS[k]?.title;
        expect(crumbsFor('/trust', '?tab=secret-scopes', labels, title)).toEqual([
            { label: 'Trust & security', to: '/trust' }, { label: 'Secret scopes' }]);
        expect(crumbsFor('/trust', '', labels, title)).toBeNull();
        expect(crumbsFor(`/asset/${encodeURIComponent('urn:li:dataset:(urn:li:dataPlatform:sqlite,main.orders,PROD)')}`, '', labels, title))
            .toEqual([{ label: 'Search', to: '/search' }, { label: 'main.orders' }]);
        expect(crumbsFor('/sql', '', labels, title)).toBeNull();
    });
});

// ---- contextual help -----------------------------------------------------------------------------------------
describe('help lookup', () => {
    it('finds the docs section for a hub tab, a page, and a deep page', () => {
        expect(helpAnchorFor('/trust', '?tab=secret-scopes', HUBS, RESOURCE_SPECS)).toBe(RESOURCE_SPECS['secret-scopes'].docs);
        expect(helpAnchorFor('/sql', '', HUBS, RESOURCE_SPECS)).toBe('administration.html#sql-workspace');
        expect(helpAnchorFor('/asset/urn%3Ax', '', HUBS, RESOURCE_SPECS)).toBe('getting-started.html#understand-an-asset');
        expect(helpAnchorFor('/nowhere', '', HUBS, RESOURCE_SPECS)).toBeNull();
        expect(docsHref('governance.html#consent')).toBe('/docs/governance.html#consent');
    });

    it('a hub opens on the tab used last', () => {
        const trust = HUBS.find((h) => h.route === '/trust')!;
        expect(hubTab(trust, '')).toBe(trust.tabs[0]);
        rememberHubTab('/trust', 'secret-scopes');
        expect(hubTab(trust, '')).toBe('secret-scopes');
        expect(hubTab(trust, '?tab=siem')).toBe('siem');
    });

    it('has a bundled snippet for every section the UI points at, and it is up to date with the docs', async () => {
        // eslint-disable-next-line @typescript-eslint/ban-ts-comment
        // @ts-ignore - a plain ES module script
        const gen = await import('../../../scripts/build-help.mjs');
        const root = path.resolve(__dirname, '../../..');
        const wanted: string[] = gen.wantedAnchors(root);
        const { snippets, problems } = gen.buildHelp(path.join(root, '../docs/site'), wanted);
        expect(problems).toEqual([]);
        expect(HELP).toEqual(snippets);              // run `node scripts/build-help.mjs` after changing the docs
        for (const spec of Object.values(RESOURCE_SPECS)) if (spec.docs) expect(Object.keys(HELP)).toContain(spec.docs);
        const s = await helpFor('administration.html#secret-scopes');
        expect(s?.title).toBe('Secret scopes');
        expect(s?.body.length).toBeGreaterThan(40);
    });
});

// ---- in the workspace ------------------------------------------------------------------------------------------
function profile(ui?: unknown, role: string | null = 'Financial analyst') {
    return { urn: ME, role, team: null, default_persona: null, language: null, pinned: [], instructions: null,
        notifications: { channels: { in_app: true }, events: {} }, personas: [], push: { enabled: false, subscriptions: 0 },
        ui, updated_ms: null };
}

function workspace(routes: Record<string, unknown>, calls: MockCall[] = [], at = '/discover') {
    const { client } = makeClient({ 'GET /ui/config': UI_CONFIG, 'GET /notify/inbox': [], ...routes }, calls);
    let location = '';
    render(
        <MemoryRouter initialEntries={[at]}>
            <I18nProvider initial="en">
                <CairnProvider client={client} cache={new QueryCache()}>
                    <ConsumerLayout>
                        <Route path="/get-started" component={GetStartedPage} />
                    </ConsumerLayout>
                    <Route render={({ location: l }) => {
                        location = l.pathname + l.search;
                        return null;
                    }} />
                </CairnProvider>
            </I18nProvider>
        </MemoryRouter>,
    );
    return { where: () => location };
}

describe('the guided workspace', () => {
    it('asks the role on first sign-in, suggested from the profile, and keeps the answer in the profile', async () => {
        const calls: MockCall[] = [];
        let stored = profile(undefined);
        workspace({
            'GET /v1/me/profile': () => stored,
            'PUT /v1/me/profile': (b: unknown) => {
                const ui = (b as { ui: { onboarding: Record<string, unknown> } }).ui;
                stored = profile({ onboarding: { role: null, done: [], dismissed: false, asked: false, ...ui.onboarding },
                    sidebar: { pinned: [], hidden: [], show_all: false } });
                return stored;
            },
            'GET /v1/me/capabilities': PERSON_CAPS,
        }, calls);
        const dialog = await screen.findByRole('dialog', { name: 'Welcome to Cairn. What do you mostly do here?' });
        const analyst = within(dialog).getByRole('radio', { name: /Analyst/ });
        expect((analyst as HTMLInputElement).checked).toBe(true);          // from "Financial analyst"
        expect(within(dialog).getByText('from your profile')).toBeTruthy();
        fireEvent.click(within(dialog).getByRole('button', { name: 'Continue' }));
        await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true));
        expect(calls.find((c) => c.method === 'PUT')!.body).toEqual({ ui: { onboarding: { role: 'analyst', asked: true, dismissed: false } } });
        await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
        // The sidebar now has the analyst's "For you" group.
        const nav = screen.getByRole('navigation', { name: 'Workspace sections' });
        expect(within(nav).getByRole('heading', { name: 'For you' })).toBeTruthy();
        expect(within(nav).getByRole('link', { name: 'Get started' })).toBeTruthy();
    });

    it('does not ask when the workspace turned onboarding off, and hides what the person cannot use', async () => {
        workspace({
            'GET /ui/config': { ...UI_CONFIG, features: { ...UI_CONFIG.features, onboarding: false } },
            'GET /v1/me/profile': profile(undefined),
            'GET /v1/me/capabilities': PERSON_CAPS,
        });
        const nav = await screen.findByRole('navigation', { name: 'Workspace sections' });
        await waitFor(() => expect(within(nav).getByRole('link', { name: 'SQL' })).toBeTruthy());
        expect(screen.queryByRole('dialog')).toBeNull();
        expect(within(nav).queryByRole('link', { name: 'Get started' })).toBeNull();
        expect(within(nav).queryByRole('link', { name: 'Governance' })).toBeNull();
    });

    it('the checklist ticks steps the API shows done, and a step can be ticked by hand', async () => {
        const calls: MockCall[] = [];
        let stored = profile({ onboarding: { role: 'analyst', done: [], dismissed: false, asked: true },
            sidebar: { pinned: [], hidden: [], show_all: false } });
        workspace({
            'GET /v1/me/profile': () => stored,
            'PUT /v1/me/profile': (b: unknown) => {
                const ui = (b as { ui: { onboarding: Record<string, unknown> } }).ui;
                stored = profile({ ...stored.ui as object, onboarding: { ...(stored.ui as { onboarding: object }).onboarding, ...ui.onboarding } });
                return stored;
            },
            'GET /v1/me/capabilities': PERSON_CAPS,
            'GET /sql/history': [{ id: 'r1' }],
            'GET /sql/saved': [{ id: 'q', owner: ME }],
            'GET /sql/dashboards': [],
        }, calls, '/get-started');
        expect(await screen.findByRole('heading', { name: 'Get started', level: 1 })).toBeTruthy();
        await waitFor(() => expect(screen.getByText('2 of 5 done')).toBeTruthy());
        const step = document.querySelector('[data-step="save-query"]') as HTMLElement;
        expect(step.getAttribute('data-done')).toBe('true');
        expect(within(step).getByRole('link', { name: 'Save the query' }).getAttribute('href')).toBe('/sql');
        fireEvent.click(screen.getByRole('checkbox', { name: 'Mark “Build a dashboard” as done' }));
        await waitFor(() => expect(screen.getByText('3 of 5 done')).toBeTruthy());
        expect(calls.filter((c) => c.method === 'PUT').pop()!.body).toEqual({ ui: { onboarding: {
            done: ['run-query', 'save-query', 'build-dashboard'] } } });
    });

    it('⌘K opens the palette (an ARIA combobox), finds a page and an action, and Enter opens it', async () => {
        const nav = workspace({ 'GET /v1/me/profile': profile({ onboarding: { role: 'analyst', done: [], dismissed: false, asked: true },
            sidebar: { pinned: [], hidden: [], show_all: false } }), 'GET /v1/me/capabilities': ADMIN_CAPS });
        await screen.findByRole('navigation', { name: 'Workspace sections' });
        act(() => {
            fireEvent.keyDown(window, { key: 'k', ctrlKey: true });
        });
        const box = await screen.findByRole('combobox', { name: 'Type a page, an action or a dataset…' });
        const list = screen.getByRole('listbox', { name: 'Command palette' });
        fireEvent.change(box, { target: { value: 'sql workflows' } });
        await waitFor(() => expect(within(list).getAllByRole('option')[0].textContent).toContain('SQL workflows'));
        expect(box.getAttribute('aria-activedescendant')).toBe(within(list).getAllByRole('option')[0].id);
        expect(within(list).getAllByRole('option')[0].getAttribute('aria-selected')).toBe('true');
        fireEvent.keyDown(box, { key: 'Enter' });
        await waitFor(() => expect(nav.where()).toBe('/sql/workflows'));
        // An action: a form in a hub, opened by the palette.
        fireEvent.click(screen.getByRole('button', { name: 'Search or jump to…' }));
        const box2 = await screen.findByRole('combobox', { name: 'Type a page, an action or a dataset…' });
        fireEvent.change(box2, { target: { value: 'create scope' } });
        const first = await screen.findByRole('option', { name: /Create scope/ });
        fireEvent.keyDown(box2, { key: 'ArrowDown' });
        fireEvent.keyDown(box2, { key: 'ArrowUp' });
        expect(first.getAttribute('aria-selected')).toBe('true');
        fireEvent.keyDown(box2, { key: 'Enter' });
        await waitFor(() => expect(nav.where()).toBe('/trust?tab=secret-scopes&action=Create%20scope'));
        // The last entry hands the text to the assistant.
        fireEvent.click(screen.getByRole('button', { name: 'Search or jump to…' }));
        const box3 = await screen.findByRole('combobox', { name: 'Type a page, an action or a dataset…' });
        fireEvent.change(box3, { target: { value: 'revenue by region last week' } });
        fireEvent.click(await screen.findByRole('option', { name: /Ask: revenue by region last week/ }));
        await waitFor(() => expect(nav.where()).toBe('/assistant?q=revenue%20by%20region%20last%20week'));
    });

    it('Enter chooses the entry the person moved to, even when results arrive after and re-rank the list', async () => {
        const pending: ((v: unknown) => void)[] = [];      // every search waits until the test answers
        const nav = workspace({ 'GET /v1/me/profile': profile({ onboarding: { role: 'analyst', done: [], dismissed: false, asked: true },
            sidebar: { pinned: [], hidden: [], show_all: false } }), 'GET /v1/me/capabilities': ADMIN_CAPS,
            'GQL cairnConsumerSearch': () => new Promise((r) => {
                pending.push(r);
            }) });
        await screen.findByRole('navigation', { name: 'Workspace sections' });
        act(() => {
            fireEvent.keyDown(window, { key: 'k', ctrlKey: true });
        });
        const box = await screen.findByRole('combobox', { name: 'Type a page, an action or a dataset…' });
        fireEvent.change(box, { target: { value: 'orders qx' } });
        const ask = await screen.findByRole('option', { name: /Ask: orders qx/ });
        fireEvent.keyDown(box, { key: 'End' });
        expect(ask.getAttribute('aria-selected')).toBe('true');
        // The catalog answers now: a dataset joins the list above the person's choice.
        await waitFor(() => expect(pending.length).toBeGreaterThan(0));
        await act(async () => {
            for (const r of pending.splice(0)) {
                r({ data: { searchAcrossEntities: { total: 1, facets: [],
                    searchResults: [{ entity: { urn: DS, type: 'DATASET', name: 'orders', platform: { name: 'snowflake' } } }] } } });
            }
        });
        const list = screen.getByRole('listbox', { name: 'Command palette' });
        await waitFor(() => expect(within(list).getAllByRole('option').map((o) => o.textContent).join('|')).toContain('snowflake'));
        expect(screen.getByRole('option', { name: /Ask: orders qx/ }).getAttribute('aria-selected')).toBe('true');
        fireEvent.keyDown(box, { key: 'Enter' });
        await waitFor(() => expect(nav.where()).toBe('/assistant?q=orders%20qx'));
    });

    it('"?" opens the keyboard shortcuts, and the help button opens this page’s docs', async () => {
        workspace({ 'GET /v1/me/profile': profile(undefined), 'GET /v1/me/capabilities': PERSON_CAPS,
            'GET /ui/config': { ...UI_CONFIG, features: { ...UI_CONFIG.features, onboarding: false } } }, [], '/sql');
        await screen.findByRole('navigation', { name: 'Workspace sections' });
        act(() => {
            fireEvent.keyDown(window, { key: '?' });
        });
        const sheet = await screen.findByRole('dialog', { name: 'Keyboard shortcuts' });
        expect(within(sheet).getByText('Command palette')).toBeTruthy();
        fireEvent.click(within(sheet).getByRole('button', { name: 'Close' }));
        fireEvent.click(screen.getAllByRole('button', { name: 'Help for this page' })[0]);
        const panel = (await screen.findByText(/^Help: /)).closest('.ant-drawer-content') as HTMLElement;
        expect(await within(panel).findByRole('heading', { name: 'SQL workspace' })).toBeTruthy();
        expect(within(panel).getByRole('link', { name: 'Open full docs' }).getAttribute('href')).toBe('/docs/administration.html#sql-workspace');
        expect(within(panel).getByRole('button', { name: 'Ask the assistant about this page' })).toBeTruthy();
    });
});
