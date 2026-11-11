/**
 * L149, one home per concept: the sidebar's groups and sections, every route and tab of earlier layouts redirecting to
 * its one home, no concept in two places, the merged data product page, and the cross-store lists of Quality &
 * operations (runs, incidents, alerts) over the existing APIs.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import React from 'react';
import { MemoryRouter, Route, matchPath } from 'react-router-dom';

import { ConsumerClient } from '../api/client';
import { CONSUMER_ROUTES, ConsumerRoutes } from '../ConsumerApp';
import { ConsumerLayout, KEPT_ROUTES, NAV, NAV_GROUPS } from '../components/Layout';
import { AllIncidents, AllRuns, loadAlerts, loadIncidents, loadRuns } from '../components/Operations';
import { CairnProvider } from '../context/CairnProvider';
import { HOST_SECTIONS } from '../hostNav';
import { I18nProvider } from '../i18n';
import { MOVED_ROUTES, MOVES, movedTarget } from '../moves';
import { DataProductsPage } from '../pages/ProductsPages';
import { SemanticsMetrics } from '../pages/SemanticsTabs';
import { CUSTOM_TABS } from '../resources/customTabs';
import { HUBS, PAGE_AREAS, RESOURCE_SPECS } from '../resources/catalog';
import { MockCall, PRODUCT, UI_CONFIG, makeClient, mockFetch } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

function wrap(ui: React.ReactElement, routes: Record<string, unknown>, at = '/', calls: MockCall[] = [], locale: 'en' | 'de' = 'en') {
    const { client, cache } = makeClient({ 'GET /ui/config': UI_CONFIG, 'GET /notify/inbox': [], 'GET /v1/me/capabilities': ADMIN,
        ...routes }, calls);
    let where = '';
    render(
        <MemoryRouter initialEntries={[at]}>
            <I18nProvider initial={locale}>
                <CairnProvider client={client} cache={cache}>
                    {ui}
                    <Route render={({ location }) => {
                        where = location.pathname + location.search;
                        return null;
                    }} />
                </CairnProvider>
            </I18nProvider>
        </MemoryRouter>,
    );
    return { calls, where: () => where };
}

const ADMIN = { actor: 'urn:li:corpuser:ana', is_admin: true, privileges: { MANAGE_POLICIES: true, MANAGE_SYSTEM_OPERATIONS: true,
    MANAGE_GLOBAL_SETTINGS: true } };

const known = (path: string) => CONSUMER_ROUTES.some((r) => matchPath(path, { path: r.path, exact: r.exact }));

/** The tabs each purpose-built page answers to (`?tab=`), besides the hubs' own. */
const PAGE_TABS: Record<string, string[]> = {
    '/governance': ['personas', 'purposes', 'consent', 'tokens', 'residency', 'privacy'],
    '/people': ['people', 'groups', 'roles', 'principals', 'workspaces', 'access', 'access-reviews'],
    '/flows': ['flows', 'sql-workflows', 'jobs', 'airflow'],
    '/observability': ['health', 'journey', 'logs', 'slos', 'export'],
    '/dashboards': ['boards', 'folders', 'exports', 'usage'],
    '/settings': ['guide', 'iac', 'embeds', 'extensions', 'licence'],
    '/products/:urn': ['overview', 'assets', 'contracts', 'health', 'lineage', 'access', 'distribution'],
};

