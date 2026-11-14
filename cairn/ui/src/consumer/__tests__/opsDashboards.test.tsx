import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { formatValue, thresholdColor, tileOf, draftOf } from '../components/OpsTiles';
import { CairnProvider } from '../context/CairnProvider';
import { paletteItems } from '../guide/CommandPalette';
import { translate } from '../i18n';
import { DashboardsPage } from '../pages/DashboardsPage';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const NOW = 1_794_000_000_000;
const CONTROLS = [{ id: 'time', label: 'Time range', kind: 'time', column: '', default: '1h', refresh: 30, targets: null, source: null },
    { id: 'service', label: 'Service', kind: 'variable', column: '', default: 'cairn-api', options: null, multi: false, targets: null, source: null,
      query: { source: 'label_values', label: 'service_name' } }];
const TILES = [
    { title: 'Requests / s', width: 2, view: 'timeseries', display: { unit: 'reqps' },
      promql: { queries: [{ expr: 'sum by (service_name) (rate(x[5m]))', legend: '{{service_name}}' }], instant: false, step: null } },
    { title: 'Errors now', width: 1, view: 'gauge', display: { unit: 'percentunit', thresholds: [{ value: null, color: 'green' }, { value: 0.05, color: 'red' }] },
      promql: { queries: [{ expr: 'sum(rate(e[5m])) / sum(rate(x[5m]))', legend: '' }], instant: true, step: null } },
    { title: 'Error logs', width: 1, view: 'logs', display: {}, logql: { queries: [{ expr: '{service_name="$service"}', legend: '' }], mode: 'logs', limit: 50, step: null } },
    { title: 'Slow requests', width: 1, view: 'table', display: {}, traces: { query: '{ duration > 1s }', limit: 10 } },
    { title: 'Firing', width: 1, view: 'table', display: {}, alerts: { state: 'firing', labels: {}, severity: null } },
    { title: 'API answers', width: 1, view: 'stat', display: {}, slo: { id: 'api-answers' } },
    { title: 'Database', width: 1, view: 'stat', display: {}, postgres: { instance: 'default' } },
];
const RENDERED = {
    id: 'ops-platform-overview', owner: 'urn:li:corpuser:cairn-observability', role: 'VIEWER', name: 'Platform overview', description: '',
    shared: true, builtin: true, updated_ms: 1, controls: CONTROLS,
    window: { start_ms: NOW - 3_600_000, end_ms: NOW, variables: { service: 'cairn-api' } },
    tiles: [
        { ...TILES[0], ops: { kind: 'series', instant: false, step_s: 15, start_ms: NOW - 3_600_000, end_ms: NOW,
            series: [{ name: 'cairn-api', labels: { service_name: 'cairn-api' }, points: [[NOW - 60_000, 1.5], [NOW, 2.25]] },
                     { name: 'trino', labels: { service_name: 'trino' }, points: [[NOW - 60_000, 0.5], [NOW, null]] }] } },
        { ...TILES[1], ops: { kind: 'series', instant: true, step_s: 15, start_ms: NOW - 3_600_000, end_ms: NOW,
            series: [{ name: 'error ratio', labels: {}, points: [[NOW, 0.12]] }] } },
        { ...TILES[2], ops: { kind: 'logs', truncated: false, lines: [{ ts_ms: NOW, service: 'cairn-api', level: 'ERROR', line: 'statement failed',
            trace_id: '4bf92f3577b34da6a3ce929d0e0e4736' }] } },
        { ...TILES[3], ops: { kind: 'traces', traces: [{ trace_id: '4bf92f3577b34da6a3ce929d0e0e4736', root_service: 'cairn-api',
            root_name: 'POST /v1/statements', start_ms: NOW, duration_ms: 1500, journey: '/observability?tab=journey&trace=4bf92f3577b34da6a3ce929d0e0e4736' }] } },
        { ...TILES[4], ops: { kind: 'alerts', total: 1, alerts: [{ id: 'a1', name: 'API is slow', severity: 'critical', state: 'firing',
            labels: { service: 'cairn-api' }, value: 2, summary: 'p95 is 2s', firing_since: null, silenced_by: null, acknowledged_by: null, incident_id: 'i1' }] } },
        { ...TILES[5], ops: { kind: 'slo', slo: { id: 'api-answers', name: 'API answers' }, firing: 0, series: [], start_ms: 0, end_ms: 1,
            status: { objective: 99.5, window: '30d', sli: 99.9, budget_consumed: 0.2, budget_remaining: 0.8,
                burn: [{ long: '1h', short: '5m', factor: 14.4, long_burn: 0.5, short_burn: 0.4, burning: false }] } } },
        { ...TILES[6], status: 501, error: 'managed PostgreSQL instances (GET /v1/databases/instances/{name}/metrics) are not available on this Cairn' },
    ],
};

function page(routes: Record<string, unknown>, calls: MockCall[], at = '/dashboards') {
    const { client } = makeClient({ 'GET /ui/config': UI_CONFIG, 'GET /sql/saved': [], 'GET /sql/workbooks': [], 'GET /sql/metrics': [],
        'GET /sql/dashboards/ops-platform-overview/actions': [], ...routes }, calls);
    render(
        <MemoryRouter initialEntries={[at]}>
            <CairnProvider client={client} cache={new QueryCache()}>
                <DashboardsPage />
            </CairnProvider>
        </MemoryRouter>,
    );
}

