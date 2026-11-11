import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import React, { useState } from 'react';
import { MemoryRouter } from 'react-router-dom';

import { CairnApiError } from '../../api/client';
import { ErrorNotice } from '../components/ErrorNotice';
import { CairnProvider } from '../context/CairnProvider';
import { I18nProvider, translate } from '../i18n';
import { RESOURCE_SPECS } from '../resources/catalog';
import {
    FORMS, FORM_EXCLUSIONS, FormSchema, describeCron, durationParts, formKey, fromBody, initialValues, mapServerErrors,
    needsForm, toBody, validate, validCron,
} from '../resources/forms';
import { FormRenderer, FormState } from '../resources/FormRenderer';
import { ResourceView } from '../resources/ResourceView';
import type { ResourceAction } from '../resources/types';
import { presentError } from '../utils/errors';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const t = (key: Parameters<typeof translate>[1], vars?: Record<string, string | number>) => translate('en', key, vars);

function wrap(ui: React.ReactElement, routes: Record<string, unknown> = {}, calls: MockCall[] = []) {
    const { client, cache } = makeClient({ 'GET /ui/config': UI_CONFIG, 'GET /notify/inbox': [], ...routes }, calls);
    return render(
        <MemoryRouter>
            <I18nProvider initial="en">
                <CairnProvider client={client} cache={cache}>{ui}</CairnProvider>
            </I18nProvider>
        </MemoryRouter>,
    );
}

const lastDialog = async () => (await screen.findAllByRole('dialog')).slice(-1)[0];

// ---- coverage ---------------------------------------------------------------------------------------------------
it('every create, change and run in the catalog has a form, or is on the reasoned exclusion list', () => {
    const missing: string[] = [];
    const used = new Set<string>();
    for (const spec of Object.values(RESOURCE_SPECS)) {
        for (const a of spec.actions ?? []) {
            if (!needsForm(a)) continue;
            used.add(formKey(a));
            if (!FORMS[formKey(a)] && !FORM_EXCLUSIONS[formKey(a)]) missing.push(`${spec.key}: ${formKey(a)}`);
        }
    }
    expect(missing, 'run python -m cairn.iac.forms, or add a reasoned FORM_EXCLUSIONS entry').toEqual([]);
    for (const [k, why] of Object.entries(FORM_EXCLUSIONS)) {
        expect(used.has(k), `${k} is excluded but no action uses it`).toBe(true);
        expect(why.length, `${k} needs a reason`).toBeGreaterThan(20);
    }
    for (const k of Object.keys(FORMS)) expect(used.has(k), `${k} is a form no action uses (regenerate)`).toBe(true);
    expect(Object.keys(FORMS).length).toBeGreaterThan(100);
});

it('every form field has a label, a known type, and pickers name what they search', () => {
    const types = new Set(['text', 'textarea', 'number', 'integer', 'boolean', 'select', 'multiselect', 'list', 'keyvalue',
        'group', 'objects', 'duration', 'cron', 'datetime', 'date', 'month', 'sql', 'code', 'json', 'password', 'picker', 'file']);
    const walk = (form: string, fields: FormSchema['fields']) => {
        for (const f of fields) {
            expect(f.label, `${form} ${f.name}`).toBeTruthy();
            expect(types.has(f.type), `${form} ${f.name}: ${f.type}`).toBe(true);
            if (f.type === 'picker') expect(f.picker, `${form} ${f.name}`).toBeTruthy();
            if (f.type === 'select') expect(f.enum?.length, `${form} ${f.name}`).toBeGreaterThan(0);
            if (f.write_only || f.sensitive) expect(['password', 'list'], `${form} ${f.name}`).toContain(f.type);
            walk(form, f.fields ?? []);
        }
    };
    for (const [k, form] of Object.entries(FORMS)) walk(k, form.fields);
    // what the brief asks of the widgets, present in the real forms
    const flat = (fields: FormSchema['fields']): FormSchema['fields'] => fields.flatMap((f) => [f, ...flat(f.fields ?? [])]);
    const all = Object.values(FORMS).flatMap((f) => flat(f.fields));
    const pickers = new Set(all.filter((f) => f.type === 'picker').map((f) => f.picker));
    for (const p of ['table', 'dataset', 'connection', 'warehouse', 'principal', 'group', 'secret_ref', 'agent', 'ref']) {
        expect(pickers.has(p as never), p).toBe(true);
    }
    expect(FORMS['PUT /v1/secret-scopes/{name}/secrets/{key}'].fields.find((f) => f.name === 'value')?.type).toBe('password');
    expect(FORMS['POST /v1/jobs'].fields.find((f) => f.name === 'cron')?.type).toBe('cron');
    expect(FORMS['POST /sql/saved'].fields.find((f) => f.name === 'sql')?.type).toBe('sql');
});

