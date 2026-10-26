// Brand-leak scan of a running Cairn stack: what a user can see must never name the engine or another
// vendor.
//
//   CAIRN_ADMIN_PASSWORD=... node core/tools/leak_scan.mjs [http://localhost:9000]
//   optional: CAIRN_SINK_URL=http://127.0.0.1:8099/   (webhook sink of the e2e suite: notification payloads)
//             LEAK_SCAN_MAX_PAGES=150
//
// What it covers:
//   * unauthenticated surface: login page, manifest, /config, every /docs page, error responses
//   * a crawl of the UI after a real login: every same-origin link it finds (from the main routes,
//     search results and entity pages), and on each page every tab, menu and dropdown it can open;
//     rendered text, page titles, tooltips, alt text, placeholders and image URLs
//   * GraphQL prose: app config, users, roles, policies, platforms, ingestion sources and their run
//     reports and logs (what Data Sources → Run History shows)
//   * notification payloads (Slack / Teams / email / webhook bodies) captured by the sink
// URNs (urn:li:…) and other identifiers are not prose and are ignored.
// Exit code 1 when anything leaks.
import { createRequire } from 'node:module';

// CommonJS resolution, so a globally installed playwright (NODE_PATH) is found too.
const { chromium } = createRequire(import.meta.url)('playwright');

const BASE = (process.argv[2] || process.env.CAIRN_URL || 'http://localhost:9000').replace(/\/$/, '');
const PASSWORD = process.env.CAIRN_ADMIN_PASSWORD;
const SINK = process.env.CAIRN_SINK_URL || '';
const MAX_PAGES = Number(process.env.LEAK_SCAN_MAX_PAGES || 150);
if (!PASSWORD) {
    console.error('set CAIRN_ADMIN_PASSWORD');
    process.exit(2);
}
const LEAK = /data\s?hub|acryl|atlan(?![a-z])/i;
const SEEDS = [
    '/', '/search?query=*', '/ingestion', '/glossary', '/domains', '/structured-properties', '/tags',
    '/analytics', '/settings/tokens', '/settings/identities/users', '/settings/identities/groups',
    '/settings/permissions/roles', '/settings/permissions/policies', '/settings/views', '/settings/preferences',
    '/settings/notifications', '/settings/home-page', '/settings/secrets', '/govern/dashboard', '/tests',
    '/data-products', '/documents', '/cairn', '/cairn/discover', '/cairn/search?q=*', '/cairn/marketplace',
    '/cairn/governance', '/cairn/context', '/cairn/assistant', '/cairn/requests', '/cairn/settings',
];
// Never follow links that change state.
const SKIP = /logout|sign-?out|\/delete|\/remove|download|\.(zip|csv|json)(\?|$)/i;
const URN = /urn:li:[^\s"'<>,)]+/g;

const findings = new Map();
const note = (where, text) => {
    if (!text) return;
    const clean = String(text).replace(URN, 'URN');
    const m = LEAK.exec(clean);
    if (!m) return;
    const at = Math.max(0, m.index - 60);
    const snippet = clean.slice(at, m.index + 60).replace(/\s+/g, ' ');
    if (!findings.has(snippet)) findings.set(snippet, where);
};

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1400, height: 900 } });
const page = await ctx.newPage();
page.on('dialog', (d) => d.dismiss().catch(() => {}));

// ---------------------------------------------------------------- 1. unauthenticated surface
const docs = await page.request.get(`${BASE}/docs/`);
const docPages = docs.ok() ? [...(await docs.text()).matchAll(/href="([a-z0-9-]+\.html)"/g)].map((m) => `/docs/${m[1]}`) : [];
if (!docs.ok()) findings.set(`GET /docs/: HTTP ${docs.status()}`, 'docs');
for (const path of ['/login', '/manifest.json', '/config', '/no-such-page', ...new Set(docPages)]) {
    const r = await page.request.get(BASE + path);
    note(`GET ${path}`, await r.text());
}
for (const [path, body] of [['/logIn', { username: 'admin', password: 'wrong-password' }],
    ['/api/v2/graphql', { query: '{ notAField }' }], ['/api/v2/graphql', { query: '{' }]]) {
    const r = await page.request.post(BASE + path, { data: body, failOnStatusCode: false });
    note(`POST ${path} (error)`, await r.text());
}
await page.goto(`${BASE}/login`, { waitUntil: 'networkidle' });
await page.waitForTimeout(2500);
note('login page', `${await page.title()}\n${await page.evaluate(() => document.body.innerText)}`);

// ---------------------------------------------------------------- 2. signed-in crawl
const login = await page.request.post(`${BASE}/logIn`, { data: { username: 'admin', password: PASSWORD } });
if (!login.ok()) {
    console.error(`login failed: HTTP ${login.status()}`);
    process.exit(2);
}
const visible = () => page.evaluate(() => {
    const attrs = [...document.querySelectorAll('[title],[alt],[aria-label],[placeholder],img[src]')]
        .map((e) => ['title', 'alt', 'aria-label', 'placeholder', 'src'].map((a) => e.getAttribute(a)).filter(Boolean).join(' '));
    return `${document.title}\n${document.body.innerText}\n${attrs.join('\n')}`;
});
const links = () => page.evaluate(() => [...document.querySelectorAll('a[href]')]
    .map((a) => a.getAttribute('href')).filter((h) => h && h.startsWith('/') && !h.startsWith('//')));

// Real entity pages: the UI navigates with click handlers, so links alone do not reach them.
const ENTITY_ROUTES = { DATASET: 'dataset', DATA_PRODUCT: 'dataProduct', DOMAIN: 'domain', GLOSSARY_TERM: 'glossaryTerm',
    TAG: 'tag', CORP_USER: 'user', CORP_GROUP: 'group', CHART: 'chart', DASHBOARD: 'dashboard', DATA_FLOW: 'pipelines',
    DATA_JOB: 'tasks', CONTAINER: 'container' };