it('formats values in their unit and colours them by threshold', () => {
    expect(formatValue(0.1234, 'percentunit')).toBe('12.3%');
    expect(formatValue(1536, 'bytes')).toBe('1.5 KiB');
    expect(formatValue(1500, 'ms')).toBe('1.5 s');
    expect(formatValue(0.25, 's')).toBe('250 ms');
    expect(formatValue(2.5, 'reqps')).toBe('2.5 req/s');
    expect(formatValue(12_345, 'short')).toBe('12.3k');
    expect(formatValue(null, 'ms')).toBe('–');
    const display = { thresholds: [{ value: null, color: 'green' }, { value: 0.05, color: 'red' }] };
    expect(thresholdColor(0.01, display)).toBe('#2f9e44');
    expect(thresholdColor(0.2, display)).toBe('#c92a2a');
    // A tile through the form and back is the same tile.
    for (const t of TILES) expect(tileOf(draftOf(t as never))).toMatchObject(t);
});

it('shows a built-in ops dashboard: series, gauge, logs, traces linked to journeys, alerts, an SLO and a clear refusal', async () => {
    const calls: MockCall[] = [];
    page({
        'GET /sql/dashboards': [{ ...RENDERED, tiles: TILES }],
        'GET /sql/dashboards/ops-platform-overview/render': RENDERED,
        'POST /sql/dashboards/ops-platform-overview/render': RENDERED,
        'GET /sql/dashboards/ops-platform-overview/controls/service/values': ['cairn-api', 'trino'],
        'POST /sql/dashboards/ops-platform-overview/copy': { ...RENDERED, id: 'mine', owner: 'urn:li:corpuser:ana', role: 'OWNER', builtin: false, name: 'Platform overview (copy)' },
        'GET /sql/dashboards/mine/render': { ...RENDERED, id: 'mine', role: 'OWNER', builtin: false, name: 'Platform overview (copy)' },
        'GET /sql/dashboards/mine/actions': [],
    }, calls, '/dashboards?open=ops-platform-overview');
    expect(await screen.findByRole('img', { name: 'Requests / s: time series of 2 series' })).toBeTruthy();
    const legend = screen.getByRole('list', { name: 'Requests / s: legend' });
    expect(within(legend).getByText('2.25 req/s')).toBeTruthy();
    expect(screen.getByRole('img', { name: 'error ratio: 12% of 100%' })).toBeTruthy();
    const logs = screen.getByRole('list', { name: 'Error logs: log lines' });
    expect(within(logs).getByText(/statement failed/)).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Journey of POST /v1/statements' }).getAttribute('href'))
        .toBe('/observability?tab=journey&trace=4bf92f3577b34da6a3ce929d0e0e4736');
    expect(screen.getByRole('link', { name: 'API is slow' }).getAttribute('href')).toBe('/incidents?incident=i1');
    expect(screen.getByRole('img', { name: /API answers: SLI \(objective 99.5%\): 99.9%/ })).toBeTruthy();
    expect(screen.getByText(/are not available on this Cairn/)).toBeTruthy();
    // The time range and a variable change what is asked for.
    fireEvent.mouseDown(screen.getByRole('combobox', { name: 'Time range' }));
    fireEvent.click(await screen.findByText('Last 6h'));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path.endsWith('/render')
        && (c.body as { controls: Record<string, unknown> }).controls.time === '6h')).toBe(true));
    fireEvent.mouseDown(screen.getByRole('combobox', { name: 'Service' }));
    fireEvent.click(await screen.findByTitle('trino'));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path.endsWith('/render')
        && (c.body as { controls: Record<string, unknown> }).controls.service === 'trino')).toBe(true));
    // A built-in dashboard is copied to be changed.
    expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Copy to edit' }));
    await waitFor(() => expect(calls.some((c) => c.path === '/sql/dashboards/ops-platform-overview/copy')).toBe(true));
});

