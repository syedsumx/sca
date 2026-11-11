import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import React from 'react';
import { MemoryRouter, Route } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { CairnProvider } from '../context/CairnProvider';
import type { AirflowDagDetail, AirflowDagSummary } from '../flows/AirflowTab';
import { FlowsPage } from '../pages/FlowsPage';
import type { ChangeRequest } from '../resources/ChangesPanel';
import { ChangesPanel } from '../resources/ChangesPanel';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const ME = 'urn:li:corpuser:ana';

const SUMMARY: AirflowDagSummary = {
    dag_id: 'orders_daily', description: 'Orders, daily', owners: ['ana'], tags: ['team:sales'], is_paused: false, schedule: '0 1 * * *',
    schedule_text: 'At 01:00', has_import_errors: false, cairn_flow: null,
    last_run: { dag_run_id: 'r1', state: 'running', start_date: null, end_date: null, run_type: 'scheduled' },
};

function detail(states: Record<string, string>, convertible = true): AirflowDagDetail {
    const map: Record<string, string> = { success: 'ok', running: 'running', scheduled: 'pending', skipped: 'skipped' };
    return {
        ...SUMMARY, timezone: 'UTC', file: 'orders_daily.py',
        steps: [
            { name: 'load', type: 'sql', after: [], operator: 'CairnTrinoOperator', trigger_rule: 'all_success', retries: 2 },
            { name: 'models', type: 'dbt', after: ['load'], operator: 'CairnDbtOperator', trigger_rule: 'all_success' },
            { name: 'notify', type: 'python', after: ['models'], operator: 'PythonOperator', trigger_rule: 'all_done' },
        ],
        runs: [{ dag_run_id: 'r1', state: Object.values(states).includes('running') ? 'running' : 'success', run_type: 'scheduled', start_date: null, end_date: null }],
        run: 'r1', run_state: Object.values(states).includes('running') ? 'running' : 'success', states,
        statuses: Object.fromEntries(Object.entries(states).map(([k, v]) => [k, map[v] ?? 'running'])),
        conversion: convertible
            ? { convertible: true, blockers: [], notes: [], flow: {} }
            : { convertible: false, notes: [], flow: null, blockers: [{ task: 'notify', operator: 'PythonOperator', reason: 'PythonOperator has no flow step' }] },
    };
}

function flowsPage(routes: Record<string, unknown>, calls: MockCall[], at = '/flows?tab=airflow') {
    const { client } = makeClient({
        'GET /ui/config': UI_CONFIG, 'GET /me': { urn: ME }, 'GET /v1/flows': [], 'GET /v1/flows/approvals': [], ...routes,
    }, calls);
    let location = '';
    render(
        <MemoryRouter initialEntries={[at]}>
            <CairnProvider client={client} cache={new QueryCache()}>
                <FlowsPage />
                <Route path="*" render={({ location: l }) => { location = `${l.pathname}${l.search}`; return null; }} />
            </CairnProvider>
        </MemoryRouter>,
    );
    return () => location;
}

