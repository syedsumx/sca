import assert from 'node:assert/strict';
import { test } from 'node:test';

import { lookup } from '../src/lookup.js';
import { patternsFor, recognise } from '../src/match.js';
import { render } from '../src/render.js';

test('recognises Looker, Tableau and Power BI pages and their catalog key', () => {
    assert.deepEqual(recognise('https://acme.cloud.looker.com/dashboards/42?Region=EU'), {
        tool: 'looker', kind: 'dashboard', id: '42', key: 'https://acme.cloud.looker.com/dashboards/42' });
    assert.equal(recognise('https://acme.looker.com/looks/7').kind, 'look');
    assert.deepEqual(recognise('https://prod-useast-a.online.tableau.com/#/site/acme/views/Sales/Overview?:iid=1'), {
        tool: 'tableau', kind: 'view', id: 'Sales/Overview',
        key: 'https://prod-useast-a.online.tableau.com/#/site/acme/views/Sales/Overview' });
    assert.deepEqual(recognise('https://app.powerbi.com/groups/me/reports/abc-1/ReportSection2?experience=power-bi'), {
        tool: 'powerbi', kind: 'report', id: 'abc-1', key: 'https://app.powerbi.com/groups/me/reports/abc-1' });
    assert.equal(recognise('https://acme.looker.com/browse'), null);
    assert.equal(recognise('https://example.com/dashboards/1'), null);
    assert.equal(recognise('not a url'), null);
});

test('custom hosts', () => {
    assert.equal(recognise('https://bi.acme.internal/dashboards/9', { looker: ['bi.acme.internal'] }).id, '9');
    assert.deepEqual(patternsFor({ looker: ['bi.acme.internal'], tableau: [] }), ['https://bi.acme.internal/*']);
});

test('lookup states', async () => {
    const reply = (status, body) => async () => new Response(JSON.stringify(body), { status });
    assert.equal((await lookup('', 'k')).status, 'unconfigured');
    assert.equal((await lookup('https://c', 'k', reply(401, {}))).status, 'signed-out');
    assert.equal((await lookup('https://c', 'k', reply(200, { urn: null, page: 'k' }))).status, 'unknown');
    const ok = await lookup('https://c', 'k', async (url) => {
        assert.equal(url, 'https://c/cairn/api/embed/v1/context?url=k');
        return new Response(JSON.stringify({ urn: 'u', card: {} }), { status: 200 });
    });
    assert.equal(ok.status, 'ok');
    assert.equal((await lookup('https://c', 'k', async () => { throw new Error('x'); })).status, 'error');
});

test('renders the card and escapes catalog text', () => {
    const html = render({ status: 'ok', data: { card: { name: 'Revenue <b>', description: 'Daily', owners: ['urn:li:corpuser:ana'],
        certification: 'CERTIFIED', quality_status: 'FAILING', url: 'x' }, monitors: [{ name: 'freshness', status: 'FAIL' }] } });
    assert.match(html, /Revenue &lt;b&gt;/);
    assert.match(html, /✔ Certified/);
    assert.match(html, /class="pill bad">● Failing/);
    assert.match(html, /ana/);
    assert.match(render({ status: 'unknown', page: { kind: 'dashboard', key: 'k' } }), /not in the catalog yet/);
});
