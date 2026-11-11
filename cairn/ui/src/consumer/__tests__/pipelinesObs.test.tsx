import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import React from 'react';
import { MemoryRouter, Route } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { duration, gantt } from '../components/PipelinesObs';
import { CairnProvider } from '../context/CairnProvider';
import { AllIncidents, PipelineRuns } from '../components/Operations';
import { ObservabilityPage } from '../pages/ObservabilityPage';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const OVERVIEW = {
    backends: { prometheus: true, loki: true, tempo: true, trace_links: false, log_links: false, vendor: null },
    firing: { critical: 1, warning: 0, info: 0 }, open_incidents: 1, slos: 0, alert_rules: 0, silences_active: 0,
};
const DAILY = 'urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.daily,PROD)';
const TASKS = [
    { task: 'extract', state: 'success', try: 1, offset_ms: 0, duration_ms: 20_000 },
    { task: 'load', state: 'failed', try: 2, offset_ms: 20_000, duration_ms: 40_000, error: 'out of memory' },
];
const RUN = {
    id: 'prn_1', pipeline: 'airflow:daily_sales', source: 'airflow', run_id: 'manual__1', ol_run_id: null, state: 'failed',
    start: '2026-11-10T10:00:00Z', end: '2026-11-10T10:01:00Z', start_ms: 1, duration_ms: 60_000, trace_id: 'b'.repeat(32),
    error: 'failed tasks: load', inputs: [], outputs: [DAILY], tasks: TASKS, anomaly: { duration_ms: 60_000, mean_ms: 20_000 },
};
const SUMMARY = {
    key: 'airflow:daily_sales', source: 'airflow', name: 'daily_sales', health: 'failing', runs: 3, success_rate: 0.6667,
    p50_ms: 20_000, p95_ms: 60_000, anomalies: 1, slas: ['sales-by-six'], data_products: ['sales'], last_run: RUN,
    trend: [{ run_id: 'a', state: 'success', duration_ms: 20_000, start: null }, { run_id: 'b', state: 'success', duration_ms: 21_000, start: null },
        { run_id: 'manual__1', state: 'failed', duration_ms: 60_000, start: null }],
};
const ROUTES = {
    'GET /v1/observability/overview': OVERVIEW,
    'GET /v1/observability/pipelines': { items: [SUMMARY, { ...SUMMARY, key: 'flow:nightly', source: 'flow', name: 'nightly',
        health: 'healthy', slas: [], last_run: null, trend: [] }] },
    'GET /v1/observability/pipelines/airflow:daily_sales': { ...SUMMARY, items: [RUN] },
    'GET /v1/observability/pipelines/airflow:daily_sales/runs/manual__1': {
        ...RUN, airflow_trace_id: 'c'.repeat(32), links: {},
        impact: { outputs: [DAILY], tables: [{ urn: DAILY, name: 'sales.daily', hops: 0 },
            { urn: 'urn:li:dataset:(urn:li:dataPlatform:iceberg,finance.report,PROD)', name: 'finance.report', hops: 1 }],
        assets: [{ kind: 'dashboard', id: 'd1', name: 'Revenue' }], data_products: [{ id: 'sales', name: 'Sales', tables: [DAILY] }],
        pipelines: [{ pipeline: 'dbt:finance', tables: [DAILY] }] },
        quality: [{ table: DAILY, monitor: 'm1', name: 'daily not empty', type: 'volume', status: 'FAIL', message: '0 rows', at: '2026-11-10T10:02:00Z' }],
    },
    'GET /v1/observability/pipelines/airflow:daily_sales/runs/manual__1/tasks/load/log': {
        lines: ['ERROR Task failed: out of memory writing to postgresql://[redacted]@db/wh'], source: 'airflow' },
    'GET /v1/observability/pipelines/airflow:daily_sales/runs/manual__1/diagnose': {
        causes: [{ kind: 'resource', text: 'it ran out of a resource', likelihood: 0.6 }],
        suggestions: [{ kind: 'runbook_action', action: 'rerun_pipeline', rationale: 'rerun the failed tasks once the cause is gone' }],
        log: { task: 'load', tail: [] } },
    'POST /v1/observability/pipelines/airflow:daily_sales/runs/manual__1/rerun': { rerun: 'airflow', cleared: ['load'] },
    'GET /v1/observability/data-products': { items: [{ id: 'sales', name: 'Sales', tables: [DAILY], pipelines: ['airflow:daily_sales'] }] },
    'GET /v1/observability/pipeline-slas': { items: [{ id: 'sales-by-six', name: 'Sales by six', pipeline: '', data_product: 'sales',
        deadline: '06:00', timezone: 'UTC', max_duration: '', freshness: '26h', on_failure: true, anomalies: false, severity: 'critical' }] },
    'GET /v1/observability/exporters': { items: [{ id: 'customer', name: 'Our Collector', protocol: 'http', endpoint: 'https://otel.example.com',
        signals: ['traces', 'logs'], enabled: true, headers: [{ name: 'authorization', scope: 'otel', key: 'token' }] }] },
    'GET /v1/observability/exporters/collector-config': { yaml: 'exporters:\n  otlp_http/customer: {}\n', env: ['CAIRN_EXPORTER_CUSTOMER_0'] },
    'POST /v1/observability/exporters/apply': { written: ['exporters.yaml', 'exporters.env'], pipelines: [] },
};

