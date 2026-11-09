/**
 * Cairn Flows in the browser: the definition (`kind: flow`), its step types (grouped, each with an icon and a
 * colour), defaults, checks (the same rules as Cairn's, so the canvas shows problems as you build) and diffs.
 * Cairn checks again when a draft is saved or published; its answer is the one that counts.
 */

import type { IconName } from '../components/Icons';

export type Json = null | boolean | number | string | Json[] | { [k: string]: Json };

export interface FlowStep {
    name: string;
    type: string;
    after?: string[];
    when?: string;
    retries?: number;
    retry_delay_s?: number;
    retry_backoff?: number;
    timeout_s?: number;
    description?: string;
    reads?: string[];
    writes?: string[];
    [field: string]: unknown;
}

export interface FlowParam {
    name: string;
    type?: string;
    default?: unknown;
    required?: boolean;
    description?: string;
}

export interface FlowTrigger {
    type: string;
    cron?: string;
    timezone?: string;
    tables?: string[];
    flow?: string;
    on?: string[];
    enabled?: boolean;
}

export interface FlowDef {
    kind?: 'flow';
    name: string;
    description?: string;
    params?: FlowParam[];
    triggers?: FlowTrigger[];
    concurrency?: { policy?: string; max_active?: number };
    sla_minutes?: number | null;
    notify?: { on?: string[]; to?: string[] };
    steps: FlowStep[];
    layout?: Record<string, { x: number; y: number }>;
}

export type FieldKind = 'text' | 'code' | 'str' | 'int' | 'num' | 'bool' | 'enum' | 'list' | 'dict' | 'names' | 'people' | 'expr' | 'items' | 'child';

export interface FieldSpec {
    name: string;
    label: string;
    kind: FieldKind;
    default?: unknown;
    required?: boolean;
    values?: string[];
    min?: number;
    max?: number;
    help?: string;
}

export interface StepType {
    type: string;
    group: Group;
    label: string;
    icon: IconName;
    timeout: number;
    blurb: string;
    fields: FieldSpec[];
}

export type Group = 'Data' | 'Compute' | 'AI' | 'Control' | 'Notify';

export const GROUPS: { name: Group; color: string; soft: string; darkSoft: string }[] = [
    { name: 'Data', color: '#2563eb', soft: '#e8f0fe', darkSoft: '#172554' },
    { name: 'Compute', color: '#7c3aed', soft: '#f1ebfe', darkSoft: '#2e1065' },
    { name: 'AI', color: '#db2777', soft: '#fde8f2', darkSoft: '#500724' },
    { name: 'Control', color: '#b45309', soft: '#fdf1e2', darkSoft: '#451a03' },
    { name: 'Notify', color: '#047857', soft: '#e3f6ef', darkSoft: '#022c22' },
];

export const OPS = ['>', '>=', '<', '<=', '==', '!='];
export const WHEN = ['all_ok', 'none_failed', 'all_done', 'any_failed'];
export const WHEN_LABEL: Record<string, string> = {
    all_ok: 'when everything before it succeeded',
    none_failed: 'unless something before it failed',
    all_done: 'when everything before it finished',
    any_failed: 'only if something before it failed',
};

const f = (name: string, label: string, kind: FieldKind, extra: Partial<FieldSpec> = {}): FieldSpec => ({ name, label, kind, ...extra });

