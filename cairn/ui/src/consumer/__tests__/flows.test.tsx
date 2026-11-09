import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';

import { QueryCache } from '../api/cache';
import { CairnProvider } from '../context/CairnProvider';
import { FlowDetail, flowsApi } from '../flows/api';
import { FlowEditor, connect, paste, removeSteps, renameStep, withLayout } from '../flows/FlowEditor';
import { NODE_H, NODE_W, layered, positions } from '../flows/layout';
import { FlowDef, compact, diff, newStep, validate } from '../flows/model';
import { TEMPLATES } from '../flows/templates';
import { YamlError, parse, stringify } from '../flows/yaml';
import { FlowsPage } from '../pages/FlowsPage';
import { MockCall, UI_CONFIG, makeClient } from './mockApi';
import { installMatchMedia } from './setup';

beforeAll(installMatchMedia);

const ME = 'urn:li:corpuser:ana';

const FLOW: FlowDef = {
    kind: 'flow',
    name: 'nightly',
    description: 'Refresh, check: report',
    params: [{ name: 'day', type: 'date', default: '{{ run.logical_date }}' }],
    triggers: [{ type: 'schedule', cron: '0 2 * * *', timezone: 'Europe/Berlin' }],
    notify: { on: ['failure'], to: ['urn:li:corpuser:bob'] },
    steps: [
        { name: 'refresh', type: 'derived', table: 'revenue' },
        { name: 'enough', type: 'check', after: ['refresh'], sql: 'SELECT count(*) AS n\nFROM marts.revenue', connection: 'lakehouse', measure: 'n', op: '>=', value: 3 },
        { name: 'size', type: 'branch', after: ['enough'], condition: 'steps.enough.value > 10', then: ['report'], else: ['note'] },
        { name: 'report', type: 'sql', after: ['size'], sql: "SELECT region, 'x: y' AS s FROM marts.revenue", connection: 'lakehouse' },
        { name: 'note', type: 'notify', after: ['size'], message: 'only {{ steps.enough.value }} rows' },
        { name: 'each', type: 'for_each', after: ['report'], items: '{{ steps.report.preview }}', concurrency: 2, step: { type: 'notify', message: '{{ item[0] }}' } },
    ],
    layout: { refresh: { x: 0, y: 0 }, enough: { x: 280, y: 0 } },
};

describe('YAML', () => {
    it('round-trips a flow with its layout, templates, multi-line SQL and the on key', () => {
        const text = stringify(compact(FLOW));
        expect(text).toContain('after: [refresh]');
        expect(text).toContain("message: 'only {{ steps.enough.value }} rows'");
        expect(text).toContain('  sql: |-\n    SELECT count(*) AS n\n    FROM marts.revenue');
        expect(text).toContain('  refresh: {x: 0, y: 0}');
        expect(text).toContain("on: [failure]");
        expect(parse(text)).toEqual(compact(FLOW));
    });

    it('reads what people write by hand and says where it cannot', () => {
        const doc = parse(`# a comment
kind: flow
name: x   # trailing comment
steps:
  - name: a
    type: notify
    message: "quoted: \\"yes\\""
  - {name: b, type: wait, seconds: 5, after: [a]}
  -
    name: c
    type: sql
    sql: >
      SELECT 1
      FROM t
    connection: wh
notify:
  on:
  - failure
  - success
`) as FlowDef;
        expect(doc.steps.map((s) => s.name)).toEqual(['a', 'b', 'c']);
        expect(doc.steps[0].message).toBe('quoted: "yes"');
        expect(doc.steps[1]).toEqual({ name: 'b', type: 'wait', seconds: 5, after: ['a'] });
        expect(doc.steps[2].sql).toBe('SELECT 1 FROM t\n');
        expect(doc.notify).toEqual({ on: ['failure', 'success'] });
        expect(() => parse('name: x\n  steps: 1\n')).toThrow(YamlError);
        expect(() => parse('name: x\nsteps: [a, b\nother: 1\n')).toThrow(/line 2/);
        expect(() => parse('a: &anchor 1\n')).toThrow(/anchors/);
        try {
            parse('kind: flow\nname: x\nname: y\n');
        } catch (e) {
            expect((e as YamlError).line).toBe(3);
        }
    });
});