// ---- the sidebar --------------------------------------------------------------------------------------------------
describe('the consolidated sidebar', () => {
    it('has the seven groups and their sections, in order, each concept once', () => {
        expect(NAV_GROUPS.map((g) => [g.label, g.items.map((i) => i.to)])).toEqual([
            ['nav.group.catalog', ['/discover', '/catalog', '/products', '/glossary', '/domains', '/semantics', '/lineage', '/requests']],
            ['nav.group.govern', ['/governance', '/people', '/automation', '/security', '/sharing', '/usage', '/reports']],
            ['nav.group.quality', ['/quality', '/runs', '/incidents', '/observability', '/alerts']],
            ['nav.group.analyze', ['/sql', '/sql/workbooks', '/dashboards', '/sql/input-tables']],
            ['nav.group.build', ['/ingestion', '/lakehouse', '/flows']],
            ['nav.group.ai', ['/assistant', '/agents', '/ml', '/ai/gateway']],
            ['nav.group.platform', ['/compute', '/settings', '/capabilities']],
        ]);
        expect(new Set(NAV.map((n) => n.to)).size).toBe(NAV.length);
        for (const n of NAV) expect(known(n.to), n.to).toBe(true);
        for (const r of KEPT_ROUTES) expect(known(r), r).toBe(true);
    });

    it('shows no tab or area in two places', () => {
        const seen = new Map<string, string>();
        for (const hub of HUBS) {
            for (const tab of hub.tabs) {
                expect(seen.get(tab), `${tab} in ${hub.route} and ${seen.get(tab)}`).toBeUndefined();
                seen.set(tab, hub.route);
            }
        }
        for (const page of PAGE_AREAS) {
            for (const { spec } of page.areas) {
                expect(seen.get(spec), `${spec} in ${page.route} and ${seen.get(spec)}`).toBeUndefined();
                seen.set(spec, page.route);
            }
        }
        // A view that replaced a page is in one hub only, and every custom view is somewhere.
        for (const key of Object.keys(CUSTOM_TABS)) expect(seen.has(key), key).toBe(true);
    });

    it('names the groups and sections, with Inbox and Profile in the header', async () => {
        wrap(<ConsumerLayout />, { 'GET /notify/inbox': [{ id: 'n1', title: 'x', body: '', created_at: '2026-01-01T00:00:00Z', read_at: null }] }, '/semantics');
        const nav = screen.getByRole('navigation', { name: 'Workspace sections' });
        await within(nav).findByRole('link', { name: 'Access policies' });   // once the person's privileges are known
        for (const h of ['Catalog', 'Govern', 'Quality & operations', 'Analyze', 'Build', 'AI', 'Platform']) {
            expect(within(nav).getByRole('heading', { name: h })).toBeTruthy();
        }
        for (const l of ['Home', 'Search', 'Semantics', 'Access policies', 'Automation', 'Security & compliance', 'External sharing',
            'Usage & cost', 'Governance report', 'Runs', 'Incidents', 'Platform health', 'Alerts & routing', 'Assistant', 'AI gateway',
            'Compute', 'Platform map']) {
            expect(within(nav).getByRole('link', { name: l })).toBeTruthy();
        }
        expect(within(nav).getByRole('link', { name: 'Semantics' }).getAttribute('aria-current')).toBe('page');
        for (const gone of ['Marketplace', 'Contracts', 'Trust & security', 'Pipelines', 'Profile', 'Query insight', 'SQL workflows']) {
            expect(within(nav).queryByRole('link', { name: gone })).toBeNull();
        }
        expect((await screen.findByRole('link', { name: 'Inbox, 1 unread' })).getAttribute('href')).toBe('/inbox');
        expect(screen.getByRole('link', { name: 'Your profile: Ana Analyst' }).getAttribute('href')).toBe('/profile');
    });

    it('speaks German in the new sections', async () => {
        wrap(<ConsumerLayout />, {}, '/', [], 'de');
        const nav = screen.getByRole('navigation', { name: 'Bereiche' });
        await within(nav).findByRole('link', { name: 'Zugriffsrichtlinien' });
        expect(within(nav).getByRole('heading', { name: 'Qualität & Betrieb' })).toBeTruthy();
        for (const l of ['Semantik', 'Zugriffsrichtlinien', 'Läufe', 'Vorfälle', 'Alarme & Weiterleitung', 'Externe Freigabe']) {
            expect(within(nav).getByRole('link', { name: l })).toBeTruthy();
        }
    });

    it('keeps the catalog shell entries opening a workspace section', () => {
        for (const s of HOST_SECTIONS) {
            const [path, query = ''] = s.route.split('?');
            expect(known(path), s.route).toBe(true);
            expect(movedTarget(path, query ? `?${query}` : ''), `${s.route} opens its home without a redirect`).toBeNull();
        }
    });
});