for (const [type, path] of Object.entries(ENTITY_ROUTES)) {
    const r = await page.request.post(`${BASE}/api/v2/graphql`, { data: { query:
        `{ search(input: { type: ${type}, query: "*", start: 0, count: 3 }) { searchResults { entity { urn } } } }` } });
    const urns = ((await r.json()).data?.search?.searchResults ?? []).map((x) => x.entity.urn);
    for (const urn of urns) SEEDS.push(`/${path}/${encodeURIComponent(urn)}`);
}
// Entity pages repeat the same templates: visit at most a few per template.
const template = (route) => route.replace(/[?#].*$/, '').replace(/\/(urn:li:[^/]+|[^/]*%3A[^/]*)/g, '/:urn');
const perTemplate = new Map();
const queue = [...SEEDS];
const visited = new Set();
let interactions = 0;
while (queue.length && visited.size < MAX_PAGES) {
    const route = queue.shift();
    if (visited.has(route) || SKIP.test(route) || route.startsWith('/docs') || route.startsWith('/cairn/api')) continue;
    const t = template(route);
    if ((perTemplate.get(t) || 0) >= 3) continue;
    perTemplate.set(t, (perTemplate.get(t) || 0) + 1);
    visited.add(route);
    await page.goto(BASE + route, { waitUntil: 'networkidle', timeout: 60000 }).catch(() => {});
    await page.waitForTimeout(1200);
    note(`page ${route}`, await visible());
    for (const href of await links()) {
        if (!visited.has(href) && !queue.includes(href)) queue.push(href);
    }
    // Tabs, menus and dropdowns render text only when opened.
    const openers = page.locator('[role="tab"], [aria-haspopup="true"], [aria-haspopup="menu"], .ant-tabs-tab, .ant-dropdown-trigger');
    const n = Math.min(await openers.count(), 12);
    for (let i = 0; i < n; i += 1) {
        const el = openers.nth(i);
        if (!(await el.isVisible().catch(() => false))) continue;
        await el.click({ timeout: 2000 }).catch(() => {});
        await page.waitForTimeout(400);
        interactions += 1;
        note(`page ${route} (opened element ${i})`, await visible());
        await page.keyboard.press('Escape').catch(() => {});
    }
}
// Left-navigation menus that open on click (e.g. Resources).
for (const label of ['Resources', 'Settings', 'Profile']) {
    await page.goto(`${BASE}/`, { waitUntil: 'networkidle' }).catch(() => {});
    const el = page.getByText(label, { exact: true }).first();
    if (await el.isVisible().catch(() => false)) {
        await el.click().catch(() => {});
        await page.waitForTimeout(800);
        note(`menu ${label}`, await visible());
    }
}

// ---------------------------------------------------------------- 3. API prose
const gql = async (query) => (await page.request.post(`${BASE}/api/v2/graphql`, { data: { query } })).json();
const prose = (v, where) => {
    if (typeof v === 'string') {
        if (/\s/.test(v)) note(where, v);
    } else if (Array.isArray(v)) v.forEach((x) => prose(x, where));
    else if (v && typeof v === 'object') Object.entries(v).forEach(([k, x]) => prose(x, `${where}.${k}`));
};
const QUERIES = {
    appConfig: '{ appConfig { appVersion visualConfig { appTitle logoUrl faviconUrl } } }',
    me: '{ me { corpUser { username properties { displayName title } editableProperties { displayName pictureLink } } } }',
    platforms: `{ search(input: { type: DATA_PLATFORM, query: "*", start: 0, count: 200 }) { searchResults { entity {
        ... on DataPlatform { properties { displayName } } } } } }`,
    roles: '{ listRoles(input: { start: 0, count: 50 }) { roles { name description } } }',
    policies: '{ listPolicies(input: { start: 0, count: 100 }) { policies { name description } } }',
    users: '{ listUsers(input: { start: 0, count: 100 }) { users { username properties { displayName title } } } }',
    ingestion: `{ listIngestionSources(input: { start: 0, count: 100 }) { ingestionSources { name type
        executions(start: 0, count: 10) { executionRequests { result { status report
        structuredReport { serializedValue } } } } } } }`,
};
for (const [name, q] of Object.entries(QUERIES)) {
    const body = await gql(q);
    if (body.errors) findings.set(`graphql ${name}: ${JSON.stringify(body.errors).slice(0, 200)}`, 'api');
    prose(body.data, `graphql ${name}`);
    // Names of seeded objects are short identifiers that users read: check them too. (Source `type`
    // values are ingestion plugin identifiers, like URNs.)
    for (const s of JSON.stringify(body.data ?? {}).match(/"(name|displayName|title|appTitle|pictureLink|logoUrl)":"[^"]*"/g) ?? []) {
        note(`graphql ${name}`, s);
    }
}

// ---------------------------------------------------------------- 4. notification payloads
if (SINK) {
    const r = await page.request.get(SINK).catch(() => null);
    if (r?.ok()) prose(await r.json(), 'notification payloads');
}

await browser.close();
const summary = `${visited.size} pages, ${interactions} opened tabs/menus, ${docPages.length} docs pages, ` +
    `${Object.keys(QUERIES).length} API queries${SINK ? ', notification payloads' : ''}`;
if (findings.size) {
    console.log(`LEAKS (${findings.size}) — ${summary}:`);
    for (const [snippet, where] of findings) console.log(`  ${where}: …${snippet}…`);
    process.exit(1);
}
console.log(`leak scan: clean (${summary})`);
