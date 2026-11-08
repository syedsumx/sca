// The add-ins' logic without a Cairn: PKCE (RFC 7636's example), formulas, the sign-in checks and one refresh
// for concurrent calls (a fake token endpoint), and the Apps Script challenge matching the browser's.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadExcel, loadSheets } from './helpers.mjs';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const dist = mkdtempSync(join(tmpdir(), 'cairn-addins-'));
execFileSync(process.execPath, [join(root, 'build.mjs'), '--cairn-url', 'https://cairn.test', '--addin-url',
    'https://addins.test', '--out', dist]);

test('build writes the Cairn and add-in addresses everywhere', () => {
    const manifest = readFileSync(join(dist, 'excel', 'manifest.xml'), 'utf8');
    assert.ok(!manifest.includes('example.com'));
    assert.match(manifest, /<SourceLocation DefaultValue="https:\/\/addins\.test\/taskpane\.html"\/>/);
    assert.match(manifest, /<AppDomain>https:\/\/cairn\.test<\/AppDomain>/);
    const config = readFileSync(join(dist, 'excel', 'config.js'), 'utf8');
    assert.match(config, /"apiBase": "https:\/\/cairn\.test\/cairn\/api"/);
    assert.match(config, /"redirectUri": "https:\/\/addins\.test\/callback\.html"/);
    assert.match(readFileSync(join(dist, 'excel', 'taskpane.html'), 'utf8'), /connect-src 'self' https:\/\/cairn\.test;/);
    const project = JSON.parse(readFileSync(join(dist, 'sheets', 'appsscript.json'), 'utf8'));
    assert.deepEqual(project.urlFetchWhitelist, ['https://cairn.test/']);
    assert.throws(() => execFileSync(process.execPath, [join(root, 'build.mjs'), '--cairn-url', 'http://cairn.test',
        '--out', join(dist, 'x')], { stdio: 'pipe' }));
});

test('PKCE S256 as RFC 7636 computes it', async () => {
    const { ctx } = loadExcel(dist);
    assert.equal(await ctx.CairnAddin.challengeOf('dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk'),
        'E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM');
});

test('formulas quote their arguments and drop trailing empty ones', () => {
    const { ctx } = loadExcel(dist);
    assert.equal(ctx.CairnAddin.formula('CAIRN.METRIC', ['revenue', 'region', '']), '=CAIRN.METRIC("revenue","region")');
    assert.equal(ctx.CairnAddin.formula('CAIRN.QUERY', ['SELECT "a" FROM t']), '=CAIRN.QUERY("SELECT ""a"" FROM t")');
});

function fakeCairn() {
    const seen = { token: [], api: [] };
    let n = 0;
    const fetchImpl = async (url, init = {}) => {
        const u = new URL(url);
        if (u.pathname.endsWith('/oauth/token')) {
            const form = new URLSearchParams(init.body);
            seen.token.push(Object.fromEntries(form));
            await new Promise((r) => setTimeout(r, 30));
            n += 1;
            return new Response(JSON.stringify({ access_token: `cat_${n}`, refresh_token: `car_${n}`, expires_in: 3600 }),
                                { status: 200, headers: { 'Content-Type': 'application/json' } });
        }
        seen.api.push(init.headers.Authorization);
        return new Response(JSON.stringify({ values: [['x'], [1]] }), { status: 200 });
    };
    return { seen, fetchImpl };
}

test('sign-in checks the state and its age; concurrent calls share one refresh', async () => {
    const { seen, fetchImpl } = fakeCairn();
    const { ctx, registry, store } = loadExcel(dist, { fetchImpl });
    await assert.rejects(registry.QUERY('SELECT 1'), (e) => e.code === '#N/A' && /Sign in to Cairn/.test(e.message));
    const client = ctx.CairnAddin.fromConfig();
    const { url } = await client.beginSignIn();
    const q = new URL(url).searchParams;
    assert.equal(q.get('code_challenge_method'), 'S256');
    assert.equal(q.get('scope'), 'read query_data model');
    assert.equal(q.get('redirect_uri'), 'https://addins.test/callback.html');
    await assert.rejects(client.completeSignIn(`https://addins.test/callback.html?code=c&state=forged`), /expired or does not match/);
    const again = await client.beginSignIn();
    const state = new URL(again.url).searchParams.get('state');
    await assert.rejects(client.completeSignIn(`https://evil.test/callback.html?code=c&state=${state}`), /unexpected address/);
    const third = await client.beginSignIn();
    const s3 = new URL(third.url).searchParams.get('state');
    await client.completeSignIn(`https://addins.test/callback.html?code=c&state=${s3}`);
    const verifier = seen.token[0].code_verifier;
    assert.ok(verifier.length >= 43 && seen.token[0].grant_type === 'authorization_code');
    assert.equal(await ctx.CairnAddin.challengeOf(verifier), new URL(third.url).searchParams.get('code_challenge'));
    // Expire the access token; five functions at once refresh once.
    const t = JSON.parse(store.get('cairn.tokens'));
    store.set('cairn.tokens', JSON.stringify({ ...t, expires_at: Date.now() - 1 }));
    ctx.CairnFunctions.reset();
    const out = await Promise.all([1, 2, 3, 4, 5].map((i) => registry.QUERY(`SELECT ${i}`)));
    assert.deepEqual(out[4], [['x'], [1]]);
    assert.equal(seen.token.filter((f) => f.grant_type === 'refresh_token').length, 1);
    assert.ok(seen.api.every((h) => h === 'Bearer cat_2'));
});

test('Apps Script: the challenge is the S256 of the verifier, custom functions never write', () => {
    const { gs, user, asCustomFunction } = loadSheets(dist, { api: 'https://cairn.test/cairn/api', scriptId: 'S'.repeat(30) });
    const url = new URL(gs.authorizationUrl());
    const pending = JSON.parse(user.getProperty('cairn.pending'));
    assert.match(pending.verifier, /^[0-9a-f]{64}$/);
    assert.equal(url.searchParams.get('code_challenge'), createHash('sha256').update(pending.verifier).digest('base64url'));
    assert.equal(url.searchParams.get('redirect_uri'), `https://script.google.com/macros/d/${'S'.repeat(30)}/usercallback`);
    assert.throws(() => asCustomFunction(() => gs.CAIRN_QUERY('SELECT 1')), /Sign in to Cairn first/);
    user.setProperty('cairn.tokens', JSON.stringify({ access_token: 'cat_x', refresh_token: 'car_x', expires_at: Date.now() - 1 }));
    assert.throws(() => asCustomFunction(() => gs.CAIRN_METRIC('revenue')), /session expired/);
    assert.equal(JSON.parse(user.getProperty('cairn.tokens')).refresh_token, 'car_x');    // nothing was spent
    gs.PropertiesService.getScriptProperties().setProperty('CAIRN_API_URL', 'http://cairn.test/cairn/api');
    assert.throws(() => gs.authorizationUrl(), /not set up/);
});