describe('the Airflow tab', () => {
    it('shows a DAG read-only on the canvas with its live task states, and converts it', async () => {
        const calls: MockCall[] = [];
        let polls = 0;
        const where = flowsPage({
            'GET /v1/airflow/dags': [SUMMARY],
            'GET /v1/airflow/dags/orders_daily': () => {
                polls += 1;
                return detail(polls === 1 ? { load: 'success', models: 'running' } : { load: 'success', models: 'success', notify: 'success' });
            },
            'POST /v1/airflow/dags/orders_daily/convert': { flow: 'orders-daily', notes: [] },
            'GET /v1/flows/orders-daily': new Error('not in this test'),
        }, calls);
        expect(await screen.findByRole('tab', { name: 'Airflow', selected: true })).toBeTruthy();
        fireEvent.click(await screen.findByRole('button', { name: 'orders_daily' }));
        const canvas = await screen.findByTestId('flow-canvas');
        // The canvas draws the DAG's tasks with their order and states, and is read-only: nothing to drop or connect.
        expect(within(canvas).getByRole('button', { name: /Step load, SQL query, ok/ })).toBeTruthy();
        expect(within(canvas).getByRole('button', { name: /Step models, dbt, running/ })).toBeTruthy();
        expect(canvas.querySelector('[data-edge="load->models"]')).toBeTruthy();
        expect(canvas.querySelector('.fl-handle.out')).toBeNull();
        expect(screen.getByText('read-only')).toBeTruthy();
        // A run in progress: it looks again until the run ends.
        await waitFor(() => expect(within(screen.getByTestId('flow-canvas')).getByRole('button', { name: /Step models, dbt, ok/ })).toBeTruthy(),
            { timeout: 6000 });
        fireEvent.keyDown(within(screen.getByTestId('flow-canvas')).getByRole('button', { name: /Step load/ }), { key: 'Enter' });
        expect(screen.getByLabelText('Task load').textContent).toContain('CairnTrinoOperator');
        fireEvent.change(screen.getByLabelText('Flow name (optional)'), { target: { value: 'orders-daily' } });
        fireEvent.click(screen.getByRole('button', { name: 'Convert to Cairn flow' }));
        await waitFor(() => expect(calls.some((c) => c.path === '/v1/airflow/dags/orders_daily/convert')).toBe(true));
        expect(calls.find((c) => c.path === '/v1/airflow/dags/orders_daily/convert')!.body).toEqual({ name: 'orders-daily' });
        await waitFor(() => expect(where()).toBe('/flows?flow=orders-daily'));
    }, 15000);

    it('says which tasks keep a DAG from converting', async () => {
        flowsPage({ 'GET /v1/airflow/dags': [SUMMARY], 'GET /v1/airflow/dags/orders_daily': detail({ load: 'success' }, false) }, []);
        fireEvent.click(await screen.findByRole('button', { name: 'orders_daily' }));
        const list = await screen.findByRole('list', { name: 'What blocks conversion' });
        expect(list.textContent).toContain('notify (PythonOperator): PythonOperator has no flow step');
        expect(screen.queryByRole('button', { name: 'Convert to Cairn flow' })).toBeNull();
    });

    it('lists none when Airflow has no DAG of yours', async () => {
        flowsPage({ 'GET /v1/airflow/dags': [] }, []);
        expect(await screen.findByText(/No Airflow DAGs of yours/)).toBeTruthy();
    });
});

const CHANGE: ChangeRequest = {
    id: 'c1', title: 'Revenue report', status: 'proposed', author: ME, agent: 'agt_x', sponsor: ME, intent: 'A report for sales',
    allow_delete: false, kinds: ['queries', 'flows'], approved_by: null, approved_ms: null, comment: null, applied_by: null, applied_ms: null,
    error: null, created_ms: 1, updated_ms: 1, commit: null, variables: ['PW'], may_approve: true, may_apply: false, result: null,
    bundle: 'queries: []\n', terraform: 'resource "cairn_saved_query" "r" {}\n', history: [{ at_ms: 1, by: ME, action: 'proposed' }],
    plan: {
        digest: 'd', counts: { create: 1, update: 1, replace: 0, delete: 0, unchanged: 0 },
        changes: [
            { action: 'update', kind: 'queries', key: 'revenue', terraform_type: 'cairn_saved_query', fields: [{ field: 'sql', before: 'SELECT 1', after: 'SELECT 2' }] },
            { action: 'create', kind: 'flows', key: 'nightly', terraform_type: 'cairn_flow', fields: [{ field: 'name', before: null, after: 'nightly' }] },
        ],
    },
};

function changes(routes: Record<string, unknown>, calls: MockCall[]) {
    const { client } = makeClient({ 'GET /ui/config': UI_CONFIG, 'GET /v1/git-folders': [], ...routes }, calls);
    render(
        <MemoryRouter>
            <CairnProvider client={client} cache={new QueryCache()}>
                <ChangesPanel />
            </CairnProvider>
        </MemoryRouter>,
    );
}

