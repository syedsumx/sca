import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import React from 'react';
import { MemoryRouter, Route } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { CairnProvider } from '../context/CairnProvider';
import { holds, routeAllowed } from '../guide/capabilities';
import { paletteItems } from '../guide/CommandPalette';
import { translate } from '../i18n';
import { PeoplePage, reasonText } from '../pages/PeoplePage';
import { HUBS } from '../resources/hubs';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const DANA = {
    id: 'dana.lee', urn: 'urn:li:corpuser:dana.lee', type: 'person', display_name: 'Dana Lee', email: 'dana@example.com',
    title: 'Analyst', department: 'Finance', country: 'DE', employment_type: 'contractor', manager: null,
    attributes: { clearance: 'high' }, status: 'active', source: 'manual', groups: ['urn:li:corpGroup:finance-analysts'],
};
const ERIN = { ...DANA, id: 'erin', urn: 'urn:li:corpuser:erin', display_name: 'Erin Okta', source: 'scim', groups: [],
    attributes: {}, employment_type: null };
const DETAIL = {
    ...DANA,
    groups: [{ id: 'finance-analysts', urn: 'urn:li:corpGroup:finance-analysts', name: 'Finance analysts', source: 'manual',
        description: '', members: 1 }],
    personas: [
        { id: 'finance', name: 'Finance analysts', permissions: ['view', 'data_access'],
            reasons: [{ kind: 'group', group: 'urn:li:corpGroup:finance-analysts' }] },
        { id: 'contractors', name: 'Contractors', permissions: ['view'],
            reasons: [{ kind: 'attribute_rule', rules: [{ attr: 'employment_type', op: 'eq', value: 'contractor' }] }] },
    ],
    persona_grants: [],
    workspaces: [{ id: 'finance', name: 'Finance', role: 'member', domains: ['urn:li:domain:finance'] }],
    owns: [{ kind: 'agent', id: 'agt_1', name: 'nightly-sync', scopes: ['read'] }],
    last_sign_in: { ts: '2026-11-10T09:00:00+00:00', action: 'device.signed_in' },
    open_access_reviews: [],
    recent_audit: [{ seq: 1, ts: '2026-11-10T09:00:00+00:00', category: 'people', action: 'person.created',
        actor: 'urn:li:corpuser:admin', subject: 'urn:li:corpuser:dana.lee' }],
    managed: { row: true, editable: true, why: null },
};
const REACH = {
    personas: [{ ...DETAIL.personas[0], privileges: ['CAIRN_DATA_ACCESS'], scope_text: 'domains: finance' }],
    purposes: [{ id: 'payroll', name: 'Payroll', labels: ['PII.*'], allowed: false, effect: 'denied view' }],
    workspaces: [],
};
const ANSWER = {
    allowed: true, decision: 'ALLOW', reason: 'allowed by policy persona:finance',
    summary: 'Yes: Dana Lee may CAIRN_DATA_ACCESS on urn:li:dataset:x, allowed by persona Finance analysts.',
    because: [{ kind: 'persona', name: 'Finance analysts', policy_id: 'persona:finance', description: "Persona 'Finance analysts'" }],
    considered: [{ kind: 'persona', name: 'Sales', policy_id: 'persona:sales', why_not: 'condition did not match' }],
    obligations: [], subject: { groups: ['urn:li:corpGroup:finance-analysts'], personas: ['finance'], workspaces: [], attributes: {} },
};

function page(path: string, routes: Record<string, unknown>, calls: MockCall[] = []) {
    const { client } = makeClient({ 'GET /ui/config': UI_CONFIG, ...routes }, calls);
    render(
        <MemoryRouter initialEntries={[path]}>
            <CairnProvider client={client} cache={new QueryCache()}>
                <Route path="/people" component={PeoplePage} />
            </CairnProvider>
        </MemoryRouter>,
    );
}

const ROUTES = {
    'GET /v1/people/users': { total: 2, offset: 0, limit: 25, items: [DANA, ERIN] },
    'GET /v1/people/users/dana.lee': DETAIL,
    'GET /v1/people/users/dana.lee/access': REACH,
    'GET /v1/people/users/erin': { ...DETAIL, ...ERIN, groups: [], personas: [], managed: { row: false, editable: false,
        why: 'managed by your identity provider (SCIM): change it there' } },
    'GET /v1/people/users/erin/access': { personas: [], purposes: [], workspaces: [] },
};