// ---- every earlier route redirects to its one home ------------------------------------------------------------------
describe('earlier routes and tabs', () => {
    const urn = encodeURIComponent(PRODUCT.urn);
    const cases = MOVED_ROUTES.map((r) => r.replace(':urn', urn));

    it('lists every route of the earlier sidebar that is gone', () => {
        for (const old of ['/search', '/marketplace', '/contracts', '/trust', '/pipelines', '/sql/metrics', '/semantic', '/ontology',
            '/context', '/queries', '/sql/workflows', '/sql/git']) {
            expect(MOVES.some((m) => m.from === old && m.to), old).toBe(true);
            expect(known(old), `${old} has no page of its own any more`).toBe(false);
        }
    });

    it.each(cases)('%s lands on its new home', (old) => {
        const [path, query = ''] = old.split('?');
        const to = movedTarget(path, query ? `?${query}` : '');
        expect(to, old).toBeTruthy();
        const [tpath, tquery = ''] = to!.split('?');
        expect(known(tpath), `${old} → ${to}`).toBe(true);
        expect(movedTarget(tpath, tquery ? `?${tquery}` : ''), `${to} redirects again`).toBeNull();
        const tab = new URLSearchParams(tquery).get('tab');
        if (tab) {
            const hub = HUBS.find((h) => h.route === tpath);
            const page = Object.entries(PAGE_TABS).find(([p]) => matchPath(tpath, { path: p, exact: true }))?.[1];
            expect(hub?.tabs.includes(tab) || page?.includes(tab), `${old} → ${to}: tab ${tab}`).toBe(true);
        }
    });

    it('keeps the rest of the query string', () => {
        expect(movedTarget('/search', '?q=orders&f._entityType=DATASET')).toBe('/catalog?q=orders&f._entityType=DATASET');
        expect(movedTarget('/observability', '?tab=incidents&incident=i1')).toBe('/incidents?incident=i1');
        expect(movedTarget('/contracts', '?status=BROKEN')).toBe('/quality?tab=contracts&status=BROKEN');
        expect(movedTarget(`/marketplace/${urn}`, '?tab=publish')).toBe(`/products/${urn}?tab=distribution`);
        expect(movedTarget('/trust', '?tab=secret-scopes&action=Create%20scope')).toBe('/security?tab=secret-scopes&action=Create+scope');
        expect(movedTarget('/observability', '?tab=journey')).toBeNull();
        expect(movedTarget(`/contracts/${urn}`, '')).toBeNull();
    });

    it.each([
        ['/search?q=orders', '/catalog?q=orders'],
        [`/marketplace/${urn}`, `/products/${urn}`],
        ['/trust?tab=secret-scopes', '/security?tab=secret-scopes'],
        ['/settings?tab=sources', '/ingestion?tab=sources'],
        ['/pipelines', '/runs?tab=dbt'],
    ])('the app redirects %s to %s before rendering', async (from, to) => {
        const nav = wrap(<ConsumerRoutes />, {}, from);
        await waitFor(() => expect(nav.where()).toBe(to));
    });
});

// ---- one data product page, Marketplace as its Browse view -----------------------------------------------------------
describe('data products', () => {
    it('Browse is the marketplace: product cards, requests, and agents and skills', async () => {
        const nav = wrap(<Route path="/products" component={DataProductsPage} />, {
            'GET /marketplace/products': [PRODUCT],
            'GET /catalog/products': { products: [] },
            'GET /marketplace/listings': { listings: [{ id: 'l1', name: 'Churn skill', kind: 'skill', target: 'x', source: 'y' }] },
        }, '/products?view=browse');
        expect(await screen.findByText('Customer 360')).toBeTruthy();
        expect(screen.getByRole('radio', { name: 'Browse' })).toBeTruthy();
        const listings = screen.getByRole('region', { name: 'Agents and skills' });
        expect(await within(listings).findByText('Churn skill')).toBeTruthy();
        fireEvent.click(screen.getByRole('radio', { name: 'All products' }));
        await waitFor(() => expect(nav.where()).toBe('/products'));
    });
});

// ---- Quality & operations: one list each, over the existing stores ---------------------------------------------------
const forbidden = () => new Response(JSON.stringify({ detail: 'needs MANAGE_SYSTEM_OPERATIONS' }), { status: 403 });