describe('layout', () => {
    it('puts each step after what it depends on, without overlaps', () => {
        const pos = layered(FLOW.steps);
        for (const s of FLOW.steps) for (const a of s.after ?? []) expect(pos[a].x).toBeLessThan(pos[s.name].x);
        const boxes = Object.values(pos);
        for (let i = 0; i < boxes.length; i++)
            for (let j = i + 1; j < boxes.length; j++)
                expect(Math.abs(boxes[i].x - boxes[j].x) >= NODE_W || Math.abs(boxes[i].y - boxes[j].y) >= NODE_H).toBe(true);
        const kept = positions(FLOW.steps, FLOW.layout);
        expect(kept.refresh).toEqual({ x: 0, y: 0 });
        expect(kept.size.x).toBeGreaterThan(kept.enough.x);
    });
});

describe('checks', () => {
    const paths = (d: FlowDef) => validate(d).map((p) => `${p.path}: ${p.message}`);
    it('match Cairn: cycles, unknown references and types, branch sides, templates', () => {
        expect(validate(FLOW)).toEqual([]);
        const s = (o: object) => ({ name: 'a', type: 'notify', message: 'x', ...o });
        expect(paths({ name: 'f', steps: [s({ after: ['b'] }), s({ name: 'b', after: ['a'] })] })).toEqual(['steps: steps depend on each other in a cycle: a -> b -> a']);
        expect(paths({ name: 'f', steps: [s({ after: ['zz'] })] })).toEqual(["steps[0].after: no step 'zz' to come after"]);
        expect(paths({ name: 'f', steps: [{ name: 'a', type: 'teleport' }] })[0]).toMatch(/^steps\[0\].type: unknown step type 'teleport'/);
        expect(paths({ name: 'f', steps: [s({}), s({ name: 'b', message: '{{ steps.c.rows }}' }), s({ name: 'c' })] }))
            .toEqual(['steps[1]: reads steps.c, which does not come before it (add c to its after)']);
        expect(paths({ name: 'f', steps: [{ name: 'a', type: 'branch', condition: 'true', then: ['b'] }, s({ name: 'b' })] }))
            .toEqual(['steps[0].then: b is on the then side, so it comes after a (add a to its after)']);
        expect(paths({ name: 'Bad Name', steps: [s({})] })[0]).toMatch(/^name:/);
        expect(paths({ name: 'f', steps: [{ name: 'q', type: 'sql', sql: 'SELECT 1' }] })[0]).toMatch(/needs a saved query/);
    });

    it('diffs versions and edits the graph safely', () => {
        const next: FlowDef = { ...FLOW, steps: [...FLOW.steps.filter((x) => x.name !== 'note'), { name: 'extra', type: 'notify', message: 'x' }] };
        next.steps[0] = { ...next.steps[0], full: true };
        expect(diff(FLOW, next)).toEqual({ added: ['extra'], removed: ['note'], changed: [{ name: 'refresh', fields: ['full'] }], settings: [] });
        expect(connect(FLOW, 'each', 'refresh')).toMatch(/cycle/);
        const linked = connect(FLOW, 'note', 'each') as FlowDef;
        expect(linked.steps.find((x) => x.name === 'each')!.after).toEqual(['report', 'note']);
        const renamed = renameStep(FLOW, 'enough', 'rows-ok');
        expect(renamed.steps[1].name).toBe('rows-ok');
        expect(renamed.steps[2]).toMatchObject({ after: ['rows-ok'], condition: 'steps.rows-ok.value > 10' });
        expect(renamed.steps[4].message).toBe('only {{ steps.rows-ok.value }} rows');
        expect(renamed.layout!['rows-ok']).toEqual({ x: 280, y: 0 });
        const removed = removeSteps(FLOW, ['report']);
        expect(removed.steps.find((x) => x.name === 'size')!.then).toEqual([]);
        expect(removed.steps.find((x) => x.name === 'each')!.after).toEqual([]);
        const p = paste(withLayout(FLOW), { steps: FLOW.steps.slice(0, 2), layout: { refresh: { x: 0, y: 0 }, enough: { x: 280, y: 0 } } });
        expect(p.names).toEqual(['refresh-copy', 'enough-copy']);
        expect(p.def.steps.find((x) => x.name === 'enough-copy')!.after).toEqual(['refresh-copy']);
        expect(newStep('notify', ['notify-1']).name).toBe('notify-2');
    });
});