it('every area links to a section of the in-product docs that exists', () => {
    const dir = join(__dirname, '../../../../docs/site');
    const slug = (s: string) => s.toLowerCase().replace(/<[^>]+>/g, '').replace(/`/g, '').replace(/[^\p{L}\p{N}]+/gu, '-').replace(/^-|-$/g, '');
    const anchors = new Set<string>();
    for (const f of readdirSync(dir).filter((x) => x.endsWith('.md'))) {
        for (const line of readFileSync(join(dir, f), 'utf8').split('\n')) {
            const m = /^#{1,4} (.+)$/.exec(line);
            if (m) anchors.add(`${f.replace(/\.md$/, '.html')}#${slug(m[1])}`);
        }
    }
    for (const spec of Object.values(RESOURCE_SPECS)) {
        expect(spec.docs, spec.key).toBeTruthy();
        expect(anchors.has(spec.docs as string), `${spec.key}: ${spec.docs}`).toBe(true);
    }
});

// ---- the renderer, one field type at a time ----------------------------------------------------------------------
const EVERY: FormSchema = {
    source: 'test',
    mode: 'create',
    fields: [
        { name: 'name', label: 'Name', type: 'text', required: true, pattern: '^[a-z-]+$', description: 'lowercase and -' },
        { name: 'notes', label: 'Notes', type: 'textarea' },
        { name: 'amount', label: 'Amount', type: 'number', unit: 'USD', min: 0 },
        { name: 'every_minutes', label: 'Every minutes', type: 'duration', unit: 'minutes', number: 'integer' },
        { name: 'enabled', label: 'Enabled', type: 'boolean', default: true },
        { name: 'mode', label: 'Mode', type: 'select', enum: ['full', 'append'] },
        { name: 'days', label: 'Days', type: 'multiselect', enum: ['mon', 'tue'] },
        { name: 'tags', label: 'Tags', type: 'list' },
        { name: 'params', label: 'Params', type: 'keyvalue' },
        { name: 'checks', label: 'Checks', type: 'objects', fields: [{ name: 'name', label: 'Name', type: 'text' }] },
        { name: 'scope', label: 'Scope', type: 'group', fields: [{ name: 'all', label: 'All', type: 'boolean' }] },
        { name: 'cron', label: 'Cron', type: 'cron' },
        { name: 'day', label: 'Day', type: 'date' },
        { name: 'at', label: 'At', type: 'datetime' },
        { name: 'sql', label: 'SQL', type: 'sql' },
        { name: 'code', label: 'Code', type: 'code' },
        { name: 'config', label: 'Config', type: 'json' },
        { name: 'value', label: 'Value', type: 'password', write_only: true },
        { name: 'connection', label: 'Connection', type: 'picker', picker: 'connection' },
        { name: 'bundle', label: 'Bundle', type: 'file' },
        { name: 'rare', label: 'Rare', type: 'text', advanced: true },
    ],
};
const ACTION: ResourceAction = { label: 'New thing', method: 'POST', path: '/v1/things', body: { name: 'orders-files' } };

function Harness({ form = EVERY, action = ACTION, start, errors = {}, onState }: {
    form?: FormSchema; action?: ResourceAction; start?: FormState; errors?: Record<string, string>; onState?: (s: FormState) => void;
}) {
    const [state, setState] = useState<FormState>(start ?? { values: initialValues(form, action, null), extra: {}, raw: null });
    return (
        <FormRenderer form={form} action={action} state={state} errors={errors} editing={false} rowAction={false}
            setState={(s) => {
                setState(s);
                onState?.(s);
            }} />
    );
}

