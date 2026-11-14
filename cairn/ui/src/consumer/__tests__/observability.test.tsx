import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import React from 'react';
import { MemoryRouter, Route } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { CairnProvider } from '../context/CairnProvider';
import { routeAllowed } from '../guide/capabilities';
import { paletteItems } from '../guide/CommandPalette';
import { translate } from '../i18n';
import { AllIncidents, PipelineRuns } from '../components/Operations';
import { ObservabilityPage, Span, waterfall } from '../pages/ObservabilityPage';
import { HUBS } from '../resources/hubs';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const OVERVIEW = {
    backends: { prometheus: true, loki: true, tempo: true, trace_links: false, log_links: false, vendor: null },
    firing: { critical: 1, warning: 0, info: 0 }, open_incidents: 1, slos: 1, alert_rules: 1, silences_active: 0,
};
const HEALTH = {
    window: '5m',
    components: [
        { name: 'cairn-api', state: 'degraded', rate: 4.2, error_ratio: 0.12, p95_ms: 87, up: null, targets: 0, firing_alerts: 1 },
        { name: 'trino', state: 'up', rate: 1.5, error_ratio: 0, p95_ms: 240, up: null, targets: 0, firing_alerts: 0 },
    ],
    edges: [{ from: 'cairn-api', to: 'trino', rate: 1.5, error_ratio: 0 }],
};
const TRACE = 'a'.repeat(32);
const JOURNEY = {
    trace_id: TRACE, source: 'tempo', links: {}, duration_ms: 120, services: ['cairn-api', 'trino'], errors: 0,
    spans: [
        { span_id: '1', parent_id: '', service: 'cairn-api', name: 'POST /v1/statements', kind: 'server', offset_ms: 0,
            duration_ms: 120, status: 'ok', attributes: { 'http.route': '/v1/statements' } },
        { span_id: '2', parent_id: '1', service: 'trino', name: 'POST /v1/statement', kind: 'server', offset_ms: 10,
            duration_ms: 80, status: 'ok', attributes: {} },
        { span_id: '3', parent_id: '2', service: 'cairn-api', name: 'POST /v1/trino/{key}/allow', kind: 'server', offset_ms: 20,
            duration_ms: 5, status: 'error', status_message: 'refused', attributes: {} },
    ],
};
const INCIDENT = {
    id: 'inc_1', title: 'SLO API answers: error budget burning', severity: 'critical', status: 'open', services: ['cairn-api'],
    summary: 'cairn-api burns its budget 20x too fast', assignee: null, opened_at: '2026-11-10T10:00:00Z', resolved_at: null,
    alerts: [], timeline: [
        { at: '2026-11-10T09:30:00Z', kind: 'deploy', actor: 'urn:li:corpuser:ci', by_agent: false, text: 'cairn-api 9.9.9 deployed' },
        { at: '2026-11-10T10:00:00Z', kind: 'alert_firing', actor: 'urn:li:corpuser:cairn-observability', by_agent: false,
            text: 'SLO API answers fired' },
        { at: '2026-11-10T10:05:00Z', kind: 'finding', actor: 'urn:li:corpuser:ops', by_agent: true, text: 'connection gone points nowhere' },
    ],
};
const ROUTES = {
    'GET /v1/observability/overview': OVERVIEW,
    'GET /v1/observability/health': HEALTH,
    'GET /v1/observability/traces': { items: [{ trace_id: TRACE, root_service: 'cairn-api', root_name: 'POST /v1/statements',
        start_ms: 1, duration_ms: 120 }] },
    [`GET /v1/observability/journey/${TRACE}`]: JOURNEY,
    'GET /v1/observability/incidents': { items: [INCIDENT] },
    'GET /v1/observability/incidents/inc_1': INCIDENT,
};

function page(path: string, routes: Record<string, unknown>, calls: MockCall[] = []) {
    const { client } = makeClient({ 'GET /ui/config': UI_CONFIG, ...routes }, calls);
    render(
        <MemoryRouter initialEntries={[path]}>
            <CairnProvider client={client} cache={new QueryCache()}>
                <Route path="/observability" component={ObservabilityPage} />
                <Route path="/incidents" component={AllIncidents} />
                <Route path="/runs" component={PipelineRuns} />
            </CairnProvider>
        </MemoryRouter>,
    );
}

