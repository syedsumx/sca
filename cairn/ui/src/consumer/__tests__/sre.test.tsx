import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import React from 'react';
import { MemoryRouter, Route } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { CairnProvider } from '../context/CairnProvider';
import { DiagnoseButton } from '../components/SreAgent';
import { paletteItems } from '../guide/CommandPalette';
import { translate } from '../i18n';
import { AllIncidents, PipelineRuns } from '../components/Operations';
import { ObservabilityPage } from '../pages/ObservabilityPage';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const EVIDENCE = [{ id: 'e3', tool: 'pg_diagnose', input: { instance: 'shop-pg' },
    result: '{"index_suggestions": [{"plan_node": "Seq Scan", "filter": "(customer_id = $1)"}]}' }];
const INCIDENT = {
    id: 'inc_1', title: 'Shop API is slow', severity: 'critical', status: 'open', services: ['shop-api'], summary: null,
    assignee: null, opened_at: '2026-11-10T10:00:00Z', resolved_at: null, alerts: [], timeline: [
        { at: '2026-11-10T10:01:00Z', kind: 'finding', actor: 'urn:li:cairnAgent:sre-agent', by_agent: true,
            text: 'shop-pg: Seq Scan on public.orders filtering customer_id', detail: { evidence: EVIDENCE } },
    ],
};
const ACTION = {
    id: 'act_1', kind: 'runbook_action', action: 'restart_instance', params: { instance: 'shop-pg' }, change_id: null,
    title: 'restart instance: instance=shop-pg', rationale: '34 sessions idle in a transaction fill the slots',
    evidence: EVIDENCE, status: 'pending', decided_by: null, error: null,
};
const CHANGE = { ...ACTION, id: 'act_2', kind: 'change_request', action: null, change_id: 'c0ffee00c0ffee00c0ffee00c0ffee00',
    title: 'Index public.orders on customer_id in shop', rationale: 'the slow statement scans all rows' };
const SRE = {
    runs: [{ id: 'run_1', kind: 'investigate', status: 'done', acts_for: 'urn:li:corpuser:ops', asked_by: null, question: null,
        answer: 'proposed', steps: [], error: null, started_at: '2026-11-10T10:00:30Z', finished_at: '2026-11-10T10:01:00Z' },
    { id: 'run_2', kind: 'ask', status: 'done', acts_for: 'urn:li:corpuser:ana', asked_by: 'urn:li:corpuser:ana',
        question: 'Is shop-pg healthy?', answer: 'shop-pg is degraded [e2].', steps: [], error: null,
        started_at: '2026-11-10T10:02:00Z', finished_at: '2026-11-10T10:02:05Z' }],
    actions: [ACTION, CHANGE], postmortem: null,
    status: { ready: true, why: null, model: 'm', enabled: true },
};
const ROUTES = {
    'GET /v1/observability/overview': { backends: { prometheus: true, loki: true, tempo: true, trace_links: false, log_links: false,
        vendor: null }, firing: { critical: 1, warning: 0, info: 0 }, open_incidents: 1, slos: 0, alert_rules: 1, silences_active: 0 },
    'GET /v1/observability/incidents': { items: [INCIDENT] },
    'GET /v1/observability/incidents/inc_1': INCIDENT,
    'GET /v1/sre/incidents/inc_1': SRE,
};

function page(routes: Record<string, unknown>, calls: MockCall[]) {
    const { client } = makeClient({ 'GET /ui/config': UI_CONFIG, ...routes }, calls);
    render(
        <MemoryRouter initialEntries={['/incidents?incident=inc_1']}>
            <CairnProvider client={client} cache={new QueryCache()}>
                <Route path="/observability" component={ObservabilityPage} />
                <Route path="/incidents" component={AllIncidents} />
                <Route path="/runs" component={PipelineRuns} />
            </CairnProvider>
        </MemoryRouter>,
    );
}

it('the incident shows the agent’s cited finding, its proposals with Approve and Reject, and the chat', async () => {
    const calls: MockCall[] = [];
    page({ ...ROUTES, 'POST /v1/sre/actions/act_1/approve': { ...ACTION, status: 'executed', decided_by: 'urn:li:corpuser:ana' },
        'POST /v1/sre/actions/act_2/reject': { ...CHANGE, status: 'rejected' },
        'POST /v1/sre/incidents/inc_1/ask': SRE.runs[1] }, calls);
    const panel = await screen.findByRole('region', { name: 'SRE agent' });
    const timeline = screen.getByRole('region', { name: 'Timeline' });
    expect(within(timeline).getByText('Evidence (1)')).toBeTruthy();
    expect(within(timeline).getByText('pg_diagnose')).toBeTruthy();
    const restart = within(panel).getByRole('group', { name: `Proposal ${ACTION.title}` });
    expect(within(restart).getByText('waits for approval')).toBeTruthy();
    expect(within(restart).getByText('runbook action')).toBeTruthy();
    fireEvent.click(within(restart).getByRole('button', { name: `Approve ${ACTION.title}` }));
    fireEvent.click(await screen.findByRole('button', { name: 'Approve' }));          // the confirmation: it runs as you
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path === '/v1/sre/actions/act_1/approve')).toBe(true));
    const change = within(panel).getByRole('group', { name: `Proposal ${CHANGE.title}` });
    expect(within(change).getByText('change request')).toBeTruthy();
    fireEvent.click(within(change).getByRole('button', { name: `Reject ${CHANGE.title}` }));
    await waitFor(() => expect(calls.some((c) => c.path === '/v1/sre/actions/act_2/reject')).toBe(true));
    expect(within(panel).getByText('shop-pg is degraded [e2].')).toBeTruthy();
    fireEvent.change(within(panel).getByLabelText('Your question about this incident'), { target: { value: 'What changed?' } });
    fireEvent.click(within(panel).getByRole('button', { name: 'Ask' }));
    await waitFor(() => expect(calls.find((c) => c.path === '/v1/sre/incidents/inc_1/ask')?.body).toEqual({ question: 'What changed?' }));
});

