import { render, screen, waitFor, within } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { PipelinesCard } from '../components/HomeCards';
import { ConsumerLayout, paletteKeys } from '../components/Layout';
import { StatusBadge, toneOf } from '../components/StatusBadge';
import { WhyPanel, userId } from '../components/WhyPanel';
import { CairnProvider } from '../context/CairnProvider';
import { StepStatus } from '../flows/FlowCanvas';
import { I18nProvider } from '../i18n';
import capabilities from '../resources/capabilities.json';
import { CapabilitiesPage } from '../resources/CapabilitiesPage';
import { awakeHours, promotionChains, type AirflowDeployment } from '../resources/AirflowDeployments';
import { UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';
// @ts-expect-error - a plain ES module script
import { parseRoadmap } from '../../../scripts/capabilities.mjs';

beforeAll(installMatchMedia);

function wrap(ui: React.ReactElement, routes: Record<string, unknown> = {}, at = '/', locale: 'en' | 'de' = 'en') {
    const { client, cache } = makeClient({ 'GET /ui/config': UI_CONFIG, 'GET /notify/inbox': [], ...routes });
    return render(
        <MemoryRouter initialEntries={[at]}>
            <I18nProvider initial={locale}>
                <CairnProvider client={client} cache={cache}>{ui}</CairnProvider>
            </I18nProvider>
        </MemoryRouter>,
    );
}

describe('StatusBadge', () => {
    it('maps the states the APIs use to five tones', () => {
        expect(['success', 'healthy', 'live', 'ready', 'ok'].map(toneOf)).toEqual(['ok', 'ok', 'ok', 'ok', 'ok']);
        expect(['failed', 'refused', 'down', 'FAIL'].map(toneOf)).toEqual(['error', 'error', 'error', 'error']);
        expect(['degraded', 'sla_missed', 'partial'].map(toneOf)).toEqual(['warn', 'warn', 'warn']);
        expect(toneOf('running')).toBe('running');
        expect(['pending', 'queued', 'skipped', 'hibernating', undefined].map(toneOf)).toEqual(
            ['waiting', 'waiting', 'waiting', 'waiting', 'waiting']);
    });

    it('always says the state in words, with a shape that is not the only cue', () => {
        wrap(<div>
            <StatusBadge state="failed" />
            <StatusBadge state="sla_missed" />
            <StatusBadge tone="ok" label="Healthy" />
        </div>);
        const failed = screen.getByText('Failed').closest('.cairn-status') as HTMLElement;
        expect(failed.getAttribute('data-tone')).toBe('error');
        expect(failed.querySelector('.mark')?.getAttribute('aria-hidden')).toBe('true');
        expect(screen.getByText('SLA missed').closest('[data-tone]')?.getAttribute('data-tone')).toBe('warn');
        expect(screen.getByText('Healthy').closest('[data-tone]')?.getAttribute('data-tone')).toBe('ok');
    });

    it('speaks the person’s language', () => {
        wrap(<StatusBadge state="running" />, {}, '/', 'de');
        expect(screen.getByText('Läuft')).toBeTruthy();
    });

    it('a flow step reads Done, Running, Waiting, never just a colour', () => {
        wrap(<div><StepStatus status="ok" /><StepStatus status="running" /><StepStatus status="waiting" /></div>);
        expect(screen.getByText('Done').getAttribute('data-status')).toBe('ok');
        expect(screen.getByText('Running')).toBeTruthy();
        expect(screen.getByText('Waiting')).toBeTruthy();
    });
});

describe('the shell', () => {
    it('marks the current area with aria-current="page", and the logo goes home', async () => {
        wrap(<ConsumerLayout />, {}, '/products');
        const nav = screen.getByRole('navigation', { name: 'Workspace sections' });
        expect(within(nav).getByRole('link', { name: 'Data products' }).getAttribute('aria-current')).toBe('page');
        expect(within(nav).getByRole('link', { name: 'Search' }).getAttribute('aria-current')).toBeNull();
        expect(nav.querySelectorAll('[aria-current="page"]')).toHaveLength(1);
        const home = screen.getByRole('link', { name: 'Cairn home' });
        expect(home.getAttribute('href')).toBe('/discover');
        // One search in the header (L150): the box searches data and finds pages and commands; Ctrl K and / focus it.
        const search = screen.getByRole('combobox', { name: 'Search data or jump to a page' });
        expect(search.getAttribute('aria-keyshortcuts')).toContain('Control+K');
        expect(search.getAttribute('aria-expanded')).toBe('false');
        expect(search.hasAttribute('aria-controls')).toBe(false);
        expect(within(search.parentElement as HTMLElement).getByText(paletteKeys())).toBeTruthy();
        expect(screen.getAllByRole('combobox', { name: /search/i })).toHaveLength(1);
        expect(screen.queryByRole('textbox', { name: 'Search' })).toBeNull();
    });

    it('writes the palette shortcut the way the computer does', () => {
        expect(paletteKeys('MacIntel')).toBe('⌘ K');
        expect(paletteKeys('Win32')).toBe('Ctrl K');
        expect(paletteKeys('Linux x86_64')).toBe('Ctrl K');
    });
});

describe('the platform map', () => {
    it('marks the rows of the release named in ROADMAP.md as new', () => {
        const md = [
            '# Roadmap', '', '## Next', '', '**In this release:** L2 to L3.', '',
            '| # | Item | Definition of done | Status |', '|---|---|---|---|',
            '| L1 | **One.** a | done | ✅ |', '| L2 | **Two.** b | done | 🟡 partly |', '| L3 | **Three.** c | done | ✅ |',
        ].join('\n');
        const items = parseRoadmap(md).items as { id: string; new?: boolean; status: string }[];
        expect(items.map((i) => [i.id, !!i.new, i.status])).toEqual([['L1', false, 'shipped'], ['L2', true, 'partial'], ['L3', true, 'shipped']]);
        expect(parseRoadmap(md.replace('**In this release:** L2 to L3.', '')).items.some((i: { new?: boolean }) => i.new)).toBe(false);
    });

    it('shows the data flow, the layers with their marks and what is new, all from the data', () => {
        wrap(<CapabilitiesPage />, {}, '/capabilities');
        expect(screen.getByRole('heading', { level: 1, name: 'One governed platform for data, analytics, pipelines and AI' })).toBeTruthy();
        const flow = screen.getByRole('region', { name: 'How data moves' });
        expect(within(flow).getAllByRole('listitem')).toHaveLength(5);
        expect(within(flow).getByText('Governance across every step')).toBeTruthy();
        const layers = screen.getByRole('region', { name: 'Capabilities by layer' });
        expect(layers.querySelectorAll('[data-layer]')).toHaveLength(capabilities.layers.length);
        // Each mark has its words for screen readers.
        expect(within(layers).getAllByText('Shipped and tested').length).toBe(capabilities.layers.filter((l) => l.status === 'shipped').length);
        const fresh = (capabilities.items as { id: string; new?: boolean; status: string }[]).filter((i) => i.new);
        expect(fresh.length).toBeGreaterThan(0);
        const release = screen.getByRole('region', { name: 'In this release' });
        expect(release.querySelectorAll('[data-item]')).toHaveLength(fresh.length);
        expect(within(release).getAllByText('new')).toHaveLength(fresh.length);
        expect(within(release).getAllByText('partly')).toHaveLength(fresh.filter((i) => i.status === 'partial').length);
        const partial = fresh.find((i) => i.status === 'partial');
        if (partial) {
            const card = release.querySelector(`[data-item="${partial.id}"]`) as HTMLElement;
            expect(card.querySelector('[data-status="partial"]')).toBeTruthy();
            expect(within(card).getByText('Partly validated')).toBeTruthy();
        }
        // An older shipped row is neither new nor partly.
        const items = screen.getByRole('region', { name: 'Roadmap items' });
        const l1 = items.querySelector('[data-item="L1"]') as HTMLElement;
        expect(within(l1).queryByText('new')).toBeNull();
    });
});

describe('screens', () => {
    it('home: the pipelines that need attention first, with their state in words', async () => {
        wrap(<PipelinesCard />, { 'GET /v1/observability/pipelines': { items: [
            { key: 'flow:a', name: 'a', source: 'flow', health: 'healthy', trend: [] },
            { key: 'airflow:b', name: 'b', source: 'airflow', health: 'failing', trend: [] },
        ] } });
        const card = await screen.findByRole('region', { name: 'Pipelines' });
        const names = within(card).getAllByRole('link').map((a) => a.textContent);
        expect(names.slice(0, 2)).toEqual(['b', 'a']);
        expect(within(card).getByText('Failing').closest('[data-tone]')?.getAttribute('data-tone')).toBe('error');
    });

    it('home: the pipelines card is not shown to someone who may not read pipelines (403), or where none are set up (404)', async () => {
        for (const status of [403, 404]) {
            const { container, unmount } = wrap(<PipelinesCard />, { 'GET /v1/observability/pipelines':
                new Response(JSON.stringify({ detail: 'no' }), { status }) });
            await new Promise((r) => setTimeout(r, 50));
            expect(container.textContent, String(status)).toBe('');
            unmount();
        }
    });

    it('home: the pipelines card says so when pipelines cannot be read (not an empty Home)', async () => {
        wrap(<PipelinesCard />, { 'GET /v1/observability/pipelines':
            new Response(JSON.stringify({ detail: 'the store is down' }), { status: 500 }) });
        const card = await screen.findByRole('region', { name: 'Pipelines' });
        expect(await within(card).findByRole('alert')).toBeTruthy();
    });

    it('statuses the licence and SRE screens send have their own tone', () => {
        expect(toneOf('valid')).toBe('ok');
        expect(toneOf('executed')).toBe('ok');
        expect(toneOf('expiring')).toBe('warn');
        expect(toneOf('invalid')).toBe('error');
        expect(toneOf('missing')).toBe('error');
    });

    it('a dataset: why you see the preview — masks from the preview, the policy from the access check', async () => {
        const urn = 'urn:li:dataset:(urn:li:dataPlatform:trino,finance.revenue,PROD)';
        const calls: { path: string; body: unknown }[] = [];
        const { client, cache } = makeClient({
            'GET /ui/config': UI_CONFIG,
            'GET /data/preview': { columns: ['day', 'email'], rows: [], masked_columns: ['email'], truncated: false, elapsed_ms: 3, tables: [] },
            'POST /v1/people/access-check': (body: unknown) => {
                calls.push({ path: 'check', body });
                return { allowed: true, decision: 'allow', summary: 'yes', because: [{ kind: 'persona', name: 'Finance analysts', policy_id: 'p1',
                    description: 'Grants query' }], considered: [], obligations: [{ type: 'row_filter', params: { expression: "region = 'EU'" } }],
                subject: { groups: [], personas: [], workspaces: [], attributes: {} } };
            },
        });
        render(<MemoryRouter><I18nProvider initial="en"><CairnProvider client={client} cache={cache}><WhyPanel urn={urn} /></CairnProvider>
        </I18nProvider></MemoryRouter>);
        const why = await screen.findByRole('complementary', { name: 'Why you see this' });
        expect(await within(why).findByText('Masked for you: email')).toBeTruthy();
        expect(await within(why).findByText('Allowed by persona Finance analysts')).toBeTruthy();
        expect(within(why).getByText("region = 'EU'")).toBeTruthy();
        expect(within(why).getByText('Every read is recorded')).toBeTruthy();
        await waitFor(() => expect(calls[0]?.body).toEqual({ person: 'ana', privilege: 'CAIRN_DATA_ACCESS', resource: urn }));
        expect(userId('urn:li:corpuser:ana')).toBe('ana');
    });

    it('a dataset: without the access check (not an administrator), only what the preview says', async () => {
        wrap(<WhyPanel urn="urn:x" />, {
            'GET /data/preview': { columns: ['a'], rows: [], masked_columns: [], truncated: false, elapsed_ms: 1, tables: [] },
            'POST /v1/people/access-check': new Response(JSON.stringify({ detail: 'forbidden' }), { status: 403 }),
        });
        const why = await screen.findByRole('complementary', { name: 'Why you see this' });
        expect(await within(why).findByText('No column is masked for you in this preview.')).toBeTruthy();
        expect(within(why).queryByText(/Allowed by/)).toBeNull();
    });

    it('airflow: environments in promotion order, and their hours in words', () => {
        const d = (name: string, environment: string, team: string | null = 'finance') => ({ name, environment, team } as AirflowDeployment);
        const chains = promotionChains([d('f-prod', 'prod'), d('f-dev', 'dev'), d('f-stage', 'stage'), d('ops', 'dev', null)]);
        expect(chains.map((c) => [c.team, c.chain.map((x) => x.name)])).toEqual([['finance', ['f-dev', 'f-stage', 'f-prod']], ['', ['ops']]]);
        expect(awakeHours(null)).toBeNull();
        expect(awakeHours({ awake: [{ days: ['mon', 'tue', 'wed'], from: '08:00', to: '19:00' }], timezone: 'UTC' })).toBe('mon–wed 08:00–19:00 UTC');
        expect(awakeHours({ awake: [{ days: ['mon', 'thu'], from: '08:00', to: '09:00' }], timezone: 'UTC' })).toBe('mon, thu 08:00–09:00 UTC');
    });
});