it('the health map shows each component’s state and the calls between them', async () => {
    page('/observability', ROUTES);
    expect(await screen.findByRole('heading', { level: 1, name: 'Observability' })).toBeTruthy();
    expect(await screen.findByText('1 firing')).toBeTruthy();
    const api = await screen.findByRole('group', { name: 'cairn-api: Degraded' });
    expect(within(api).getByText(/12% errors/)).toBeTruthy();
    expect(within(api).getByText('1 alerts firing')).toBeTruthy();
    expect(screen.getByRole('group', { name: 'trino: Up' })).toBeTruthy();
    const calls = screen.getByRole('region', { name: 'Calls between components' });
    expect(within(calls).getAllByText('trino').length).toBeGreaterThan(0);
});

it('a request’s journey is a waterfall across components, spans open to their attributes', async () => {
    page(`/observability?tab=journey&trace=${TRACE}`, ROUTES);
    const fall = await screen.findByRole('list', { name: `Spans of trace ${TRACE}` });
    expect(within(fall).getAllByRole('button')).toHaveLength(3);
    expect(screen.getByText('3 spans in 2 components, 120 ms')).toBeTruthy();
    fireEvent.click(within(fall).getByRole('button', { name: 'cairn-api: POST /v1/statements, 120 ms' }));
    expect(await within(fall).findByText('/v1/statements')).toBeTruthy();
    expect(within(fall).getByText('error')).toBeTruthy();
});

it('logs of a trace are asked for with its id', async () => {
    const calls: MockCall[] = [];
    page(`/observability?tab=logs&trace=${TRACE}`, { ...ROUTES, 'GET /v1/observability/logs': { source: 'loki', items: [
        { at: '2026-11-10T10:00:00Z', service: 'cairn-api', level: 'INFO', trace_id: TRACE, line: 'statement 1 started' }] } }, calls);
    expect(await screen.findByText('statement 1 started')).toBeTruthy();
    expect(calls.some((c) => c.path === "/v1/observability/logs" && c.url.searchParams.get("trace_id") === TRACE)).toBe(true);
});

it('an incident’s timeline holds its alerts, the deploy before it and what an agent found; notes are added', async () => {
    const calls: MockCall[] = [];
    page('/incidents?incident=inc_1', { ...ROUTES, 'POST /v1/observability/incidents/inc_1/notes': INCIDENT }, calls);
    const drawer = await screen.findByRole('region', { name: `Incident ${INCIDENT.title}` });
    const timeline = within(drawer).getByRole('region', { name: 'Timeline' });
    expect(within(timeline).getByText('cairn-api 9.9.9 deployed')).toBeTruthy();
    expect(within(timeline).getByText('agent')).toBeTruthy();
    fireEvent.change(within(drawer).getByLabelText('What you found or did'), { target: { value: 'rolled back' } });
    fireEvent.click(within(drawer).getByRole('button', { name: 'Add' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true));
    expect(calls.find((c) => c.method === 'POST')!.body).toEqual({ kind: 'note', text: 'rolled back' });
    expect(within(drawer).getByRole('button', { name: 'Resolve' })).toBeTruthy();
});

it('without MANAGE_SYSTEM_OPERATIONS the page says so, and the sidebar and palette leave it out', async () => {
    page('/observability', { 'GET /v1/observability/overview': () => new Response(JSON.stringify({ detail: 'denied' }), { status: 403 }) });
    expect(await screen.findByText('Observability is for operators (MANAGE_SYSTEM_OPERATIONS).')).toBeTruthy();
    const none = { caps: { actor: 'a', is_admin: false, privileges: {} }, loading: false, error: false };
    const ops = { caps: { actor: 'a', is_admin: false, privileges: { MANAGE_SYSTEM_OPERATIONS: true } }, loading: false, error: false };
    expect(routeAllowed('/observability', HUBS, none)).toBe(false);
    expect(routeAllowed('/observability', HUBS, ops)).toBe(true);
    const t = (k: Parameters<typeof translate>[1], v?: Record<string, string | number>) => translate('en', k, v);
    const ids = (nav: { to: string; label: string }[]) => paletteItems(nav, t, () => [], null, () => undefined).map((i) => i.id);
    expect(ids([{ to: '/discover', label: 'Discover' }])).not.toContain('act:new-slo');
    const withObs = paletteItems([{ to: '/observability', label: 'Observability' }], t, () => [], null, () => undefined);
    expect(withObs.find((i) => i.id === 'act:new-slo')?.to).toBe('/observability?tab=slos&new=slo');
    expect(withObs.find((i) => i.id === 'act:find-trace')?.label).toBe('Find a request’s journey');
});

it('the waterfall nests by parent and places bars by time', () => {
    const rows = waterfall(JOURNEY.spans as Span[]);
    expect(rows.map((r) => r.depth)).toEqual([0, 1, 2]);
    expect(rows[1].left).toBeCloseTo(100 * 10 / 120);
    expect(rows[2].width).toBeGreaterThan(0);
});