it('when it cannot work it says why; a resolved incident offers the postmortem to edit and approve', async () => {
    const calls: MockCall[] = [];
    const resolved = { ...INCIDENT, status: 'resolved', resolved_at: '2026-11-10T11:00:00Z' };
    const pm = { markdown: '# Postmortem: Shop API is slow\n\n## Cause\n\nNo index.', status: 'draft', edited_by: null, approved_by: null };
    page({ ...ROUTES, 'GET /v1/observability/incidents/inc_1': resolved,
        'GET /v1/sre/incidents/inc_1': { ...SRE, actions: [], postmortem: pm,
            status: { ready: false, why: 'no model is configured for the SRE agent', model: null, enabled: true } },
        'PUT /v1/sre/incidents/inc_1/postmortem': pm, 'POST /v1/sre/incidents/inc_1/postmortem/approve': { ...pm, status: 'approved' } }, calls);
    const panel = await screen.findByRole('region', { name: 'SRE agent' });
    expect(within(panel).getByText('no model is configured for the SRE agent')).toBeTruthy();
    expect(within(panel).getByText('Nothing proposed yet.')).toBeTruthy();
    const editor = within(panel).getByRole('textbox', { name: 'Postmortem' });
    fireEvent.change(editor, { target: { value: pm.markdown + '\nReviewed.' } });
    fireEvent.click(within(panel).getByRole('button', { name: 'Approve the postmortem' }));
    await waitFor(() => expect(calls.some((c) => c.path === '/v1/sre/incidents/inc_1/postmortem/approve')).toBe(true));
    expect(calls.find((c) => c.method === 'PUT')!.body).toEqual({ markdown: pm.markdown + '\nReviewed.' });
});

it('Diagnose on an instance shows findings and remediations, and proposes one as a change request', async () => {
    const calls: MockCall[] = [];
    const report = { instance: 'shop-pg', verdict: 'needs attention', connections: { total: 4, max: 40 }, problems: [],
        findings: [{ kind: 'index', text: 'shop: Seq Scan on public.orders' }], errors: [], slow_queries: [],
        suggestions: [{ kind: 'change_request', title: 'Index public.orders on customer_id in shop', rationale: 'scans all rows',
            bundle_yaml: 'postgres_indexes:\n  - database: shop\n' }] };
    const { client } = makeClient({ 'GET /ui/config': UI_CONFIG, 'POST /v1/sre/instances/shop-pg/diagnose':
        { run: 'run_1', report, answer: null, answer_unavailable: null }, 'POST /v1/iac/changes': { id: 'c1', status: 'proposed' } }, calls);
    render(<CairnProvider client={client} cache={new QueryCache()}><DiagnoseButton instance="shop-pg" /></CairnProvider>);
    fireEvent.click(screen.getByRole('button', { name: 'Diagnose' }));
    const d = await screen.findByRole('region', { name: 'Diagnosis of shop-pg' });
    expect(within(d).getByText('shop: Seq Scan on public.orders')).toBeTruthy();
    fireEvent.click(within(d).getByRole('button', { name: 'Propose as a change request' }));
    await waitFor(() => expect(calls.find((c) => c.path === '/v1/iac/changes')?.body).toEqual({
        title: 'Index public.orders on customer_id in shop', intent: 'scans all rows', bundle_yaml: 'postgres_indexes:\n  - database: shop\n' }));
});

it('the command palette offers asking the SRE agent where Observability is offered', () => {
    const t = (k: Parameters<typeof translate>[1], v?: Record<string, string | number>) => translate('en', k, v);
    const items = paletteItems([{ to: '/observability', label: 'Observability' }], t, () => [], null, () => undefined);
    expect(items.find((i) => i.id === 'act:ask-sre')?.to).toBe('/incidents');
    expect(paletteItems([{ to: '/discover', label: 'Discover' }], t, () => [], null, () => undefined).map((i) => i.id))
        .not.toContain('act:ask-sre');
});