// ---- the editor ---------------------------------------------------------------------------------------------
function detailOf(def: FlowDef, extra: Partial<FlowDetail> = {}): FlowDetail {
    return {
        id: 'f1', name: def.name, owner: ME, description: '', editors: [], viewers: [], enabled: true, published_version: 1, draft_errors: 0,
        next_run_ms: null, updated_ms: 0, updated_by: ME, steps: def.steps.length, role: 'owner', last_run: null, draft: def, errors: [],
        draft_yaml: null, published: def, ...extra,
    };
}

function mountEditor(def: FlowDef, routes: Record<string, unknown> = {}, extra: Partial<FlowDetail> = {}) {
    const calls: MockCall[] = [];
    const { client } = makeClient({ 'GET /ui/config': UI_CONFIG, ...routes }, calls);
    const saved: FlowDetail[] = [];
    const utils = render(
        <CairnProvider client={client} cache={new QueryCache()}>
            <FlowEditor detail={detailOf(def, extra)} api={flowsApi(client)} me={ME} onSaved={(d) => saved.push(d)} onDeleted={() => undefined}
                theme="light" onTheme={() => undefined} />
        </CairnProvider>,
    );
    return { ...utils, calls, saved };
}

const node = (name: string) => document.querySelector(`[data-step="${name}"]`) as HTMLElement;
const SMALL: FlowDef = { kind: 'flow', name: 'small', steps: [{ name: 'a', type: 'notify', message: 'a' }, { name: 'b', type: 'notify', message: 'b' }] };