it('draws every field type with a label, help, units, previews and the example as placeholder', async () => {
    let last: FormState | null = null;
    wrap(<Harness onState={(s) => (last = s)} />, { 'GET /data/sources': [{ connection: 'wh', platform: 'sqlite' }] });
    const name = screen.getByLabelText(/^Name/) as HTMLInputElement;
    expect(name.placeholder).toBe('orders-files');
    expect(name.getAttribute('aria-describedby')).toBeTruthy();
    expect(screen.getByText('lowercase and -')).toBeTruthy();
    expect(screen.getByLabelText('Notes').tagName).toBe('TEXTAREA');
    expect(screen.getByText('USD')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('Every minutes'), { target: { value: '1440' } });
    fireEvent.blur(screen.getByLabelText('Every minutes'));
    expect(await screen.findByText('= 1 day')).toBeTruthy();
    expect(screen.getByLabelText('Enabled').getAttribute('role')).toBe('switch');
    expect(screen.getByLabelText('Enabled').getAttribute('aria-checked')).toBe('true');
    expect(screen.getByLabelText('Mode')).toBeTruthy();
    expect(screen.getByLabelText('Days')).toBeTruthy();
    expect(screen.getByLabelText('Tags')).toBeTruthy();
    expect(screen.getByLabelText('Params: key 1')).toBeTruthy();
    expect(screen.getByRole('group', { name: 'Checks' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Add to Checks' }));
    expect(screen.getByRole('group', { name: 'Item 1' })).toBeTruthy();
    expect(screen.getByRole('group', { name: 'Scope' })).toBeTruthy();
    fireEvent.change(screen.getByLabelText('Cron'), { target: { value: '0 2 * * *' } });
    expect(screen.getByText('Every day at 02:00 UTC')).toBeTruthy();
    expect((screen.getByLabelText('Day') as HTMLInputElement).type).toBe('date');
    expect((screen.getByLabelText('At') as HTMLInputElement).type).toBe('datetime-local');
    expect(screen.getByLabelText('SQL').tagName).toBe('TEXTAREA');
    expect(screen.getByLabelText('Code').className).toContain('cairn-mono');
    expect(screen.getByLabelText('Config')).toBeTruthy();
    const secret = screen.getByLabelText('Value') as HTMLInputElement;
    expect(secret.type).toBe('password');
    expect(secret.value).toBe('');
    expect(secret.autocomplete).toBe('new-password');
    expect(screen.getByText('Never shown again after it is saved.')).toBeTruthy();
    expect((screen.getByLabelText('Bundle') as HTMLInputElement).type).toBe('file');
    // Advanced: closed until asked for
    expect(screen.queryByLabelText('Rare')).toBeNull();
    const more = screen.getByRole('button', { name: /Advanced options \(1\)/ });
    expect(more.getAttribute('aria-expanded')).toBe('false');
    fireEvent.click(more);
    expect(screen.getByLabelText('Rare')).toBeTruthy();
    // a picker lists the real API's choices and takes one
    const picker = screen.getByLabelText('Connection');
    fireEvent.focus(picker);
    fireEvent.mouseDown(picker);
    fireEvent.click(await screen.findByText('wh (sqlite)'));
    await waitFor(() => expect(last?.values.connection).toBe('wh'));
});

it('a principal picker turns a typed name into a person or a group', async () => {
    const form: FormSchema = { source: 't', mode: 'create', fields: [{ name: 'principal', label: 'Principal', type: 'picker', picker: 'principal' }] };
    let last: FormState | null = null;
    wrap(<Harness form={form} onState={(s) => (last = s)} />, { 'GET /me': { urn: 'urn:li:corpuser:ana' } });
    const input = screen.getByLabelText('Principal');
    fireEvent.mouseDown(input);
    fireEvent.change(input, { target: { value: 'data-eng' } });
    fireEvent.click(await screen.findByText('data-eng (group)'));
    await waitFor(() => expect(last?.values.principal).toBe('urn:li:corpGroup:data-eng'));
});

it('a secret reference is chosen from the scopes and keys the person may read, never a value', async () => {
    const form: FormSchema = { source: 't', mode: 'create', fields: [
        { name: 'credential_ref', label: 'Credential ref', type: 'picker', picker: 'secret_ref' },
        { name: 'warehouse', label: 'Warehouse', type: 'picker', picker: 'warehouse' }] };
    let last: FormState | null = null;
    const calls: MockCall[] = [];
    wrap(<Harness form={form} onState={(s) => (last = s)} />, {
        'GET /v1/secret-scopes': [{ name: 'partner' }],
        'GET /v1/secret-scopes/partner': { name: 'partner', secrets: [{ key: 'client-secret', updated_ms: 1 }], acl: [] },
        'GET /v1/warehouses': [{ name: 'sales', state: 'stopped' }],
    }, calls);
    const ref = screen.getByLabelText('Credential ref');
    fireEvent.focus(ref);
    fireEvent.mouseDown(ref);
    fireEvent.click(await screen.findByText('partner / client-secret'));
    await waitFor(() => expect(last?.values.credential_ref).toBe('scope:partner/client-secret'));
    expect(calls.some((c) => /secrets\/client-secret$/.test(c.path))).toBe(false);   // the value is never read
    const wh = screen.getByLabelText('Warehouse');
    fireEvent.focus(wh);
    fireEvent.mouseDown(wh);
    fireEvent.click((await screen.findAllByText('sales')).find((e) => e.closest('.ant-select-item-option')) as HTMLElement);
    await waitFor(() => expect(last?.values.warehouse).toBe('sales'));
});

// ---- values, validation and Raw JSON ------------------------------------------------------------------------------
it('validates required fields, patterns, ranges, cron and JSON', () => {
    const v = validate(EVERY, { name: 'Bad Name', amount: -1, cron: '0 2 * *', config: '{', checks: [] });
    expect(v.name).toEqual({ code: 'pattern' });
    expect(v.amount).toEqual({ code: 'min', n: 0 });
    expect(v.cron).toEqual({ code: 'cron' });
    expect(v.config).toEqual({ code: 'json' });
    expect(validate(EVERY, {}).name).toEqual({ code: 'required' });
    expect(validate(EVERY, { name: 'ok-name' })).toEqual({});
    expect(t('form.err.min', { label: 'Amount', n: 0 })).toBe('Amount must be at least 0.');
});

it('shows a problem on its field, announced and tied to the input', async () => {
    wrap(<Harness errors={{ name: 'name: lowercase letters, digits and -' }} />);
    const input = screen.getByLabelText(/^Name/);
    expect(input.getAttribute('aria-invalid')).toBe('true');
    const alert = screen.getByRole('alert');
    expect(alert.textContent).toBe('name: lowercase letters, digits and -');
    expect(input.getAttribute('aria-describedby')).toContain(alert.id);
});

it('builds the body from typed values and reads it back (Raw JSON round trip), keeping keys only JSON knew', () => {
    const values = {
        name: 'orders', amount: 12.5, every_minutes: 60, enabled: false, mode: 'full', tags: ['a', 'b'],
        params: [{ key: 'day', value: '2026-11-09' }], checks: [{ name: 'no_nulls' }], scope: { all: true }, cron: '0 7 * * 1',
        config: '{"x": 1}', value: 's3cret', connection: 'wh',
    };
    const body = toBody(EVERY, values, { kept: 1 });
    expect(body).toEqual({
        kept: 1, name: 'orders', amount: 12.5, every_minutes: 60, enabled: false, mode: 'full', tags: ['a', 'b'],
        params: { day: '2026-11-09' }, checks: [{ name: 'no_nulls' }], scope: { all: true }, cron: '0 7 * * 1', config: { x: 1 },
        value: 's3cret', connection: 'wh',
    });
    const back = fromBody(EVERY, JSON.parse(JSON.stringify(body)));
    expect(back.extra).toEqual({ kept: 1 });
    expect(toBody(EVERY, back.values, back.extra)).toEqual(body);
});

it('Raw JSON in the form: edits there come back into the fields', async () => {
    let last: FormState | null = null;
    wrap(<Harness onState={(s) => (last = s)} />);
    fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: 'first' } });
    fireEvent.click(screen.getByLabelText('Raw JSON'));
    const raw = screen.getByLabelText('Request body (JSON)') as HTMLTextAreaElement;
    expect(JSON.parse(raw.value)).toMatchObject({ name: 'first', enabled: true });
    fireEvent.change(raw, { target: { value: '{"name": "second", "mode": "append", "unknown_key": 3}' } });
    fireEvent.click(screen.getByLabelText('Raw JSON'));
    expect((screen.getByLabelText(/^Name/) as HTMLInputElement).value).toBe('second');
    expect(last!.values.mode).toBe('append');
    expect(last!.extra).toEqual({ unknown_key: 3 });
    fireEvent.click(screen.getByLabelText('Raw JSON'));
    fireEvent.change(screen.getByLabelText('Request body (JSON)'), { target: { value: '{"name": ' } });
    fireEvent.click(screen.getByLabelText('Raw JSON'));
    expect(screen.getByRole('alert').textContent).toMatch(/The JSON is not valid/);
});

