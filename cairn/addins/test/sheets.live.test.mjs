// Cairn for Google Sheets against a real Cairn: Code.gs (the Apps Script project as built) runs in Node with
// stand-ins for the Apps Script services; sign-in goes through Cairn's OAuth (PKCE) and Apps Script's usercallback.
import assert from 'node:assert/strict';
import test from 'node:test';

import { consent, live, loadSheets } from './helpers.mjs';

test('Sheets: sign in with PKCE, then CAIRN_QUERY, CAIRN_METRIC and CAIRN_ASK as the person', { skip: !live.url && 'needs CAIRN_TEST_URL' }, async () => {
    const { gs, ui, triggers, cells, user, stateTokens, asCustomFunction } = loadSheets(live.dist, { api: live.url, scriptId: live.scriptId });
    gs.onOpen({ authMode: 'LIMITED' });
    assert.deepEqual(ui.menus[0].items.map((i) => i[0]), ['Sign in', 'Browse metrics', '-', 'Sign out']);
    assert.equal(ui.menus[0].added, true);
    assert.throws(() => asCustomFunction(() => gs.CAIRN_QUERY('SELECT id FROM main.orders')), /Sign in to Cairn first/);

    // The sidebar, signed out, offers Cairn's sign-in; Apps Script's state token routes the callback.
    gs.showSidebar();
    assert.match(ui.sidebars[0].getContent(), /Sign in to Cairn/);
    const state = gs.sidebarState();
    assert.equal(state.signedIn, false);
    const authorize = new URL(state.signInUrl);
    assert.equal(authorize.origin + authorize.pathname, `${live.url}/oauth/authorize`);
    assert.equal(authorize.searchParams.get('client_id'), 'agt_sheets_addon');
    assert.equal(stateTokens.at(-1).method, 'authCallback');
    const back = await consent(state.signInUrl, live.person);
    const location = new URL(back.location);
    assert.equal(location.origin + location.pathname, `https://script.google.com/macros/d/${live.scriptId}/usercallback`);
    const page = gs.authCallback({ parameter: Object.fromEntries(location.searchParams) });
    assert.match(page.getContent(), /Signed in to Cairn/);
    assert.deepEqual(triggers.map((t) => [t.handler, t.every]), [['refreshSession', '1h']]);
    const tokens = JSON.parse(user.getProperty('cairn.tokens'));
    assert.ok(tokens.expires_at - Date.now() > 3600 * 1000 && tokens.expires_at - Date.now() <= 2 * 3600 * 1000);
    // The same callback again (a replay) finds no pending sign-in.
    assert.match(gs.authCallback({ parameter: Object.fromEntries(location.searchParams) }).getContent(), /expired/);
    assert.equal(gs.sidebarState().person, 'ana');

    // Custom functions run as ana, read-only on properties (as Sheets runs them).
    const users = asCustomFunction(() => gs.CAIRN_QUERY('SELECT id, email FROM main.users ORDER BY id'));
    assert.deepEqual(users[0], ['id', 'email']);
    assert.ok(users.slice(1).every((r) => /•/.test(r[1])) && !JSON.stringify(users).includes('acme.com'), JSON.stringify(users));
    assert.throws(() => asCustomFunction(() => gs.CAIRN_QUERY('SELECT k FROM main.secrets')), /may not query main\.secrets/);
    assert.deepEqual(asCustomFunction(() => gs.CAIRN_METRIC('revenue', 'region', '')), [['region', 'revenue'], ['EU', 15.5], ['US', 7]]);
    assert.deepEqual(asCustomFunction(() => gs.CAIRN_METRIC('revenue', [['region']], [['region in EU, APAC']])),
                     [['region', 'revenue'], ['EU', 15.5]]);                     // APAC is filtered out for ana
    assert.deepEqual(asCustomFunction(() => gs.CAIRN_ASK('orders per region')), [['region', 'orders'], ['EU', 2], ['US', 1]]);

    // The sidebar: browse metrics, insert a live formula and the current values.
    assert.deepEqual(gs.listMetrics('rev').map((m) => [m.name, m.label, m.dimensions]), [['revenue', 'Revenue', ['region']]]);
    assert.equal(gs.insertFormula('metric', ['revenue', 'region', 'region = "EU"']), '=CAIRN_METRIC("revenue","region","region = ""EU""")');
    assert.deepEqual(cells.formulas, ['=CAIRN_METRIC("revenue","region","region = ""EU""")']);
    gs.insertMetricValues('revenue', 'region', '');
    assert.deepEqual(cells.values[0], { r: 3, c: 2, h: 3, w: 2, v: [['region', 'revenue'], ['EU', 15.5], ['US', 7]] });

    // Expired: a custom function cannot refresh (it could not store the rotated token) and says so; the hourly
    // trigger refreshes; then the functions work again with the new token.
    user.setProperty('cairn.tokens', JSON.stringify({ ...tokens, expires_at: Date.now() - 1, issued_at: 0 }));
    assert.throws(() => asCustomFunction(() => gs.CAIRN_METRIC('revenue')), /session expired/);
    gs.refreshSession();
    const refreshed = JSON.parse(user.getProperty('cairn.tokens'));
    assert.notEqual(refreshed.refresh_token, tokens.refresh_token);
    assert.deepEqual(asCustomFunction(() => gs.CAIRN_METRIC('revenue')), [['revenue'], [22.5]]);

    // Scopes: the token is the person's, never an administrator's.
    const admin = await fetch(`${live.url}/admin/agents`, { headers: { Authorization: `Bearer ${refreshed.access_token}` } });
    assert.equal(admin.status, 403);

    // Sign out: tokens revoked at Cairn, the trigger removed, functions ask to sign in.
    gs.signOut();
    assert.equal(user.getProperty('cairn.tokens'), null);
    assert.equal(triggers.length, 0);
    const me = await fetch(`${live.url}/me`, { headers: { Authorization: `Bearer ${refreshed.access_token}` } });
    assert.equal(me.status, 401);
    assert.throws(() => asCustomFunction(() => gs.CAIRN_ASK('anything')), /Sign in to Cairn first/);
});