export const STEP_TYPES: StepType[] = [
    { type: 'sql', group: 'Data', label: 'SQL query', icon: 'code', timeout: 3600, blurb: 'Run a query as the owner (a saved query, or SQL on a connection).',
        fields: [f('sql', 'SQL', 'code'), f('connection', 'Connection', 'str'), f('query', 'Saved query (id or name)', 'str'), f('limit', 'Row limit', 'int', { default: 1000, min: 1, max: 10000 })] },
    { type: 'derived', group: 'Data', label: 'Refresh table', icon: 'table', timeout: 21600, blurb: 'Refresh a derived table the owner owns.',
        fields: [f('table', 'Derived table', 'str', { required: true }), f('full', 'Full rebuild', 'bool', { default: false })] },
    { type: 'check', group: 'Data', label: 'Quality check', icon: 'shield', timeout: 3600, blurb: 'A measure compared with a value; a failure stops what comes after.',
        fields: [f('sql', 'SQL', 'code'), f('connection', 'Connection', 'str'), f('query', 'Saved query (id or name)', 'str'),
            f('measure', 'Measure (rows or a column)', 'str', { default: 'rows' }), f('op', 'Operator', 'enum', { required: true, values: OPS }),
            f('value', 'Value', 'num', { required: true })] },
    { type: 'pipe', group: 'Data', label: 'Load files', icon: 'pipes', timeout: 3600, blurb: 'Load the files a pipe has not loaded yet.',
        fields: [f('pipe', 'Pipe', 'str', { required: true })] },
    { type: 'stream', group: 'Data', label: 'Wait for stream', icon: 'pulse', timeout: 3600, blurb: 'Wait until a stream has written what its topic holds now.',
        fields: [f('stream', 'Stream', 'str', { required: true }), f('poll_s', 'Look every (s)', 'int', { default: 15, min: 5, max: 600 })] },
    { type: 'python', group: 'Compute', label: 'Python', icon: 'code', timeout: 3600, blurb: 'Python in a job container, with the owner’s Trino token.',
        fields: [f('code', 'Code', 'code'), f('job', 'Or a job (name)', 'str'), f('params', 'Parameters (JSON)', 'dict', { default: {} })] },
    { type: 'notebook', group: 'Compute', label: 'Notebook', icon: 'book', timeout: 3600, blurb: 'A notebook job, run as the owner.',
        fields: [f('job', 'Job (name)', 'str'), f('notebook', 'Or a notebook (JSON)', 'dict'), f('params', 'Parameters (JSON)', 'dict', { default: {} })] },
    { type: 'spark', group: 'Compute', label: 'Spark job', icon: 'spark', timeout: 14400, blurb: 'spark-submit on the lakehouse as the owner.',
        fields: [f('application', 'Application', 'str', { required: true }), f('class_name', 'Main class', 'str'), f('args', 'Arguments', 'list', { default: [] }), f('conf', 'Spark settings (JSON)', 'dict', { default: {} })] },
    { type: 'dbt', group: 'Compute', label: 'dbt', icon: 'cube', timeout: 14400, blurb: 'Run or build dbt models on Trino as the owner.',
        fields: [f('command', 'Command', 'enum', { default: 'build', values: ['run', 'build', 'test', 'seed', 'snapshot'] }), f('select', 'Select', 'str', { default: '' }), f('project_dir', 'Project directory', 'str', { default: '/dbt' })] },
    { type: 'airflow', group: 'Compute', label: 'Airflow DAG', icon: 'flow', timeout: 21600, blurb: 'Trigger a DAG run in Airflow and wait for it.',
        fields: [f('dag_id', 'DAG id', 'str', { required: true }), f('conf', 'Conf (JSON)', 'dict', { default: {} }), f('wait', 'Wait for it', 'bool', { default: true }), f('poll_s', 'Look every (s)', 'int', { default: 30, min: 5, max: 600 })] },
    { type: 'agent', group: 'AI', label: 'Agent', icon: 'bot', timeout: 900, blurb: 'Ask an agent app; its answer is there for later steps.',
        fields: [f('app', 'Agent app', 'str', { required: true }), f('input', 'Input', 'text', { required: true })] },
    { type: 'approval', group: 'Control', label: 'Approval', icon: 'user', timeout: 259200, blurb: 'Wait for a person or a group to approve in Cairn.',
        fields: [f('approvers', 'Approvers', 'people', { default: [] }), f('groups', 'Groups', 'list', { default: [] }), f('message', 'Message', 'text', { default: '' })] },
    { type: 'wait', group: 'Control', label: 'Wait', icon: 'clock', timeout: 604800, blurb: 'Wait a while, or until a time.',
        fields: [f('seconds', 'Seconds', 'int', { min: 1, max: 604800 }), f('until', 'Or until (HH:MM or a date and time)', 'str')] },
    { type: 'branch', group: 'Control', label: 'Branch', icon: 'branch', timeout: 60, blurb: 'Choose a side from what earlier steps gave.',
        fields: [f('condition', 'Condition', 'expr', { required: true, help: 'e.g. steps.count.rows > 100' }), f('then', 'Then', 'names', { default: [] }), f('else', 'Else', 'names', { default: [] })] },
    { type: 'for_each', group: 'Control', label: 'For each', icon: 'repeat', timeout: 21600, blurb: 'Repeat a step over a list, a few at a time.',
        fields: [f('items', 'Items', 'items', { required: true, help: 'a list, or {{ steps.x.preview }}' }), f('concurrency', 'At once', 'int', { default: 4, min: 1, max: 32 }), f('step', 'Step to repeat', 'child', { required: true })] },
    { type: 'notify', group: 'Notify', label: 'Notify', icon: 'bell', timeout: 60, blurb: 'Tell people (in Cairn and their channels).',
        fields: [f('to', 'To (default: the owner)', 'people', { default: [] }), f('title', 'Title', 'str', { default: '' }), f('message', 'Message', 'text', { required: true })] },
];

