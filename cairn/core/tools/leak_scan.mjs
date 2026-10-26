// Brand-leak scan of a running Cairn stack: what a signed-in user can see must never name the
// engine or another vendor.
//
//   CAIRN_ADMIN_PASSWORD=... node core/tools/leak_scan.mjs [http://localhost:9000]
//
// Checks: the unauthenticated shell (login page, manifest, title), rendered text, tooltips, alt
// text and image URLs on the main UI routes after a real login, and prose fields in GraphQL
// responses. URNs, aspect names and other identifiers are not prose and are ignored.
// Exit code 1 when anything leaks.
import { createRequire } from 'node:module';

// CommonJS resolution, so a globally installed playwright (NODE_PATH) is found too.
const { chromium } = createRequire(import.meta.url)('playwright');

const BASE = (process.argv[2] || process.env.CAIRN_URL || 'http://localhost:9000').replace(/\/$/, '');
const PASSWORD = process.env.CAIRN_ADMIN_PASSWORD;
if (!PASSWORD) {
    console.error('set CAIRN_ADMIN_PASSWORD');
    process.exit(2);
}
const LEAK = /data\s?hub|acryl|atlan(?![a-z])/i;
const ROUTES = [
    '/', '/search?query=*', '/ingestion', '/glossary', '/domains', '/structured-properties',
    '/analytics', '/settings/tokens', '/settings/identities/users', '/settings/permissions/roles',
    '/settings/permissions/policies', '/settings/views', '/settings/preferences', '/settings/notifications',
    '/settings/home-page', '/govern/dashboard', '/tests', '/data-products', '/cairn', '/cairn/marketplace',
];
const findings = [];
const note = (where, text) => {
    const m = LEAK.exec(text);
    if (m) {
        const at = Math.max(0, m.index - 50);
        findings.push(`${where}: …${text.slice(at, m.index + 60).replace(/\s+/g, ' ')}…`);
    }
};

const browser = await chromium.launch();
const page = await browser.newPage();

// 1. Unauthenticated shell.
for (const path of ['/login', '/manifest.json']) {
    const r = await page.request.get(BASE + path);
    note(`GET ${path}`, await r.text());
}
await page.goto(`${BASE}/login`, { waitUntil: 'networkidle' });
note('login title', await page.title());
note('login page', await page.evaluate(() => document.body.innerText));

// 2. Sign in and walk the UI.
await page.request.post(`${BASE}/logIn`, { data: { username: 'admin', password: PASSWORD } });
const visible = () => page.evaluate(() => {
    const attrs = [...document.querySelectorAll('[title],[alt],[aria-label],[placeholder],img[src]')]
        .map((e) => [e.getAttribute('title'), e.getAttribute('alt'), e.getAttribute('aria-label'),
            e.getAttribute('placeholder'), e.getAttribute('src')].filter(Boolean).join(' '));
    return `${document.title}\n${document.body.innerText}\n${attrs.join('\n')}`;
});
for (const route of ROUTES) {
    await page.goto(BASE + route, { waitUntil: 'networkidle', timeout: 60000 }).catch(() => {});
    await page.waitForTimeout(1500);
    note(`page ${route}`, await visible());
}

// 3. Prose in API responses (the same session; strings without spaces are identifiers).
const gql = async (query) => (await page.request.post(`${BASE}/api/v2/graphql`, { data: { query } })).json();
const prose = (v, where) => {
    if (typeof v === 'string') {
        if (/\s/.test(v)) note(where, v);
    } else if (Array.isArray(v)) v.forEach((x, i) => prose(x, `${where}[${i}]`));
    else if (v && typeof v === 'object') Object.entries(v).forEach(([k, x]) => prose(x, `${where}.${k}`));
};
const QUERIES = {
    appConfig: '{ appConfig { appVersion visualConfig { appTitle logoUrl faviconUrl } } }',
    me: '{ me { corpUser { urn username properties { displayName title } editableProperties { displayName pictureLink } } } }',
    platforms: '{ listDataPlatforms(input: { start: 0, count: 500 }) { dataPlatforms { urn properties { displayName } } } }',
    ingestion: '{ listIngestionSources(input: { start: 0, count: 100 }) { ingestionSources { urn name type } } }',
    roles: '{ listRoles(input: { start: 0, count: 50 }) { roles { urn name description } } }',
    policies: '{ listPolicies(input: { start: 0, count: 100 }) { policies { urn name description } } }',
    users: '{ listUsers(input: { start: 0, count: 100 }) { users { urn username properties { displayName title } } } }',
};
for (const [name, q] of Object.entries(QUERIES)) {
    const body = await gql(q);
    prose(body.data, `graphql ${name}`);
    // Names of seeded objects are short identifiers, so check them even without spaces.
    for (const s of JSON.stringify(body.data ?? {}).match(/"(name|displayName|title|appTitle|pictureLink|logoUrl)":"[^"]*"/g) ?? []) {
        note(`graphql ${name}`, s);
    }
}

await browser.close();
if (findings.length) {
    console.log(`LEAKS (${findings.length}):`);
    for (const f of [...new Set(findings)]) console.log(`  ${f}`);
    process.exit(1);
}
console.log(`leak scan: clean (${ROUTES.length} routes, ${Object.keys(QUERIES).length} API queries)`);