describe('change requests', () => {
    it('shows the plan field by field, approves with a comment and applies with variables', async () => {
        const calls: MockCall[] = [];
        let state: ChangeRequest = { ...CHANGE };
        changes({
            'GET /v1/iac/changes/assistant': { ready: true, model: 'm', why: null },
            'GET /v1/iac/changes': () => [state],
            'GET /v1/iac/changes/c1': () => state,
            'POST /v1/iac/changes/c1/approve': () => { state = { ...state, status: 'approved', may_approve: false, may_apply: true, approved_by: 'urn:li:corpuser:bob' }; return state; },
            'POST /v1/iac/changes/c1/apply': () => { state = { ...state, status: 'applied', may_apply: false }; return state; },
        }, calls);
        fireEvent.click(await screen.findByRole('button', { name: 'Revenue report' }));
        const update = await screen.findByLabelText('update queries/revenue');
        expect(within(update).getByText('SELECT 1')).toBeTruthy();
        expect(within(update).getByText('SELECT 2')).toBeTruthy();
        expect(within(screen.getByLabelText('create flows/nightly')).getByRole('link', { name: 'Open in canvas' }).getAttribute('href'))
            .toBe('/flows?change=c1&preview=nightly');
        fireEvent.change(screen.getByLabelText('Comment'), { target: { value: 'fine' } });
        fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
        await waitFor(() => expect(calls.find((c) => c.path === '/v1/iac/changes/c1/approve')?.body).toEqual({ comment: 'fine' }));
        fireEvent.change(await screen.findByLabelText('Value of PW'), { target: { value: 's3cret-value' } });
        fireEvent.click(screen.getByRole('button', { name: 'Apply' }));
        await waitFor(() => expect(calls.find((c) => c.path === '/v1/iac/changes/c1/apply')?.body).toEqual({ variables: { PW: 's3cret-value' } }));
        expect((await screen.findAllByText('Applied')).length).toBeGreaterThan(0);
    });

    it('asks an agent for a flow and opens it in the canvas; without a model it says why', async () => {
        const calls: MockCall[] = [];
        changes({
            'GET /v1/iac/changes/assistant': { ready: true, model: 'm', why: null },
            'GET /v1/iac/changes': [],
            'GET /v1/iac/changes/c1': CHANGE,
            'POST /v1/iac/changes/draft': { reply: 'I proposed the flow nightly; it waits for approval.', change: CHANGE, flows: [{ name: 'nightly' }], tool_calls: [] },
        }, calls);
        fireEvent.change(await screen.findByLabelText('Describe the change'), { target: { value: 'a nightly flow that refreshes X, checks it, emails it' } });
        fireEvent.click(screen.getByRole('button', { name: 'Draft the change' }));
        const link = await screen.findByRole('link', { name: 'Open the flow nightly in the canvas' });
        expect(link.getAttribute('href')).toBe('/flows?change=c1&preview=nightly');
        expect(calls.find((c) => c.path === '/v1/iac/changes/draft')?.body).toEqual({ prompt: 'a nightly flow that refreshes X, checks it, emails it' });
    });

    it('says plainly when no model is configured', async () => {
        changes({ 'GET /v1/iac/changes/assistant': { ready: false, model: null, why: 'No model is configured for Ask an agent.' },
            'GET /v1/iac/changes': [] }, []);
        expect(await screen.findByText('No model is configured for Ask an agent.')).toBeTruthy();
        expect(screen.queryByRole('button', { name: 'Draft the change' })).toBeNull();
    });

    it('opens a proposed flow read-only on the canvas', async () => {
        const proposed = { ...CHANGE, plan: { ...CHANGE.plan, changes: [{ ...CHANGE.plan.changes[1], fields: [
            { field: 'spec', before: null, after: { steps: [{ name: 'refresh', type: 'derived', table: 't' }, { name: 'email', type: 'notify', after: ['refresh'], message: 'hi' }] } }] }] } };
        flowsPage({ 'GET /v1/iac/changes/c1': proposed }, [], '/flows?change=c1&preview=nightly');
        const canvas = await screen.findByTestId('flow-canvas');
        expect(within(canvas).getByRole('button', { name: /Step email, Notify/ })).toBeTruthy();
        expect(screen.getByText('proposed draft')).toBeTruthy();
    });
});