export const CHILD_TYPES = ['sql', 'derived', 'check', 'pipe', 'notebook', 'python', 'spark', 'dbt', 'airflow', 'agent', 'notify'];
export const TYPE_BY: Record<string, StepType> = Object.fromEntries(STEP_TYPES.map((t) => [t.type, t]));
export const GROUP_BY: Record<string, (typeof GROUPS)[number]> = Object.fromEntries(GROUPS.map((g) => [g.name, g]));
const COMMON = ['name', 'type', 'after', 'when', 'retries', 'retry_delay_s', 'retry_backoff', 'timeout_s', 'description', 'reads', 'writes'];
const STEP_NAME = /^[A-Za-z][A-Za-z0-9_-]{0,62}$/;
export const FLOW_NAME = /^[a-z][a-z0-9_-]{0,62}$/;
const TEMPLATE = /\{\{\s*([\s\S]+?)\s*\}\}/g;

export interface Problem {
    path: string;
    message: string;
    step?: string;
}

/** Step names an expression or template reads (steps.<name>). */
export function refs(value: unknown): Set<string> {
    const out = new Set<string>();
    const scan = (text: string) => {
        const re = /\bsteps(?:\.([A-Za-z][A-Za-z0-9_-]*)|\[\s*['"]([^'"]+)['"]\s*\])/g;
        let m: RegExpExecArray | null;
        while ((m = re.exec(text))) out.add(m[1] ?? m[2]);
    };
    const walk = (v: unknown) => {
        if (typeof v === 'string') {
            let m: RegExpExecArray | null;
            const re = new RegExp(TEMPLATE.source, 'g');
            while ((m = re.exec(v))) scan(m[1]);
        } else if (Array.isArray(v)) v.forEach(walk);
        else if (v && typeof v === 'object') Object.values(v as Record<string, unknown>).forEach(walk);
    };
    walk(value);
    return out;
}

function exprRefs(text: unknown): Set<string> {
    const out = new Set<string>();
    if (typeof text !== 'string') return out;
    const inner = /^\s*\{\{([\s\S]*)\}\}\s*$/.exec(text)?.[1] ?? text;
    const re = /\bsteps(?:\.([A-Za-z][A-Za-z0-9_-]*)|\[\s*['"]([^'"]+)['"]\s*\])/g;
    let m: RegExpExecArray | null;
    while ((m = re.exec(inner))) out.add(m[1] ?? m[2]);
    return out;
}

/** A cycle among the steps (names, first repeated at the end), or null. */
export function findCycle(steps: FlowStep[]): string[] | null {
    const by = new Map(steps.map((s) => [s.name, s]));
    const state = new Map<string, number>();
    const path: string[] = [];
    const visit = (n: string): string[] | null => {
        state.set(n, 1);
        path.push(n);
        for (const a of by.get(n)?.after ?? []) {
            if (!by.has(a)) continue;
            if (state.get(a) === 1) return [...path.slice(path.indexOf(a)), a];
            if (!state.has(a)) {
                const found = visit(a);
                if (found) return found;
            }
        }
        state.set(n, 2);
        path.pop();
        return null;
    };
    for (const s of steps) {
        if (!state.has(s.name)) {
            const found = visit(s.name);
            if (found) return found.reverse();
        }
    }
    return null;
}