it('places the API\'s refusals on fields: FastAPI 422 loc, and sentences that name a field', () => {
    const form = FORMS['POST /quality/monitors'];
    const r = mapServerErrors(form, 422, { detail: [
        { loc: ['body', 'dataset_urn'], msg: "String should match pattern '^urn:li:dataset:\\(.+\\)$'", type: 'string_pattern_mismatch' },
        { loc: ['body', 'schedule', 'interval_seconds'], msg: 'Input should be greater than or equal to 60', type: 'greater_than_equal' },
        { loc: ['body'], msg: 'Something about the whole body', type: 'value_error' },
    ] });
    expect(r.fields.dataset_urn).toMatch(/pattern/);
    expect(r.fields['schedule.interval_seconds']).toMatch(/60/);
    expect(r.rest).toEqual(['Something about the whole body']);
    const acl = FORMS['PUT /v1/secret-scopes/{name}/acl'];
    expect(mapServerErrors(acl, 400, { detail: 'principal: urn:li:corpuser:… or urn:li:corpGroup:…' }).fields.principal).toBeTruthy();
    expect(mapServerErrors(acl, 403, { detail: 'needs MANAGE on the scope' }).fields).toEqual({});
});

it('describes cron schedules in words and durations in larger units', () => {
    expect(describeCron('0 2 * * *')).toEqual({ key: 'cron.daily', time: '02:00' });
    expect(describeCron('*/15 * * * *')).toEqual({ key: 'cron.everyNMinutes', n: 15 });
    expect(describeCron('30 * * * *')).toEqual({ key: 'cron.hourly', minute: '30' });
    expect(describeCron('0 7 * * 1')).toEqual({ key: 'cron.weekly', days: [1], time: '07:00' });
    expect(describeCron('0 9 * * 1-5')).toEqual({ key: 'cron.weekdays', time: '09:00' });
    expect(describeCron('0 3 1 * *')).toEqual({ key: 'cron.monthly', day: 1, time: '03:00' });
    expect(describeCron('0 3 1 1 *')).toEqual({ key: 'cron.custom' });
    expect(validCron('61 * * * *')).toBe(false);
    expect(validCron('0 0 * * MON')).toBe(true);
    expect(durationParts(90, 'minutes')).toEqual({ n: 1.5, unit: 'hours' });
    expect(durationParts(45, 'minutes')).toBeNull();
});