function page(path: string, calls: MockCall[] = []) {
    const { client } = makeClient({ 'GET /ui/config': UI_CONFIG, ...ROUTES }, calls);
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

it('the pipelines list shows each pipeline’s health, success rate, durations and its last runs', async () => {
    page('/runs?tab=pipelines');
    const list = await screen.findByRole('region', { name: 'Pipelines' });
    expect(await within(list).findByText('Failing')).toBeTruthy();
    expect(within(list).getAllByText('66.7%').length).toBe(2);
    expect(within(list).getAllByText('20 s').length).toBeGreaterThan(0);
    expect(within(list).getByRole('img', { name: 'Durations of the last 3 runs of airflow:daily_sales' })).toBeTruthy();
    fireEvent.change(within(list).getByLabelText('Filter pipelines'), { target: { value: 'nightly' } });
    await waitFor(() => expect(within(list).queryByText('Failing')).toBeNull());
    expect(within(list).getByText('Healthy')).toBeTruthy();
    // Data products and SLAs below.
    expect(screen.getByRole('region', { name: 'Data products' })).toBeTruthy();
    expect(await screen.findByText('failure · by 06:00 UTC · fresh 26h')).toBeTruthy();
});

it('a run: tasks as a Gantt, the failed task’s log, what it reaches, quality, causes and a rerun', async () => {
    const calls: MockCall[] = [];
    page('/runs?tab=pipelines&pipeline=airflow:daily_sales&run=manual__1', calls);
    const g = await screen.findByRole('list', { name: 'Tasks of run manual__1' });
    expect(within(g).getByRole('listitem', { name: 'load: failed, 40 s' })).toBeTruthy();
    expect(screen.getByText('Slower than usual (1 min 00 s against 20 s on average)')).toBeTruthy();
    const impact = screen.getByRole('region', { name: 'What it reaches' });
    expect(within(impact).getByText('finance.report')).toBeTruthy();
    expect(within(impact).getByText('dashboard: Revenue')).toBeTruthy();
    expect(within(impact).getByText('dbt:finance')).toBeTruthy();
    expect(within(screen.getByRole('region', { name: 'Quality of what it wrote' })).getByText('FAIL')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Log of load' }));
    const log = await screen.findByRole('region', { name: 'Log of load' });
    expect(await within(log).findByText(/out of memory writing to postgresql:\/\/\[redacted\]/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Why did it fail?' }));
    const causes = await screen.findByRole('region', { name: 'Likely causes' });
    expect(within(causes).getByText('resource')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Rerun failed tasks' }));
    fireEvent.click(await screen.findByRole('button', { name: 'OK' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path.endsWith('/rerun'))).toBe(true));
});

it('export: destinations with headers from secret scopes, the Collector configuration and apply', async () => {
    const calls: MockCall[] = [];
    page('/observability?tab=export', calls);
    const region = await screen.findByRole('region', { name: 'OpenTelemetry export' });
    expect(await within(region).findByText('authorization ← otel/token')).toBeTruthy();
    fireEvent.click(within(region).getByRole('button', { name: 'Collector configuration' }));
    const cfg = await screen.findByRole('region', { name: 'Collector configuration' });
    expect(within(cfg).getByText(/CAIRN_EXPORTER_CUSTOMER_0/)).toBeTruthy();
    fireEvent.click(within(region).getByRole('button', { name: 'Apply' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path === '/v1/observability/exporters/apply')).toBe(true));
});

it('durations read as people say them and Gantt bars sit where their tasks ran', () => {
    expect(duration(850)).toBe('850 ms');
    expect(duration(12_500)).toBe('12.5 s');
    expect(duration(245_000)).toBe('4 min 05 s');
    expect(duration(3_720_000)).toBe('1 h 02 min');
    const bars = gantt({ tasks: TASKS, duration_ms: 60_000 } as never);
    expect(bars[1].left).toBeCloseTo(100 * 20 / 60);
    expect(bars[1].width).toBeCloseTo(100 * 40 / 60);
});