function ancestors(steps: FlowStep[]): Map<string, Set<string>> {
    const by = new Map(steps.map((s) => [s.name, s]));
    const memo = new Map<string, Set<string>>();
    const anc = (n: string, seen: Set<string>): Set<string> => {
        if (memo.has(n)) return memo.get(n)!;
        const out = new Set<string>();
        for (const a of by.get(n)?.after ?? []) {
            if (!by.has(a) || seen.has(a)) continue;
            out.add(a);
            anc(a, new Set([...seen, n])).forEach((x) => out.add(x));
        }
        memo.set(n, out);
        return out;
    };
    return new Map(steps.map((s) => [s.name, anc(s.name, new Set())]));
}

function checkFields(t: StepType, s: Record<string, unknown>, path: string, name: string, out: Problem[], child = false) {
    const allowed = new Set([...t.fields.map((x) => x.name), ...COMMON]);
    if (child) ['name', 'after', 'when'].forEach((k) => allowed.delete(k));
    for (const k of Object.keys(s)) {
        if (!allowed.has(k) && k !== 'type') out.push({ path: `${path}.${k}`, message: `is not a field of a ${t.type} step`, step: name });
    }
    for (const fs of t.fields) {
        const v = s[fs.name];
        const empty = v === undefined || v === null || v === '';
        if (fs.required && empty && fs.kind !== 'child') out.push({ path: `${path}.${fs.name}`, message: 'is required', step: name });
        if (empty) continue;
        if (fs.kind === 'int' && (typeof v !== 'number' || !Number.isInteger(v) || (fs.min !== undefined && v < fs.min) || (fs.max !== undefined && v > fs.max)))
            out.push({ path: `${path}.${fs.name}`, message: `is a whole number from ${fs.min} to ${fs.max}`, step: name });
        if (fs.kind === 'num' && (typeof v !== 'number' || Number.isNaN(v))) out.push({ path: `${path}.${fs.name}`, message: 'is a number', step: name });
        if (fs.kind === 'enum' && !fs.values!.includes(String(v))) out.push({ path: `${path}.${fs.name}`, message: `is one of ${fs.values!.join(', ')}`, step: name });
        if (fs.kind === 'people' && (!Array.isArray(v) || !v.every((p) => typeof p === 'string' && /^urn:li:(corpuser|corpGroup):\S+$/.test(p))))
            out.push({ path: `${path}.${fs.name}`, message: 'is a list of people or groups (urn:li:corpuser:… or urn:li:corpGroup:…)', step: name });
        if (fs.kind === 'dict' && (typeof v !== 'object' || Array.isArray(v))) out.push({ path: `${path}.${fs.name}`, message: 'is a mapping', step: name });
        if (fs.kind === 'bool' && typeof v !== 'boolean') out.push({ path: `${path}.${fs.name}`, message: 'is true or false', step: name });
        if (typeof v === 'string' && /\{\{(?![\s\S]*\}\})/.test(v)) out.push({ path: `${path}.${fs.name}`, message: 'has a {{ without its }}', step: name });
    }
    if ((t.type === 'sql' || t.type === 'check') && !s.query && !(s.sql && s.connection))
        out.push({ path, message: 'needs a saved query (query) or sql and a connection', step: name });
    if ((t.type === 'python' || t.type === 'notebook') && !!s.job === !!s[t.type === 'python' ? 'code' : 'notebook'])
        out.push({ path, message: `needs either job (a job's name) or ${t.type === 'python' ? 'code' : 'notebook'}`, step: name });
    if (t.type === 'wait' && (s.seconds === undefined || s.seconds === null) === (s.until === undefined || s.until === null || s.until === ''))
        out.push({ path, message: 'needs either seconds or until (HH:MM, or a date and time)', step: name });
    if (t.type === 'approval' && !((s.approvers as unknown[]) ?? []).length && !((s.groups as unknown[]) ?? []).length)
        out.push({ path, message: 'needs approvers (people) or groups who may approve', step: name });
    if (t.type === 'for_each') {
        const c = s.step as Record<string, unknown> | undefined;
        if (!c || typeof c !== 'object' || !CHILD_TYPES.includes(String(c.type))) {
            out.push({ path: `${path}.step`, message: `is the step to repeat: a type, one of ${CHILD_TYPES.join(', ')}`, step: name });
        } else {
            checkFields(TYPE_BY[String(c.type)], c, `${path}.step`, name, out, true);
        }
    }
    if (s.retries !== undefined && (typeof s.retries !== 'number' || s.retries < 0 || s.retries > 10 || !Number.isInteger(s.retries)))
        out.push({ path: `${path}.retries`, message: 'is 0 to 10', step: name });
    if (s.timeout_s !== undefined && (typeof s.timeout_s !== 'number' || s.timeout_s < 1 || s.timeout_s > 604800))
        out.push({ path: `${path}.timeout_s`, message: 'is 1 to 604800 seconds', step: name });
}