// ---- the error presenter ------------------------------------------------------------------------------------------
it('turns API errors into a sentence and what to do, with the detail kept', () => {
    const err = (status: number, detail: unknown, path = '/v1/budgets') => new CairnApiError(String(detail), status, path, { detail });
    const forbidden = presentError(err(403, 'needs MANAGE_SYSTEM_OPERATIONS'), t, { action: 'New budget', area: 'Budgets' });
    expect(forbidden.title).toBe('You don’t have permission for “New budget”');
    expect(forbidden.hint).toBe('Ask an owner of Budgets, or an admin, to give you access. It needs MANAGE_SYSTEM_OPERATIONS.');
    expect(forbidden.detail).toBe('HTTP 403 · /v1/budgets\nneeds MANAGE_SYSTEM_OPERATIONS');
    expect(presentError(err(403, 'no'), t, { area: 'Budgets' }).title).toBe('You don’t have permission to see Budgets');
    expect(presentError(err(409, 'a budget named x exists'), t).kind).toBe('conflict');
    expect(presentError(err(422, [{ loc: ['body', 'name'], msg: 'Field required' }]), t).detail).toContain('name: Field required');
    expect(presentError(err(429, 'slow down'), t).title).toBe('Too many requests right now');
    const off = presentError(err(503, 'secret scopes are not configured (set CAIRN_SECRETS_KEY)'), t,
        { area: 'Secret scopes', docs: 'administration.html#secret-scopes' });
    expect(off.kind).toBe('unconfigured');
    expect(off.title).toBe('This feature isn’t set up on this Cairn');
    expect(off.docs).toBe('/docs/administration.html#secret-scopes');
    expect(presentError(err(503, 'warehouse is starting'), t, { area: 'Warehouses' }).title).toBe('Warehouses is unavailable right now');
    expect(presentError(err(500, 'boom'), t).kind).toBe('server');
    expect(presentError(new CairnApiError('Network error calling /x: offline', 0, '/x'), t).kind).toBe('network');
    expect(presentError(err(401, 'expired'), t).title).toBe('Your session has ended');
});