describe('the flow editor', () => {
    it('adds steps from the palette with the keyboard, links them by dragging, and undoes', () => {
        const { calls } = mountEditor(SMALL);
        void calls;
        expect(node('a')).toBeTruthy();
        // Select a, then add an SQL step from the palette: it comes after a and opens in the side panel.
        fireEvent.pointerDown(node('a'), { button: 0 });
        fireEvent.pointerUp(node('a'));
        fireEvent.click(screen.getByRole('button', { name: 'Add SQL query step' }));
        expect(node('sql-1')).toBeTruthy();
        const panel = screen.getByRole('complementary', { name: 'Step sql-1' });
        expect((within(panel).getByLabelText('a') as HTMLInputElement).checked).toBe(true);    // runs after a
        // Drag from b's handle onto sql-1: sql-1 now also comes after b.
        fireEvent.pointerDown(document.querySelector('[data-handle="b"]')!, { button: 0 });
        fireEvent.pointerMove(screen.getByTestId('flow-canvas'), { clientX: 300, clientY: 100 });
        fireEvent.pointerUp(node('sql-1'));
        expect(document.querySelector('[data-edge="b->sql-1"]')).toBeTruthy();
        // A link that would make a cycle is refused, and says why.
        fireEvent.pointerDown(document.querySelector('[data-handle="sql-1"]')!, { button: 0 });
        fireEvent.pointerUp(node('a'));
        expect(screen.getByRole('alert').textContent).toMatch(/cycle/);
        // Delete removes the selected step; Ctrl+Z brings it back; Ctrl+Shift+Z removes it again.
        const canvas = screen.getByTestId('flow-canvas');
        fireEvent.pointerDown(node('sql-1'), { button: 0 });
        fireEvent.keyDown(canvas, { key: 'Delete' });
        expect(node('sql-1')).toBeNull();
        fireEvent.keyDown(canvas, { key: 'z', ctrlKey: true });
        expect(node('sql-1')).toBeTruthy();
        fireEvent.keyDown(canvas, { key: 'z', ctrlKey: true, shiftKey: true });
        expect(node('sql-1')).toBeNull();
        // Copy and paste: a copy with its own name.
        fireEvent.pointerDown(node('a'), { button: 0 });
        fireEvent.keyDown(canvas, { key: 'c', ctrlKey: true });
        fireEvent.keyDown(canvas, { key: 'v', ctrlKey: true });
        expect(node('a-copy')).toBeTruthy();
        // Ctrl+A then Delete: everything goes, and the canvas says how to start.
        fireEvent.keyDown(canvas, { key: 'a', ctrlKey: true });
        fireEvent.keyDown(canvas, { key: 'Delete' });
        expect(document.querySelectorAll('[data-step]').length).toBe(0);
        expect(screen.getByText(/Drag a step here/)).toBeTruthy();
    });

    it('edits a step in its form with problems shown inline, and moves steps with the arrow keys', () => {
        mountEditor(SMALL);
        fireEvent.keyDown(node('a'), { key: 'Enter' });
        const panel = screen.getByRole('complementary', { name: 'Step a' });
        fireEvent.change(within(panel).getByLabelText(/^Message/), { target: { value: '' } });
        expect(within(panel).getByText('is required')).toBeTruthy();
        expect(node('a').getAttribute('aria-label')).toMatch(/1 problem/);
        fireEvent.change(within(panel).getByLabelText(/^Message/), { target: { value: 'hello {{ steps.b.x }}' } });
        expect(within(panel).getByText(/does not come before it/)).toBeTruthy();
        fireEvent.change(within(panel).getByLabelText(/^Name/), { target: { value: 'first' } });
        fireEvent.blur(within(panel).getByLabelText(/^Name/));
        expect(node('first')).toBeTruthy();
        const before = node('first').style.left;
        fireEvent.keyDown(screen.getByTestId('flow-canvas'), { key: 'ArrowRight' });
        expect(node('first').style.left).not.toBe(before);
    });

    it('keeps the canvas and the YAML in step, both ways', async () => {
        mountEditor(SMALL);
        fireEvent.click(screen.getByRole('tab', { name: 'Code' }));
        const area = screen.getByLabelText('Flow definition (YAML)') as HTMLTextAreaElement;
        expect(area.value).toContain('name: small');
        fireEvent.change(area, { target: { value: `${area.value.replace(/layout:[\s\S]*$/, '')}- {name: c, type: wait, seconds: 5, after: [b]}\n` } });
        fireEvent.click(screen.getByRole('tab', { name: 'Canvas' }));
        expect(node('c')).toBeTruthy();
        expect(document.querySelector('[data-edge="b->c"]')).toBeTruthy();
        fireEvent.click(screen.getByRole('tab', { name: 'Code' }));
        const again = screen.getByLabelText('Flow definition (YAML)') as HTMLTextAreaElement;
        expect(again.value).toContain('name: c');
        fireEvent.change(again, { target: { value: 'kind: flow\nname: small\nsteps: [\n' } });
        expect(screen.getByRole('alert').textContent).toMatch(/line/);
    });

    it('shows a run on the canvas, approves inline and reruns from a step', async () => {
        const def: FlowDef = { kind: 'flow', name: 'gate', steps: [{ name: 'q', type: 'sql', sql: 'SELECT 1', connection: 'wh' },
            { name: 'ok', type: 'approval', after: ['q'], approvers: [ME] }, { name: 'tell', type: 'notify', after: ['ok'], message: 'x' }] };
        const run = (status: string, steps: [string, string][]) => ({
            id: 'r1', flow_id: 'f1', flow: 'gate', version: 1, status, trigger: 'manual', triggered_by: ME, run_as: ME, params: {}, logical_date: '2026-01-01',
            detail: {}, created_ms: 1, started_ms: 1, finished_ms: status === 'succeeded' ? 9 : null, error: null, cancel_requested: false, definition: def,
            steps: steps.map(([key, st]) => ({ key, step: key, type: def.steps.find((s) => s.name === key)!.type, status: st, attempt: 1, started_ms: 1,
                finished_ms: null, next_ms: null, error: null, output: key === 'q' ? { rows: 1 } : null, reused: false, decision: null,
                ...(key === 'ok' ? { approvers: [ME], groups: [] } : {}) })),
        });
        const waiting = run('waiting', [['q', 'ok'], ['ok', 'waiting'], ['tell', 'pending']]);
        const done = run('succeeded', [['q', 'ok'], ['ok', 'ok'], ['tell', 'ok']]);
        let current: unknown = waiting;
        const { calls } = mountEditor(def, {
            'POST /v1/flows/gate/runs': () => waiting,
            'GET /v1/flows/runs/r1': () => current,
            'POST /v1/flows/runs/r1/steps/ok/approve': () => {
                current = done;
                return done;
            },
            'POST /v1/flows/runs/r1/rerun': () => ({ ...done, id: 'r2' }),
            'GET /v1/flows/runs/r2': () => ({ ...done, id: 'r2' }),
        });
        fireEvent.click(screen.getByRole('button', { name: /^Run$/ }));
        await waitFor(() => expect(node('ok').className).toContain('st-waiting'));
        expect(node('q').className).toContain('st-ok');
        expect(document.querySelector('[data-edge="q->ok"] path')!.getAttribute('class')).toContain('live');
        fireEvent.pointerDown(node('ok'), { button: 0 });
        fireEvent.change(screen.getByLabelText('Comment'), { target: { value: 'go' } });
        fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
        await waitFor(() => expect(node('tell').className).toContain('st-ok'));
        expect(calls.find((c) => c.path === '/v1/flows/runs/r1/steps/ok/approve')!.body).toEqual({ comment: 'go' });
        fireEvent.pointerDown(node('q'), { button: 0 });
        fireEvent.click(screen.getByRole('button', { name: 'Rerun from here' }));
        await waitFor(() => expect(calls.some((c) => c.path === '/v1/flows/runs/r1/rerun' && (c.body as { from_step: string }).from_step === 'q')).toBe(true));
    });

    it('shows what publishing changes, then publishes', async () => {
        const { calls } = mountEditor(SMALL, {
            'PUT /v1/flows/small': (b: unknown) => detailOf(b as FlowDef),
            'POST /v1/flows/small/publish': { flow: 'small', version: 2 },
            'GET /v1/flows/small': detailOf(SMALL, { published_version: 2 }),
        }, { published: { ...SMALL, steps: [...SMALL.steps, { name: 'gone', type: 'notify', message: 'x' }] } });
        fireEvent.click(screen.getByRole('button', { name: 'Add Wait step' }));
        fireEvent.click(screen.getByRole('button', { name: /Publish/ }));
        const dialog = screen.getByRole('dialog', { name: 'Publish small' });
        expect(within(dialog).getByText('+ step wait-1 added')).toBeTruthy();
        expect(within(dialog).getByText(/step gone removed/)).toBeTruthy();
        fireEvent.change(within(dialog).getByLabelText('Note'), { target: { value: 'adds a wait' } });
        await act(async () => {
            fireEvent.click(within(dialog).getByRole('button', { name: 'Publish' }));
        });
        await waitFor(() => expect(calls.some((c) => c.path === '/v1/flows/small/publish')).toBe(true));
        expect((calls.find((c) => c.method === 'PUT')!.body as FlowDef).steps.map((s) => s.name)).toEqual(['a', 'b', 'wait-1']);
        expect(calls.find((c) => c.path === '/v1/flows/small/publish')!.body).toEqual({ note: 'adds a wait' });
    });
});

