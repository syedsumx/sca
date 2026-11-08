// Cairn for Excel against a real Cairn (tests/test_spreadsheet_addins.py starts it and builds the add-in): the
// task pane's client signs in through Cairn's OAuth (PKCE) as the person, then the custom functions run as them.
import assert from 'node:assert/strict';
import test from 'node:test';

import { consent, live, loadExcel } from './helpers.mjs';

test('Excel: sign in with PKCE, then QUERY, METRIC and ASK as the person', { skip: !live.url && 'needs CAIRN_TEST_URL' }, async () => {
    const { ctx, registry, store, calls, client } = loadExcel(live.dist);
    assert.deepEqual(Object.keys(registry).sort(), ['ASK', 'METRIC', 'QUERY']);
    await assert.rejects(registry.QUERY('SELECT id FROM main.orders'), (e) => /Sign in to Cairn/.test(e.message));

    // Sign-in: the dialog opens Cairn's authorization page; the person allows; the callback carries the code.
    const { url } = await client.beginSignIn();
    assert.ok(url.startsWith(`${live.url}/oauth/authorize?`));
    const back = await consent(url, live.person);
    assert.deepEqual(back.scopes, ['read', 'query_data', 'model']);
    assert.ok(back.location.startsWith(`${live.addinOrigin}/callback.html?`), back.location);
    await client.completeSignIn(back.location);
    const first = JSON.parse(store.get('cairn.tokens'));
    assert.match(first.access_token, /^cat_/);
    assert.match(first.refresh_token, /^car_/);
    assert.equal(first.scope, 'read query_data model');
    assert.ok(first.expires_at - Date.now() <= 3600 * 1000);                 // short-lived
    // The code works once.
    const replay = await fetch(`${live.url}/oauth/token`, { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({ grant_type: 'authorization_code', code: new URL(back.location).searchParams.get('code'),
                                    client_id: 'agt_excel_addin', redirect_uri: `${live.addinOrigin}/callback.html`, code_verifier: 'x'.repeat(43) }) });
    assert.equal(replay.status, 400);
    // ...and a replayed code ends the grant it produced (RFC 6749 10.5): sign in again.
    await assert.rejects(client.me(), (e) => e.signIn === true);
    const second = await client.beginSignIn();
    await client.completeSignIn((await consent(second.url, live.person)).location);
    const tokens = JSON.parse(store.get('cairn.tokens'));
    assert.match((await client.me()).urn, /corpuser:ana$/);

    // =CAIRN.QUERY: the PII column comes back masked; a denied table is refused; a write is refused.
    const users = await registry.QUERY('SELECT id, email FROM main.users ORDER BY id', null);
    assert.deepEqual(users[0], ['id', 'email']);
    assert.equal(users.length, 3);
    assert.ok(users.slice(1).every((r) => /•/.test(r[1])), JSON.stringify(users));
    assert.ok(!JSON.stringify(users).includes('kim@acme.com') && !JSON.stringify(users).includes('lee@acme.com'));
    await assert.rejects(registry.QUERY('SELECT k FROM main.secrets'), (e) => e.code === '#VALUE!' && /may not query main\.secrets/.test(e.message));
    await assert.rejects(registry.QUERY('DELETE FROM main.orders'), (e) => e.code === '#N/A');

    // =CAIRN.METRIC: the governed metric, ana's row filter applied (no APAC), dimensions and filters from cells.
    assert.deepEqual(await registry.METRIC('revenue', [['region']], null), [['region', 'revenue'], ['EU', 15.5], ['US', 7]]);
    assert.deepEqual(await registry.METRIC('revenue', [['region']], [['region = EU']]), [['region', 'revenue'], ['EU', 15.5]]);
    assert.deepEqual(await registry.METRIC('revenue', null, null), [['revenue'], [22.5]]);
    await assert.rejects(registry.METRIC('nope', null, null), /no approved metric named 'nope'/);

    // =CAIRN.ASK: the answer's rows (the model is a stand-in; the SQL still runs as ana).
    assert.deepEqual(await registry.ASK('how many orders per region?'), [['region', 'orders'], ['EU', 2], ['US', 1]]);

    // The token is the person's narrowed to its scopes: administration is out of reach.
    const admin = await fetch(`${live.url}/admin/agents`, { headers: { Authorization: `Bearer ${tokens.access_token}` } });
    assert.equal(admin.status, 403);
    const write = await fetch(`${live.url}/collab/bookmarks/urn:li:dataset:x`, { method: 'PUT', headers: { Authorization: `Bearer ${tokens.access_token}` } });
    assert.equal(write.status, 403);

    // Expired: five functions at once refresh once (the refresh token rotates; using it twice would end the grant).
    store.set('cairn.tokens', JSON.stringify({ ...tokens, expires_at: Date.now() - 1 }));
    const before = calls.filter((c) => c.url.endsWith('/oauth/token')).length;
    const many = await Promise.all([1, 2, 3, 4, 5].map(() => registry.METRIC('revenue', null, null)));
    assert.ok(many.every((v) => v[1][0] === 22.5));
    assert.equal(calls.filter((c) => c.url.endsWith('/oauth/token')).length - before, 1);
    const spent = await fetch(`${live.url}/oauth/token`, { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({ grant_type: 'refresh_token', refresh_token: tokens.refresh_token, client_id: 'agt_excel_addin' }) });
    assert.equal(spent.status, 400);                       // the old refresh token is spent, and the grant with it
    await assert.rejects(registry.METRIC('revenue', null, null), (e) => /Sign in to Cairn/.test(e.message));

    // Sign in again, then sign out: the tokens stop working.
    const again = await client.beginSignIn();
    await client.completeSignIn((await consent(again.url, live.person)).location);
    const fresh = JSON.parse(store.get('cairn.tokens'));
    assert.equal((await registry.QUERY('SELECT count(*) AS n FROM main.orders'))[1][0], 3);          // row filter: no APAC
    await client.signOut();
    assert.equal(store.get('cairn.tokens'), undefined);
    const after = await fetch(`${live.url}/me`, { headers: { Authorization: `Bearer ${fresh.access_token}` } });
    assert.equal(after.status, 401);
    ctx.CairnFunctions.reset();
});