it('builds an ops dashboard with the friendly forms: a variable, a tile previewed through Cairn, saved', async () => {
    const calls: MockCall[] = [];
    page({
        'GET /sql/dashboards': [],
        'POST /v1/observability/tiles/preview': { ops: { kind: 'series', instant: false, step_s: 15, start_ms: NOW - 900_000, end_ms: NOW,
            series: [{ name: 'cairn-api', labels: {}, points: [[NOW, 3]] }] }, window: { start_ms: NOW - 900_000, end_ms: NOW } },
        'POST /sql/dashboards': (body: unknown) => ({ ...(body as object), id: 'd9', owner: 'urn:li:corpuser:ana', updated_ms: 1 }),
        'GET /sql/dashboards/d9/render': { ...RENDERED, id: 'd9', builtin: false, role: 'OWNER', tiles: [RENDERED.tiles[0]] },
        'GET /sql/dashboards/d9/actions': [],
        'GET /sql/dashboards/d9/controls/service/values': ['cairn-api'],
    }, calls, '/dashboards?new=ops');
    const editor = await screen.findByRole('dialog', { name: 'New ops dashboard' });
    fireEvent.change(within(editor).getByLabelText('Dashboard name'), { target: { value: 'API on call' } });
    fireEvent.click(within(editor).getByRole('button', { name: 'Add variable' }));
    fireEvent.change(within(editor).getByLabelText('Variable 1 name'), { target: { value: 'service' } });
    fireEvent.change(within(editor).getByLabelText('Variable 1 default'), { target: { value: 'cairn-api' } });
    fireEvent.click(within(editor).getByRole('button', { name: 'Add tile' }));
    // Two dialogs at once: jsdom gives both the same title id, so the tile's form is found by its title text.
    const tile = await waitFor(() => {
        const d = screen.getAllByRole('dialog').find((x) => x.textContent?.startsWith('New ops tile'));
        expect(d).toBeTruthy();
        return d as HTMLElement;
    });
    fireEvent.change(within(tile).getByLabelText('Tile title'), { target: { value: 'Requests / s' } });
    fireEvent.change(within(tile).getByLabelText('Query 1'), { target: { value: 'sum(rate(traces_span_metrics_calls_total{service_name="$service"}[$__rate_interval]))' } });
    fireEvent.change(within(tile).getByLabelText('Legend 1'), { target: { value: '{{service_name}}' } });
    expect(within(tile).getByText(/Variables: \$service/)).toBeTruthy();
    fireEvent.click(within(tile).getByRole('button', { name: 'Preview' }));
    const preview = await within(tile).findByRole('region', { name: 'Preview' });
    expect(within(preview).getByRole('img', { name: 'Requests / s: time series of 1 series' })).toBeTruthy();
    const sent = calls.find((c) => c.path === '/v1/observability/tiles/preview')!.body as { tile: { promql: { queries: { expr: string }[] } };
        controls: { id: string; kind: string }[]; values: Record<string, unknown> };
    expect(sent.tile.promql.queries[0].expr).toContain('$service');
    expect(sent.controls.map((c) => c.kind)).toEqual(['time', 'variable']);
    expect(sent.values.service).toBe('cairn-api');
    fireEvent.click(within(tile).getByRole('button', { name: 'Add tile' }));
    await waitFor(() => expect(screen.queryAllByRole('dialog').some((x) => x.textContent?.startsWith('New ops tile'))).toBe(false));
    expect(within(editor).getByRole('list', { name: 'Tiles' }).textContent).toContain('Requests / s');
    fireEvent.click(within(editor).getByRole('button', { name: 'Create' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path === '/sql/dashboards')).toBe(true));
    const body = calls.find((c) => c.method === 'POST' && c.path === '/sql/dashboards')!.body as {
        name: string; tiles: { promql: unknown; view: string }[]; controls: { id: string; kind: string; query?: unknown }[] };
    expect(body.name).toBe('API on call');
    expect(body.tiles[0]).toMatchObject({ view: 'timeseries', promql: { queries: [{ legend: '{{service_name}}' }], instant: false } });
    expect(body.controls).toMatchObject([{ id: 'time', kind: 'time', default: '1h', refresh: 30 },
        { id: 'service', kind: 'variable', default: 'cairn-api', query: { source: 'label_values', label: 'service_name' } }]);
    expect(await screen.findByRole('img', { name: 'Requests / s: time series of 2 series' })).toBeTruthy();
});

it('imports a Grafana dashboard and says what it left out', async () => {
    const calls: MockCall[] = [];
    page({
        'GET /sql/dashboards': [],
        'POST /sql/dashboards/import/grafana': { dashboard: { id: 'g1', name: 'From Grafana' },
            skipped: [{ panel: 'Notes', reason: 'a text panel is not a tile' }] },
    }, calls);
    fireEvent.click(await screen.findByRole('button', { name: 'Import from Grafana' }));
    const d = await screen.findByRole('dialog', { name: 'Import a Grafana dashboard' });
    fireEvent.change(within(d).getByLabelText('Grafana dashboard JSON'), { target: { value: '{"title": "From Grafana", "panels": []}' } });
    fireEvent.click(within(d).getByRole('button', { name: 'Import' }));
    expect(await within(d).findByText('Notes: a text panel is not a tile')).toBeTruthy();
    expect(calls.find((c) => c.path === '/sql/dashboards/import/grafana')!.body).toEqual({ title: 'From Grafana', panels: [] });
});

it('the palette offers a new ops dashboard to operators', () => {
    const t = (k: Parameters<typeof translate>[1], v?: Record<string, string | number>) => translate('en', k, v);
    const withObs = paletteItems([{ to: '/observability', label: 'Observability' }], t, () => [], null, () => undefined);
    expect(withObs.find((i) => i.id === 'act:new-ops-dashboard')).toMatchObject({ label: 'New ops dashboard', to: '/dashboards?new=ops' });
    expect(paletteItems([{ to: '/discover', label: 'Discover' }], t, () => [], null, () => undefined).map((i) => i.id))
        .not.toContain('act:new-ops-dashboard');
});