it('lists people with where each comes from, and a person’s page says what they hold and why', async () => {
    page('/people', ROUTES);
    expect(await screen.findByRole('heading', { level: 1, name: 'People & roles' })).toBeTruthy();
    expect(await screen.findByText('Made in Cairn')).toBeTruthy();
    expect(screen.getByText('SCIM')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Open Dana Lee' }));
    const person = await screen.findByRole('region', { name: 'Person Dana Lee' });
    expect(within(person).getByText('Why: through the group finance-analysts')).toBeTruthy();
    expect(within(person).getByText('Why: by attribute: employment_type eq "contractor"')).toBeTruthy();
    expect(await within(person).findByText(/domains: finance/)).toBeTruthy();
    expect(within(person).getByText(/Payroll \(PII\.\*\): denied view/)).toBeTruthy();
    expect(within(person).getByText('high')).toBeTruthy();                     // an attribute ABAC reads
    expect(within(person).getByRole('button', { name: 'Edit person' })).toBeTruthy();
    expect(within(person).getByRole('button', { name: 'Deactivate' })).toBeTruthy();
});

it('a SCIM person is the identity provider’s: no edit, no deactivate, and it says so', async () => {
    page('/people?person=erin', ROUTES);
    const person = await screen.findByRole('region', { name: 'Person Erin Okta' });
    expect(within(person).getByText('Managed by your identity provider (SCIM): change this person there.')).toBeTruthy();
    expect(within(person).queryByRole('button', { name: 'Edit person' })).toBeNull();
    expect(within(person).queryByRole('button', { name: 'Deactivate' })).toBeNull();
    expect(within(person).getByRole('button', { name: 'Check access' })).toBeTruthy();
});

it('the access check asks the API and shows yes or no and why', async () => {
    const calls: MockCall[] = [];
    page('/people?tab=access&check=dana.lee', { ...ROUTES, 'POST /v1/people/access-check': ANSWER }, calls);
    fireEvent.change(await screen.findByLabelText('On (dataset or other asset, optional)'), { target: { value: 'urn:li:dataset:x' } });
    fireEvent.click(screen.getByRole('button', { name: 'Check' }));
    const answer = await screen.findByRole('status', { name: 'Access check answer' });
    expect(within(answer).getByText('Yes')).toBeTruthy();
    expect(within(answer).getByText('persona Finance analysts')).toBeTruthy();
    expect(within(answer).getByText('persona:finance')).toBeTruthy();
    expect(within(answer).getByText('condition did not match')).toBeTruthy();
    await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true));
    expect(calls.find((c) => c.method === 'POST')!.body).toEqual({ person: 'dana.lee', privilege: 'VIEW_ENTITY_PAGE',
        resource: 'urn:li:dataset:x' });
});

it('without MANAGE_POLICIES the page says so and shows what is theirs; the palette leaves the administrator actions out', async () => {
    page('/people', { 'GET /v1/people/users': () => new Response(JSON.stringify({ detail: 'MANAGE_POLICIES denied' }), { status: 403 }) });
    expect(await screen.findByText('People & roles is for administrators (MANAGE_POLICIES).')).toBeTruthy();
    // What is theirs is still here (review of fa60a2c): the access reviews assigned to them, and their business units.
    expect(screen.getByRole('tab', { name: 'Access reviews' }).getAttribute('aria-selected')).toBe('true');
    expect(screen.getByRole('tab', { name: 'Business units' })).toBeTruthy();
    expect(screen.queryByRole('tab', { name: 'Roles' })).toBeNull();
    const none = { caps: { actor: 'a', is_admin: false, privileges: {} }, loading: false, error: false };
    const admin = { caps: { actor: 'a', is_admin: true, privileges: { MANAGE_POLICIES: true } }, loading: false, error: false };
    // The page is offered to everyone (review of 91dd05a): reviewers and members reach what is theirs from the sidebar.
    expect(routeAllowed('/people', HUBS, none)).toBe(true);
    expect(routeAllowed('/people', HUBS, admin)).toBe(true);
    expect(routeAllowed('/governance', HUBS, none)).toBe(false);
    expect(holds(['MANAGE_POLICIES'], { caps: undefined, loading: true, error: false })).toBe(false);   // fail closed
    const t = (k: Parameters<typeof translate>[1], v?: Record<string, string | number>) => translate('en', k, v);
    const ids = (nav: { to: string; label: string }[]) => paletteItems(nav, t, () => [], null, () => undefined).map((i) => i.id);
    expect(ids([{ to: '/discover', label: 'Discover' }])).not.toContain('act:check-access');
    expect(ids([{ to: '/people', label: 'People & roles' }])).not.toContain('act:check-access');
    const withPeople = paletteItems([{ to: '/people', label: 'People & roles' }, { to: '/governance', label: 'Access policies' }], t,
        () => [], null, () => undefined);
    expect(withPeople.find((i) => i.id === 'act:check-access')?.to).toBe('/people?tab=access');
    expect(withPeople.find((i) => i.id === 'act:new-person')?.label).toBe('New person');
});

it('says why in words', () => {
    expect(reasonText({ kind: 'direct' })).toBe('named in the persona');
    expect(reasonText({ kind: 'grant', source: 'access_request', expires_at_ms: null })).toBe('granted (access_request)');
    expect(reasonText({ kind: 'grant', source: 'manual' })).toBe('granted by hand');
});