describe('runs, incidents and alerts', () => {
    const client = (routes: Record<string, unknown>) => new ConsumerClient({ fetchImpl: mockFetch(routes) });

    it('Runs reads each run API and labels each run with what ran it', async () => {
        const out = await loadRuns(client({
            'GET /v1/flows': [{ name: 'nightly' }],
            'GET /v1/flows/nightly/runs': [{ id: 'fr1', status: 'succeeded', trigger: 'cron', started_ms: 5000, finished_ms: 6000, error: null }],
            'GET /v1/sql-workflows': [{ id: 'w1', name: 'refresh' }],
            'GET /v1/sql-workflows/w1/runs': [{ id: 'wr1', status: 'failed', trigger: 'schedule', started_ms: 4000, finished_ms: 4500 }],
            'GET /v1/jobs': [{ name: 'score' }],
            'GET /v1/jobs/score/runs': [{ id: 'jr1', status: 'running', trigger: 'manual', started_ms: 7000, finished_ms: null, error: null }],
            'GET /pipelines/dbt/runs': [{ invocation: 'inv', project: 'shop', command: 'build', status: 'error', started_ms: 3000, finished_ms: 3500 }],
            'GET /v1/derived': [{ id: 'd1', name: 'by_region' }],
            'GET /v1/derived/d1/runs': [{ id: 'dr1', status: 'ok', trigger: 'change', started_ms: 2000, finished_ms: 2100, error: null }],
            'GET /v1/pipes': [{ id: 'p1', name: 'orders-files' }],
            'GET /v1/pipes/p1/files': [{ key: 'a.csv', version: '1', status: 'ok', error: null, loaded_ms: 1000 }],
            'GET /v1/observability/pipelines': forbidden,
        }));
        expect(out.rows.map((r) => [r.source, r.state])).toEqual([['job', 'running'], ['flow', 'succeeded'], ['sql-workflow', 'failed'],
            ['dbt', 'failed'], ['derived', 'succeeded'], ['pipe', 'succeeded']]);
        expect(out.rows[1].link).toBe('/flows?flow=nightly');
        expect(out.problems.map((p) => p.source)).toEqual(['Airflow']);
    });

    it('Runs shows the list with its kinds and says which sources it could not read', async () => {
        wrap(<AllRuns />, {
            'GET /v1/flows': [{ name: 'nightly' }],
            'GET /v1/flows/nightly/runs': [{ id: 'fr1', status: 'failed', trigger: 'cron', started_ms: 5000, finished_ms: 6000, error: 'boom' }],
            'GET /v1/sql-workflows': [], 'GET /v1/jobs': [], 'GET /pipelines/dbt/runs': [], 'GET /v1/derived': [], 'GET /v1/pipes': [],
            'GET /v1/observability/pipelines': forbidden,
        });
        const table = await screen.findByRole('table');
        expect(await within(table).findByText('nightly')).toBeTruthy();
        expect(within(table).getByText('Flow')).toBeTruthy();
        expect(within(table).getByText('boom')).toBeTruthy();
        expect(screen.getByText('Some sources are not shown')).toBeTruthy();
    });

    it('Incidents brings the three stores into one list, each row labelled with its source', async () => {
        const out = await loadIncidents(client({
            'GET /quality/monitors': [
                { id: 'm1', name: 'orders freshness', dataset_urn: 'urn:li:dataset:(urn:li:dataPlatform:hive,sales.orders,PROD)',
                  enabled: true, last_evaluation: { status: 'FAIL', ts_ms: 3000, message: 'late' } },
                { id: 'm2', name: 'ok one', dataset_urn: 'x', enabled: true, last_evaluation: { status: 'PASS', ts_ms: 1, message: '' } }],
            'GET /v1/observability/incidents': { items: [{ id: 'i1', title: 'API slow', severity: 'critical', status: 'open',
                services: ['api'], opened_at: '2026-01-01T00:00:02Z' }] },
            'GET /status.json': { incidents: [{ id: 's1', title: 'Degraded search', impact: 'degraded', components: ['Search'],
                starts_ms: 2000, resolved_ms: null, updates: [{ phase: 'investigating' }] }], history: [] },
        }));
        expect(out.rows.map((r) => [r.source, r.title])).toEqual(expect.arrayContaining([['quality', 'orders freshness failing'],
            ['platform', 'API slow'], ['status', 'Degraded search']]));
        expect(out.rows).toHaveLength(3);
        expect(out.problems).toEqual([]);
    });

    it('Incidents opens a platform incident in its drawer', async () => {
        const nav = wrap(<Route path="/incidents" component={AllIncidents} />, {
            'GET /quality/monitors': [], 'GET /status.json': forbidden,
            'GET /v1/observability/incidents': { items: [{ id: 'i1', title: 'API slow', severity: 'critical', status: 'open',
                services: ['api'], opened_at: '2026-01-01T00:00:02Z' }] },
            'GET /v1/observability/incidents/i1': { id: 'i1', title: 'API slow', severity: 'critical', status: 'open', services: ['api'],
                summary: null, assignee: null, opened_at: '2026-01-01T00:00:02Z', resolved_at: null, alerts: [], timeline: [] },
        }, '/incidents');
        expect(await screen.findByText('Platform')).toBeTruthy();
        fireEvent.click(screen.getByRole('button', { name: 'Open API slow' }));
        await waitFor(() => expect(nav.where()).toBe('/incidents?incident=i1'));
    });

    it('Alerts lists every kind of alert from its own API', async () => {
        const out = await loadAlerts(client({
            'GET /sql/alerts': [{ id: 'a1', name: 'Big orders', op: '>', value: 1, measure: 'rows', state: 'triggered', enabled: true }],
            'GET /sql/metric-alerts': [{ id: 'm1', name: 'Revenue drop', metric: 'revenue', state: 'ok', rule: { kind: 'change' } }],
            'GET /v1/observability/alert-rules': { items: [{ id: 'r1', name: 'High errors', severity: 'critical', enabled: true,
                last_evaluation: { matches: 2 } }] },
            'GET /v1/budgets': [{ id: 'b1', name: 'Team AI', amount_usd: 100, period: 'month', thresholds: [50, 100] }],
        }));
        expect(out.rows.map((r) => [r.kind, r.name, r.firing])).toEqual([['sql', 'Big orders', true], ['metric', 'Revenue drop', false],
            ['platform', 'High errors', true], ['budget', 'Team AI', false]]);
        expect(out.rows.map((r) => r.link)).toEqual(['/alerts?tab=sql-alerts', '/alerts?tab=metric-alerts', '/alerts?tab=platform-rules',
            '/usage?tab=budgets']);
    });

    it('the no-UI capabilities have real homes backed by their APIs', () => {
        const home = (key: string) => HUBS.find((h) => h.tabs.includes(key))?.route;
        expect(home('metric-alerts')).toBe('/alerts');
        expect(home('clean-rooms')).toBe('/sharing');
        expect(home('app-installs')).toBe('/compute');
        expect(home('synthetic')).toBe('/lakehouse');
        expect(home('model-monitors')).toBe('/quality');
        expect(RESOURCE_SPECS['metric-alerts'].path).toBe('/sql/metric-alerts');
        expect(RESOURCE_SPECS['model-monitors'].path).toBe('/v1/ml/monitors');
        expect(RESOURCE_SPECS['app-installs'].path).toBe('/v1/app-installs');
        expect(RESOURCE_SPECS.synthetic.actions?.[0].path).toBe('/v1/synthetic');
        expect(RESOURCE_SPECS['clean-rooms'].actions?.[0].path).toBe('/v1/clean-rooms');
    });
});

// ---- Semantics: both metric stores, each labelled ------------------------------------------------------------------
it('Semantics › Metrics names the store each list comes from until they merge', async () => {
    wrap(<SemanticsMetrics />, { 'GET /sql/metrics': [], 'GET /data/sources': [], 'GET /context/definitions': [] });
    expect(await screen.findByText('Two metric stores, shown together until they are merged')).toBeTruthy();
    expect(document.querySelector('[data-store="/sql/metrics"]')).toBeTruthy();
    expect(document.querySelector('[data-store="/context/definitions"]')).toBeTruthy();
});