/** Every problem with a definition, with its path (the same paths Cairn reports). */
export function validate(d: FlowDef | null | undefined): Problem[] {
    const out: Problem[] = [];
    if (!d || typeof d !== 'object') return [{ path: '', message: 'a flow is a mapping (kind: flow, name, steps)' }];
    if (!d.name || !FLOW_NAME.test(d.name)) out.push({ path: 'name', message: 'is lowercase letters, digits, - and _ (starting with a letter, up to 63)' });
    if (!Array.isArray(d.steps) || d.steps.length < 1) {
        out.push({ path: 'steps', message: 'is a list of 1 to 200 steps' });
        return out;
    }
    const names = new Set<string>();
    const good: FlowStep[] = [];
    d.steps.forEach((s, i) => {
        const path = `steps[${i}]`;
        if (!s || typeof s !== 'object') return out.push({ path, message: 'is a mapping with a name and a type' });
        if (typeof s.name !== 'string' || !STEP_NAME.test(s.name)) return out.push({ path: `${path}.name`, message: 'is letters, digits, - and _ (starting with a letter, up to 63)' });
        if (names.has(s.name)) return out.push({ path: `${path}.name`, message: `${s.name} is the name of another step`, step: s.name });
        names.add(s.name);
        const t = TYPE_BY[s.type];
        if (!t) return out.push({ path: `${path}.type`, message: `unknown step type '${s.type}' (the types are ${STEP_TYPES.map((x) => x.type).join(', ')})`, step: s.name });
        if (s.when !== undefined && !WHEN.includes(s.when)) out.push({ path: `${path}.when`, message: `is one of ${WHEN.join(', ')}`, step: s.name });
        checkFields(t, s, path, s.name, out);
        good.push(s);
        return undefined;
    });
    const idx = new Map(d.steps.map((s, i) => [s?.name, i]));
    for (const s of good) {
        for (const a of s.after ?? []) {
            if (a === s.name) out.push({ path: `steps[${idx.get(s.name)}].after`, message: 'a step cannot come after itself', step: s.name });
            else if (!names.has(a)) out.push({ path: `steps[${idx.get(s.name)}].after`, message: `no step '${a}' to come after`, step: s.name });
        }
    }
    const cycle = findCycle(good);
    if (cycle) {
        out.push({ path: 'steps', message: `steps depend on each other in a cycle: ${cycle.join(' -> ')}`, step: cycle[0] });
        return out;
    }
    const anc = ancestors(good);
    for (const s of good) {
        const p = `steps[${idx.get(s.name)}]`;
        const fields: Record<string, unknown> = {};
        for (const [k, v] of Object.entries(s)) if (!COMMON.includes(k) && k !== 'then' && k !== 'else' && k !== 'condition' && k !== 'items') fields[k] = v;
        const r = refs(fields);
        if (s.type === 'branch') exprRefs(s.condition).forEach((x) => r.add(x));
        if (s.type === 'for_each') exprRefs(s.items).forEach((x) => r.add(x));
        for (const x of Array.from(r).sort()) {
            if (!names.has(x)) out.push({ path: p, message: `reads steps.${x}, but there is no step '${x}'`, step: s.name });
            else if (!anc.get(s.name)!.has(x)) out.push({ path: p, message: `reads steps.${x}, which does not come before it (add ${x} to its after)`, step: s.name });
        }
        if (s.type === 'branch') {
            for (const side of ['then', 'else'] as const) {
                for (const t of (s[side] as string[] | undefined) ?? []) {
                    const target = good.find((g) => g.name === t);
                    if (!target) out.push({ path: `${p}.${side}`, message: `no step '${t}'`, step: s.name });
                    else if (!(target.after ?? []).includes(s.name))
                        out.push({ path: `${p}.${side}`, message: `${t} is on the ${side} side, so it comes after ${s.name} (add ${s.name} to its after)`, step: s.name });
                }
            }
            if (!((s.then as string[]) ?? []).length && !((s.else as string[]) ?? []).length)
                out.push({ path: p, message: 'names the steps of at least one side (then or else)', step: s.name });
        }
    }
    for (const [i, t] of (d.triggers ?? []).entries()) {
        if (!['schedule', 'dataset', 'flow', 'manual', 'api'].includes(t.type)) out.push({ path: `triggers[${i}]`, message: 'is a trigger with a type: schedule, dataset, flow, manual, api' });
        if (t.type === 'schedule' && !(typeof t.cron === 'string' && t.cron.trim().split(/\s+/).length === 5))
            out.push({ path: `triggers[${i}].cron`, message: 'is a five-field cron expression, such as 0 2 * * *' });
        if (t.type === 'dataset' && !(t.tables ?? []).length) out.push({ path: `triggers[${i}].tables`, message: 'is a list of 1 to 50 tables (schema.table, or a dataset urn)' });
        if (t.type === 'flow' && t.flow === d.name) out.push({ path: `triggers[${i}].flow`, message: 'a flow cannot start after itself' });
    }
    return out;
}

