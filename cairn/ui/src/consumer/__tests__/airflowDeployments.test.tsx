import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { RESOURCE_SAMPLES } from '../../../demo/resourceSamples';
import { CairnProvider } from '../context/CairnProvider';
import { I18nProvider } from '../i18n';
import { deploySource, type AirflowList } from '../resources/AirflowDeployments';
import { HUBS, RESOURCE_SPECS } from '../resources/catalog';
import { formFor } from '../resources/forms';
import { HubPage } from '../resources/HubPage';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const LIST = RESOURCE_SAMPLES['/v1/airflow/deployments'] as AirflowList;
const STATUS = {
    name: 'finance-dev', state: 'ready', health: 'degraded', problems: ['1 DAG file(s) do not import'],
    components: { metadatabase: { status: 'healthy' }, scheduler: { status: 'healthy' },
        dag_processor: { status: 'healthy' }, triggerer: { status: 'healthy' } },
    scheduler_heartbeat_age_s: 4.2, tasks: { queued: 1, running: 2 },
    import_errors: [{ file: 'broken.py', error: 'SyntaxError: invalid syntax' }],
};
const DEPLOYS = { deploys: [
    { id: 'd2', seq: 2, kind: 'dags', state: 'live', image: null, sha256: 'b', files: 3, git_sha: 'abc1234def', note: null,
      source: null, created_by: 'urn:li:corpuser:raj', created_ms: 1794200000000, checked: null, error: null },
    { id: 'd1', seq: 1, kind: 'dags', state: 'superseded', image: null, sha256: 'a', files: 2, git_sha: null, note: 'first',
      source: null, created_by: 'urn:li:corpuser:raj', created_ms: 1794100000000, checked: null, error: null }] };
const PROMOTIONS = { promotions: [{ id: 'p1', deploy: 'd2', source: 'finance-dev', target: 'finance-prod', state: 'pending',
    requested_by: 'urn:li:corpuser:raj', requested_ms: 1794200000000, decided_by: null, comment: null, error: null }] };

function wrap(routes: Record<string, unknown>, calls: MockCall[], at: string) {
    const { client, cache } = makeClient({ 'GET /ui/config': UI_CONFIG, 'GET /notify/inbox': [], ...routes }, calls);
    const hub = HUBS.find((h) => h.route === '/compute')!;
    return render(
        <MemoryRouter initialEntries={[at]}>
            <I18nProvider initial="en">
                <CairnProvider client={client} cache={cache}><HubPage hub={hub} /></CairnProvider>
            </I18nProvider>
        </MemoryRouter>,
    );
}

it('is a tab of Compute & jobs with a generated create form, answered by the demo', () => {
    expect(HUBS.find((h) => h.route === '/compute')!.tabs).toContain('airflow-deployments');
    const spec = RESOURCE_SPECS['airflow-deployments'];
    expect(spec.view).toBe('airflow-deployments');
    const form = formFor(spec.actions![0])!;
    expect(form.source).toBe('registry:airflow_deployments');
    expect(form.fields.map((f) => f.name)).toEqual(expect.arrayContaining(['name', 'environment', 'secrets', 'hibernation']));
    expect(LIST.deployments.map((d) => d.name)).toEqual(['finance-dev', 'finance-prod']);
    expect(deploySource({ source: 'rollback:d1', git_sha: null })).toBe('rollback to d1');
    expect(deploySource({ source: 'promoted:finance-dev/d2', git_sha: 'abc1234def' })).toBe('promoted from finance-dev (git abc1234)');
});

it('shows a deployment\'s health, rolls back a deploy and approves a promotion', async () => {
    const calls: MockCall[] = [];
    wrap({
        'GET /v1/airflow/deployments': LIST,
        'GET /v1/airflow/deployments/finance-dev/status': STATUS,
        'GET /v1/airflow/deployments/finance-dev/deploys': DEPLOYS,
        'GET /v1/airflow/deployments/finance-dev/promotions': PROMOTIONS,
        'POST /v1/airflow/deployments/finance-dev/deploys/d1/rollback': { id: 'd3', state: 'live' },
        'POST /v1/airflow/promotions/p1/approve': { id: 'p1', state: 'applied' },
        'POST /v1/airflow/deployments/finance-dev/hibernate': { name: 'finance-dev', state: 'hibernating' },
    }, calls, '/compute?tab=airflow-deployments');
    // The environments are cards in promotion order (dev before prod); the first is picked and scopes what follows.
    const dev = await screen.findByRole('button', { name: 'finance-dev' });
    const prod = screen.getByRole('button', { name: 'finance-prod' });
    expect(dev.compareDocumentPosition(prod) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'finance: dev → prod' })).toBeTruthy();
    fireEvent.click(dev);
    expect(dev.getAttribute('aria-pressed')).toBe('true');
    expect(prod.getAttribute('aria-pressed')).toBe('false');
    const card = screen.getByRole('article', { name: 'finance-dev' });
    expect(within(card).getByText('mon–fri 07:00–20:00 Europe/Berlin')).toBeTruthy();
    expect(within(card).getByText('Ready').closest('[data-tone]')?.getAttribute('data-tone')).toBe('ok');
    const health = await screen.findByRole('region', { name: 'finance-dev health' });
    expect(await within(health).findByText('Degraded')).toBeTruthy();
    expect(within(health).getByText('broken.py')).toBeTruthy();
    expect(within(health).getByText('2 running, 1 queued')).toBeTruthy();
    fireEvent.click(within(card).getByRole('button', { name: 'Hibernate' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path.endsWith('/finance-dev/hibernate'))).toBe(true));
    const deploys = screen.getByRole('region', { name: 'Deploys to finance-dev' });
    expect(await within(deploys).findByText('deployed (git abc1234)')).toBeTruthy();
    expect(within(deploys).getByText('Replaced')).toBeTruthy();
    fireEvent.click(within(deploys).getByRole('button', { name: 'Roll back' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path.endsWith('/deploys/d1/rollback'))).toBe(true));
    const waiting = screen.getByRole('region', { name: 'Promotions waiting' });
    fireEvent.click(await within(waiting).findByRole('button', { name: 'Approve' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path === '/v1/airflow/promotions/p1/approve')).toBe(true));
    // Picking another environment scopes the deploys, promotions and health to it.
    fireEvent.click(prod);
    expect(prod.getAttribute('aria-pressed')).toBe('true');
    expect(await screen.findByRole('region', { name: 'Deploys to finance-prod' })).toBeTruthy();
    expect(screen.getByRole('region', { name: 'finance-prod health' })).toBeTruthy();
    expect(screen.queryByRole('region', { name: 'Deploys to finance-dev' })).toBeNull();
});

it('the command palette\'s action opens the generated create form, which posts the deployment', async () => {
    const calls: MockCall[] = [];
    wrap({ 'GET /v1/airflow/deployments': LIST, 'POST /v1/airflow/deployments': { name: 'ops-dev', state: 'ready' } },
        calls, '/compute?tab=airflow-deployments&action=New%20deployment');
    const dialog = (await screen.findAllByRole('dialog')).slice(-1)[0];
    fireEvent.change(await within(dialog).findByLabelText(/^Name/), { target: { value: 'ops-dev' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'New deployment' }));
    await waitFor(() => expect(calls.find((c) => c.method === 'POST' && c.path === '/v1/airflow/deployments')).toBeTruthy());
    const call = calls.find((c) => c.method === 'POST' && c.path === '/v1/airflow/deployments')!;
    expect((call.body as Record<string, unknown>).name).toBe('ops-dev');
});