it('the error notice keeps the technical detail under Details, with a copy button', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    wrap(<ErrorNotice error={new CairnApiError('x', 503, '/v1/secret-scopes', { detail: 'secret scopes are not configured' })}
        area="Secret scopes" docs="administration.html#secret-scopes" />);
    expect(screen.getByText('This feature isn’t set up on this Cairn')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'How to enable it' }).getAttribute('href')).toBe('/docs/administration.html#secret-scopes');
    expect(screen.queryByText(/HTTP 503/)).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Details' }));
    expect(screen.getByText(/HTTP 503 · \/v1\/secret-scopes/)).toBeTruthy();
    await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Copy details' }));
    });
    expect(writeText).toHaveBeenCalledWith('HTTP 503 · /v1/secret-scopes\nsecret scopes are not configured');
});

// ---- the forms in an area -----------------------------------------------------------------------------------------
it('an empty area says what it is for, offers its create form and links to its docs', async () => {
    const spec = RESOURCE_SPECS['budgets'];
    wrap(<ResourceView spec={spec} />, { 'GET /v1/budgets': [] });
    const region = await screen.findByRole('region', { name: 'No Budgets yet' });
    expect(within(region).getByText(spec.description)).toBeTruthy();
    expect(within(region).getByRole('link', { name: 'Learn more' }).getAttribute('href')).toBe(`/docs/${spec.docs}`);
    fireEvent.click(within(region).getByRole('button', { name: 'Create' }));
    const dialog = await lastDialog();
    expect(within(dialog).getByLabelText(/^Name/)).toBeTruthy();
    expect(within(dialog).getByLabelText(/^Amount/)).toBeTruthy();
});