/** Problems of one step, by its name. */
export function problemsByStep(problems: Problem[]): Map<string, Problem[]> {
    const out = new Map<string, Problem[]>();
    for (const p of problems) if (p.step) out.set(p.step, [...(out.get(p.step) ?? []), p]);
    return out;
}

/** A new step of a type with its defaults and a name of its own. */
export function newStep(type: string, taken: Iterable<string>): FlowStep {
    const t = TYPE_BY[type];
    const used = new Set(taken);
    const base = type.replace(/_/g, '-');
    let n = 1;
    while (used.has(`${base}-${n}`)) n += 1;
    const step: FlowStep = { name: `${base}-${n}`, type };
    for (const fs of t.fields) if (fs.required && fs.kind === 'enum') step[fs.name] = fs.values![0];
    if (type === 'sql') Object.assign(step, { sql: 'SELECT 1', connection: 'lakehouse' });
    if (type === 'check') Object.assign(step, { sql: 'SELECT count(*) AS n FROM schema.table', connection: 'lakehouse', measure: 'n', op: '>', value: 0 });
    if (type === 'python') step.code = 'print("hello from Cairn")\n';
    if (type === 'notify') step.message = 'Done';
    if (type === 'wait') step.seconds = 60;
    if (type === 'branch') Object.assign(step, { condition: 'true', then: [], else: [] });
    if (type === 'for_each') Object.assign(step, { items: '[1, 2, 3]', step: { type: 'notify', message: '{{ item }}' } });
    if (type === 'agent') step.input = 'Summarise the run';
    return step;
}

/** What changes from `old` to `next`: steps added, removed, changed (fields) and other settings. Positions are not changes. */
export function diff(old: FlowDef | null | undefined, next: FlowDef) {
    const a = new Map((old?.steps ?? []).map((s) => [s.name, s]));
    const b = new Map(next.steps.map((s) => [s.name, s]));
    const changed: { name: string; fields: string[] }[] = [];
    const same = (x: unknown, y: unknown) => JSON.stringify(sortKeys(x)) === JSON.stringify(sortKeys(y));
    for (const [n, s] of b) {
        const o = a.get(n);
        if (!o || same(o, s)) continue;
        const keys = Array.from(new Set([...Object.keys(o), ...Object.keys(s)])).filter((k) => !same(o[k] ?? defaultOf(o.type, k), s[k] ?? defaultOf(s.type, k)));
        if (keys.length) changed.push({ name: n, fields: keys.sort() });
    }
    const settings = ['description', 'params', 'triggers', 'concurrency', 'sla_minutes', 'notify'].filter(
        (k) => !same((old as unknown as Record<string, unknown>)?.[k] ?? null, (next as unknown as Record<string, unknown>)[k] ?? null),
    );
    return { added: Array.from(b.keys()).filter((n) => !a.has(n)), removed: Array.from(a.keys()).filter((n) => !b.has(n)), changed, settings };
}