describe('the Flows page', () => {
    it('lists flows and makes a draft from a template', async () => {
        const calls: MockCall[] = [];
        const created: FlowDef[] = [];
        const { client } = makeClient({
            'GET /ui/config': UI_CONFIG,
            'GET /me': { urn: ME },
            'GET /v1/flows': [{ ...detailOf(FLOW), last_run: { id: 'r', status: 'failed', created_ms: 1, finished_ms: 2 } }],
            'GET /v1/flows/approvals': [{ run: 'r9', flow: 'gate', step: 'ok', message: 'go on?', since_ms: 1 }],
            'POST /v1/flows': (b: unknown) => {
                created.push(b as FlowDef);
                return detailOf(b as FlowDef);
            },
            'GET /v1/flows/my-flow': () => detailOf(created[0]),
        }, calls);
        render(
            <MemoryRouter initialEntries={['/flows']}>
                <CairnProvider client={client} cache={new QueryCache()}>
                    <FlowsPage />
                </CairnProvider>
            </MemoryRouter>,
        );
        expect(await screen.findByRole('button', { name: 'nightly' })).toBeTruthy();
        expect(screen.getByText('failed')).toBeTruthy();
        expect(screen.getByText(/gate \/ ok/)).toBeTruthy();
        fireEvent.click(screen.getAllByRole('button', { name: /New flow/ })[0]);
        fireEvent.click(screen.getByRole('radio', { name: /Agent triage with approval/ }));
        fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: 'my-flow' } });
        fireEvent.click(screen.getByRole('button', { name: 'Create draft' }));
        await waitFor(() => expect(created.length).toBe(1));
        expect(created[0].name).toBe('my-flow');
        expect(created[0].steps.map((s) => s.type)).toEqual(['sql', 'branch', 'agent', 'approval', 'notify']);
        expect(validate(created[0])).toEqual([]);
        expect(Object.keys(created[0].layout!)).toHaveLength(5);
        for (const t of TEMPLATES) expect(validate(t.make('x', ME))).toEqual([]);
    });
});