it('creates from the form, maps a 422 onto its field, and sends the typed body', async () => {
    const spec = RESOURCE_SPECS['budgets'];
    const calls: MockCall[] = [];
    let attempt = 0;
    wrap(<ResourceView spec={spec} />, {
        'GET /v1/budgets': [],
        'POST /v1/budgets': () => {
            attempt += 1;
            return attempt === 1
                ? new Response(JSON.stringify({ detail: [{ loc: ['body', 'amount_usd'], msg: 'Input should be greater than 0', type: 'gt' }] }), { status: 422 })
                : { id: 'b1', name: 'AI spend' };
        },
    }, calls);
    fireEvent.click(await screen.findByRole('button', { name: 'Create' }));
    const dialog = await lastDialog();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create' }));
    expect(await within(dialog).findByText('Name is required.')).toBeTruthy();
    expect(calls.some((c) => c.method === 'POST')).toBe(false);
    fireEvent.change(within(dialog).getByLabelText(/^Name/), { target: { value: 'AI spend' } });
    fireEvent.change(within(dialog).getByLabelText(/^Amount/), { target: { value: '-5' } });
    fireEvent.blur(within(dialog).getByLabelText(/^Amount/));
    fireEvent.click(within(dialog).getByLabelText(/^AI/));                   // the scope: one of, here AI spend
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create' }));
    expect(await within(dialog).findByText('Input should be greater than 0')).toBeTruthy();
    expect(within(dialog).getByText('Some values need fixing')).toBeTruthy();
    const post = calls.find((c) => c.method === 'POST')!;
    expect(post.body).toMatchObject({ name: 'AI spend', amount_usd: -5, scope: { ai: true }, period: 'month', thresholds: [80, 100] });
    fireEvent.change(within(dialog).getByLabelText(/^Amount/), { target: { value: '2000' } });
    fireEvent.blur(within(dialog).getByLabelText(/^Amount/));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create' }));
    await waitFor(() => expect(calls.filter((c) => c.method === 'POST')).toHaveLength(2));
});

it('a one-of group (a budget’s scope) takes exactly one, and a switch left off is not sent', async () => {
    const spec = RESOURCE_SPECS['budgets'];
    const calls: MockCall[] = [];
    wrap(<ResourceView spec={spec} />, { 'GET /v1/budgets': [], 'POST /v1/budgets': { id: 'b2', name: 'Lake' } }, calls);
    fireEvent.click(await screen.findByRole('button', { name: 'Create' }));
    const dialog = await lastDialog();
    fireEvent.change(within(dialog).getByLabelText(/^Name/), { target: { value: 'Lake' } });
    fireEvent.change(within(dialog).getByLabelText(/^Amount/), { target: { value: '100' } });
    fireEvent.blur(within(dialog).getByLabelText(/^Amount/));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create' }));
    expect(await within(dialog).findByText('Scope is required.')).toBeTruthy();
    fireEvent.click(within(dialog).getByLabelText(/^All/));
    fireEvent.click(within(dialog).getByLabelText(/^AI/));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create' }));
    expect(await within(dialog).findByText('Choose only one for Scope.')).toBeTruthy();
    expect(calls.some((c) => c.method === 'POST')).toBe(false);
    fireEvent.click(within(dialog).getByLabelText(/^AI/));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create' }));
    await waitFor(() => expect(calls.find((c) => c.method === 'POST')?.body).toMatchObject({ scope: { all: true } }));
});

it('an edit starts from the row and a PATCH sends only what changed', async () => {
    const spec = RESOURCE_SPECS['budgets'];
    const calls: MockCall[] = [];
    const row = { id: 'b1', name: 'AI spend', amount_usd: 2000, period: 'month', scope: { ai: true }, thresholds: [80, 100],
        forecast: true, notify: ['urn:li:corpGroup:finops'], enabled: true };
    wrap(<ResourceView spec={spec} />, { 'GET /v1/budgets': [row], 'PATCH /v1/budgets/b1': { ...row, amount_usd: 2500 } }, calls);
    fireEvent.click(await screen.findByRole('button', { name: 'Edit: AI spend' }));
    const dialog = await lastDialog();
    expect((within(dialog).getByLabelText(/^Name/) as HTMLInputElement).value).toBe('AI spend');
    expect((within(dialog).getByLabelText(/^Amount/) as HTMLInputElement).value).toBe('2000');
    fireEvent.change(within(dialog).getByLabelText(/^Amount/), { target: { value: '2500' } });
    fireEvent.blur(within(dialog).getByLabelText(/^Amount/));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Edit' }));
    await waitFor(() => expect(calls.find((c) => c.method === 'PATCH')?.body).toEqual({ amount_usd: 2500 }));
});