function defaultOf(type: string, key: string): unknown {
    if (key === 'after') return [];
    if (key === 'when') return 'all_ok';
    if (key === 'retries') return 0;
    if (key === 'retry_delay_s') return 30;
    if (key === 'retry_backoff') return 2;
    if (key === 'timeout_s') return TYPE_BY[type]?.timeout;
    if (key === 'description') return '';
    if (key === 'reads' || key === 'writes') return [];
    const fs = TYPE_BY[type]?.fields.find((x) => x.name === key);
    return fs?.default ?? null;
}

function sortKeys(v: unknown): unknown {
    if (Array.isArray(v)) return v.map(sortKeys);
    if (v && typeof v === 'object') return Object.fromEntries(Object.keys(v as object).sort().map((k) => [k, sortKeys((v as Record<string, unknown>)[k])]));
    return v;
}

/** The definition without what Cairn fills in by default (what the YAML shows). */
export function compact(d: FlowDef): FlowDef {
    const out: FlowDef = { kind: 'flow', name: d.name, steps: [] };
    if (d.description) out.description = d.description;
    if (d.params?.length) out.params = d.params.map((p) => {
        const q: FlowParam = { name: p.name };
        if (p.type && p.type !== 'string') q.type = p.type;
        if (p.default !== undefined && p.default !== null) q.default = p.default;
        if (p.required) q.required = true;
        if (p.description) q.description = p.description;
        return q;
    });
    if (d.triggers?.length) out.triggers = d.triggers.map((t) => {
        const q: FlowTrigger = { ...t };
        if (q.enabled === true) delete q.enabled;
        if (q.timezone === 'UTC') delete q.timezone;
        if (q.on && q.on.length === 1 && q.on[0] === 'succeeded') delete q.on;
        return q;
    });
    const c = d.concurrency;
    if (c && !(c.policy === 'queue' && (c.max_active ?? 1) === 1) && !(c.policy === undefined && c.max_active === undefined)) {
        out.concurrency = c.policy === 'allow' && c.max_active === 10 ? { policy: 'allow' } : { ...c };
    }
    if (d.sla_minutes) out.sla_minutes = d.sla_minutes;
    const n = d.notify;
    if (n && ((n.to ?? []).length || JSON.stringify(n.on ?? ['failure', 'sla']) !== JSON.stringify(['failure', 'sla']))) {
        out.notify = { on: n.on ?? ['failure', 'sla'], ...((n.to ?? []).length ? { to: n.to } : {}) };
    }
    out.steps = d.steps.map((s) => compactStep(s));
    if (d.layout && Object.keys(d.layout).length) out.layout = d.layout;
    return out;
}

export function compactStep(s: FlowStep | Record<string, unknown>, child = false): FlowStep {
    const t = TYPE_BY[String(s.type)];
    const out: FlowStep = child ? ({ type: String(s.type) } as FlowStep) : { name: String(s.name), type: String(s.type) };
    if (!child && Array.isArray(s.after) && s.after.length) out.after = s.after as string[];
    if (!child && s.when && s.when !== 'all_ok') out.when = String(s.when);
    for (const fs of t?.fields ?? []) {
        const v = s[fs.name];
        if (fs.name === 'step' && v) out.step = compactStep(v as FlowStep, true);
        else if (v !== undefined && v !== null && JSON.stringify(v) !== JSON.stringify(fs.default ?? null) && !(v === '' && (fs.default === undefined || fs.default === '')))
            out[fs.name] = v;
    }
    const defaults: Record<string, unknown> = { retries: 0, retry_delay_s: 30, retry_backoff: 2, timeout_s: t?.timeout, description: '', reads: [], writes: [] };
    for (const [k, dflt] of Object.entries(defaults)) {
        if (s[k] !== undefined && JSON.stringify(s[k]) !== JSON.stringify(dflt)) out[k] = s[k];
    }
    return out;
}