it('a delete asks first, naming what goes', async () => {
    const spec = RESOURCE_SPECS['secret-scopes'];
    const calls: MockCall[] = [];
    wrap(<ResourceView spec={spec} />, { 'GET /v1/secret-scopes': [{ name: 'payments-gateway', description: 'Adyen' }],
        'DELETE /v1/secret-scopes/payments-gateway': undefined }, calls);
    // A delete is in the row's menu (L150), then asks first.
    fireEvent.click(await screen.findByRole('button', { name: 'More actions for payments-gateway' }));
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Delete scope' }));
    const dialog = await lastDialog();
    expect(within(dialog).getByText('Delete payments-gateway? This cannot be undone.')).toBeTruthy();
    expect(calls.some((c) => c.method === 'DELETE')).toBe(false);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete scope' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'DELETE' && c.path === '/v1/secret-scopes/payments-gateway')).toBe(true));
});

it('a secret is typed into a password field, its key fills the path, and the value never comes back', async () => {
    const spec = RESOURCE_SPECS['secret-scopes'];
    const calls: MockCall[] = [];
    wrap(<ResourceView spec={spec} />, { 'GET /v1/secret-scopes': [{ name: 'payments-gateway' }],
        'PUT /v1/secret-scopes/payments-gateway/secrets/api-key': { scope: 'payments-gateway', key: 'api-key' } }, calls);
    fireEvent.click(await screen.findByRole('button', { name: 'Put secret: payments-gateway' }));
    const dialog = await lastDialog();
    const value = within(dialog).getByLabelText(/^Value/) as HTMLInputElement;
    expect(value.type).toBe('password');
    fireEvent.change(within(dialog).getByLabelText(/^Key/), { target: { value: 'api-key' } });
    fireEvent.change(value, { target: { value: 'sk_live_1' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Put secret' }));
    await waitFor(() => expect(calls.find((c) => c.method === 'PUT')?.body).toEqual({ value: 'sk_live_1' }));
    expect(calls.find((c) => c.method === 'PUT')?.path).toBe('/v1/secret-scopes/payments-gateway/secrets/api-key');
});

it('a row\'s details are a formatted panel, with View JSON', async () => {
    const spec = RESOURCE_SPECS['notification-routes'];
    wrap(<ResourceView spec={spec} />, { 'GET /notify/routes': [{ id: 'finance', channel: 'webhook', target: 'https://hooks.example/T0K3N',
        event_types: ['quality.incident'], domains: [], created_by: 'urn:li:corpuser:raj' }] });
    fireEvent.click(await screen.findByText('finance'));
    const drawer = document.querySelector('.ant-drawer') as HTMLElement;
    expect(await within(drawer).findByText('raj')).toBeTruthy();
    expect(within(drawer).queryByText('https://hooks.example/T0K3N')).toBeNull(); // sensitive: masked until shown
    fireEvent.click(within(drawer).getByRole('button', { name: 'View JSON' }));
    expect(within(drawer).getByLabelText('The record as JSON').textContent).toContain('"channel": "webhook"');
    expect(within(drawer).getByLabelText('The record as JSON').textContent).not.toContain('T0K3N');
    expect(within(drawer).getByRole('button', { name: 'Copy as Terraform' })).toBeTruthy();
});

it('a refused action says so in words, keeping the API\'s answer under Details', async () => {
    const spec = RESOURCE_SPECS['secret-scopes'];
    wrap(<ResourceView spec={spec} />, { 'GET /v1/secret-scopes': [],
        'POST /v1/secret-scopes': new Response(JSON.stringify({ detail: 'secret scopes are not configured: set CAIRN_SECRETS_KEY' }), { status: 503 }) });
    fireEvent.click(await screen.findByRole('button', { name: 'Create scope' }));
    const dialog = await lastDialog();
    fireEvent.change(within(dialog).getByLabelText(/^Name/), { target: { value: 'payments' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create scope' }));
    expect(await within(dialog).findByText('This feature isn’t set up on this Cairn')).toBeTruthy();
    expect(within(dialog).getByRole('link', { name: 'How to enable it' }).getAttribute('href')).toBe('/docs/administration.html#secret-scopes');
});

it('a form\'s strings are translated (German)', () => {
    expect(translate('de', 'form.advanced', { n: 2 })).toBe('Erweiterte Optionen (2)');
    expect(translate('ja', 'form.rawJson')).toBe('JSON を直接編集');
    expect(translate('fr', 'form.passwordNew')).toBe(translate('en', 'form.passwordNew')); // English until translated
});
