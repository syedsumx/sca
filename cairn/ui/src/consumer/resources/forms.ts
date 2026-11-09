/**
 * Forms for the platform areas' actions: the schemas (generated from the API by `python -m cairn.iac.forms`, see
 * forms.generated.json), and everything about values that is not drawing them: starting values, the request body,
 * Raw JSON both ways, validation, the API's errors placed on fields, and cron in words.
 */
import generated from './forms.generated.json';
import type { ResourceAction } from './types';

export type FieldType =
    | 'text' | 'textarea' | 'number' | 'integer' | 'boolean' | 'select' | 'multiselect' | 'list' | 'keyvalue' | 'group'
    | 'objects' | 'duration' | 'cron' | 'datetime' | 'date' | 'month' | 'sql' | 'code' | 'json' | 'password' | 'picker'
    | 'file';

export type PickerKind =
    | 'table' | 'dataset' | 'connection' | 'warehouse' | 'principal' | 'user' | 'group' | 'secret_scope' | 'secret_ref'
    | 'git_folder' | 'flow' | 'agent' | 'ref';

export interface FormField {
    name: string;
    label: string;
    type: FieldType;
    required?: boolean;
    default?: unknown;
    description?: string;
    enum?: (string | number)[];
    /** The options are suggestions: other values are allowed. */
    open?: boolean;
    min?: number;
    max?: number;
    min_length?: number;
    max_length?: number;
    pattern?: string;
    unit?: string;
    /** A duration's number type. */
    number?: 'integer' | 'number';
    /** A list's item type (text when absent). */
    item?: 'integer' | 'number' | 'textarea' | 'sql' | 'text';
    /** A key-value list's value type (text when absent). */
    value?: 'integer';
    key_picker?: PickerKind;
    picker?: PickerKind;
    multiple?: boolean;
    ref?: { kind: string; read: string; list_key: string; value: string; label: string };
    fields?: FormField[];
    advanced?: boolean;
    write_only?: boolean;
    sensitive?: boolean;
    /** The field's name in the records Cairn returns (dotted for nested), when it differs. */
    read_as?: string;
    /** Set when created; an edit cannot change it. */
    immutable?: boolean;
    computed?: boolean;
    /** The path placeholder the field fills. */
    path?: string;
    /** false: the value goes in the path only. */
    in_body?: boolean;
    accept?: string;
}

export interface FormSchema {
    /** registry:<kind>, openapi, or example (a plain JSON object body, typed from the example). */
    source: string;
    mode: 'create' | 'update' | 'upsert' | 'action';
    fields: FormField[];
    /** Where an edit reads the whole record (the list may leave fields out). */
    prefill?: { read: string; list_key: string; id: string; singleton: boolean };
}

export const FORMS: Record<string, FormSchema> = (generated as unknown as { forms: Record<string, FormSchema> }).forms;

/**
 * Actions that send a body but keep the raw JSON editor, each with why. Empty today: every create, change and run
 * in the catalog has a form. An entry here must say why a form cannot do better than JSON.
 */
export const FORM_EXCLUSIONS: Record<string, string> = {};

export const formKey = (a: Pick<ResourceAction, 'method' | 'path' | 'form'>) => a.form ?? `${a.method} ${a.path}`;

/** A create, change or run that sends a body the person fills in (DELETEs and fixed bodies are confirmations). */
export function needsForm(a: ResourceAction): boolean {
    return a.method !== 'DELETE' && a.body !== null && a.body !== undefined && !a.fixed;
}

export function formFor(a: ResourceAction): FormSchema | null {
    return needsForm(a) ? FORMS[formKey(a)] ?? null : null;
}

// ---- values -----------------------------------------------------------------------------------------------------
/** What the person edits: strings for text-like fields, numbers, booleans, string lists, pairs, nested values. */
export type Values = Record<string, unknown>;
export interface Pair { key: string; value: string }

const TEXTLIKE = new Set<FieldType>(['text', 'textarea', 'sql', 'code', 'cron', 'password', 'date', 'month', 'select', 'file']);

function isPlainObject(v: unknown): v is Record<string, unknown> {
    return !!v && typeof v === 'object' && !Array.isArray(v);
}

export function getPath(obj: unknown, dotted: string): unknown {
    let cur: unknown = obj;
    for (const k of dotted.split('.')) {
        if (!isPlainObject(cur)) return undefined;
        cur = cur[k];
    }
    return cur;
}

function pad(n: number) {
    return String(n).padStart(2, '0');
}

/** An API value as the editor holds it. */
export function toEditor(f: FormField, v: unknown): unknown {
    if (v === undefined || v === null) return undefined;
    switch (f.type) {
        case 'boolean':
            return typeof v === 'boolean' ? v : v === 'true';
        case 'number': case 'integer': case 'duration':
            return typeof v === 'number' ? v : Number.isFinite(Number(v)) && v !== '' ? Number(v) : undefined;
        case 'json':
            return typeof v === 'string' ? v : JSON.stringify(v, null, 2);
        case 'datetime': {
            const d = new Date(typeof v === 'number' ? (v > 1e11 ? v : v * 1000) : String(v));
            if (Number.isNaN(d.getTime())) return String(v);
            return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
        }
        case 'keyvalue':
            return isPlainObject(v) ? Object.entries(v).map(([key, value]) => ({ key, value: value === null ? '' : String(value) })) : [];
        case 'group':
            return isPlainObject(v) ? fromBodyFields(f.fields ?? [], v).values : {};
        case 'objects':
            return Array.isArray(v) ? v.map((x) => (isPlainObject(x) ? fromBodyFields(f.fields ?? [], x).values : {})) : [];
        case 'list': case 'multiselect':
            return Array.isArray(v) ? v.map((x) => (typeof x === 'string' ? x : String(x))) : [String(v)];
        case 'picker':
            if (f.multiple) return Array.isArray(v) ? v.map(String) : [String(v)];
            return String(v);
        default:
            return typeof v === 'string' ? v : typeof v === 'object' ? JSON.stringify(v) : String(v);
    }
}

export class FieldValueError extends Error {
    constructor(readonly field: string, readonly code: 'json' | 'number') {
        super(`${field}: ${code}`);
    }
}

/** The editor's value as the API takes it; undefined when empty. */
export function fromEditor(f: FormField, v: unknown, where = f.name): unknown {
    if (v === undefined || v === null) return undefined;
    switch (f.type) {
        case 'boolean':
            return !!v;
        case 'number': case 'integer': case 'duration':
            return typeof v === 'number' && Number.isFinite(v) ? v : undefined;
        case 'json': {
            const s = String(v).trim();
            if (!s) return undefined;
            try {
                return JSON.parse(s);
            } catch {
                throw new FieldValueError(where, 'json');
            }
        }
        case 'datetime': {
            const s = String(v).trim();
            if (!s) return undefined;
            const d = new Date(s);
            return Number.isNaN(d.getTime()) ? s : d.toISOString();
        }
        case 'keyvalue': {
            const out: Record<string, unknown> = {};
            for (const p of (v as Pair[]) ?? []) {
                if (!p.key.trim()) continue;
                if (f.value === 'integer') {
                    const n = Number(p.value);
                    if (!Number.isFinite(n)) throw new FieldValueError(where, 'number');
                    out[p.key.trim()] = n;
                } else out[p.key.trim()] = p.value;
            }
            return Object.keys(out).length ? out : undefined;
        }
        case 'group': {
            const out = toBodyFields(f.fields ?? [], v as Values, where);
            return Object.keys(out).length ? out : undefined;
        }
        case 'objects': {
            const items = ((v as Values[]) ?? []).map((x, i) => toBodyFields(f.fields ?? [], x, `${where}[${i}]`))
                .filter((x) => Object.keys(x).length);
            return items.length ? items : undefined;
        }
        case 'list': case 'multiselect': {
            const items = ((v as string[]) ?? []).map((x) => String(x).trim()).filter(Boolean);
            if (f.item === 'integer' || f.item === 'number') {
                return items.map((x) => {
                    const n = Number(x);
                    if (!Number.isFinite(n)) throw new FieldValueError(where, 'number');
                    return n;
                });
            }
            return items;
        }
        case 'picker':
            if (f.multiple) return ((v as string[]) ?? []).filter(Boolean);
            return String(v).trim() || undefined;
        default: {
            const s = String(v);
            return s.trim() === '' ? undefined : s;
        }
    }
}

function toBodyFields(fields: FormField[], values: Values, where = ''): Record<string, unknown> {
    const out: Record<string, unknown> = {};
    for (const f of fields) {
        const v = fromEditor(f, values?.[f.name], where ? `${where}.${f.name}` : f.name);
        if (v !== undefined) out[f.name] = v;
    }
    return out;
}

function fromBodyFields(fields: FormField[], body: Record<string, unknown>): { values: Values; extra: Record<string, unknown> } {
    const values: Values = {};
    const known = new Set(fields.map((f) => f.name));
    for (const f of fields) {
        const v = toEditor(f, body[f.name]);
        if (v !== undefined) values[f.name] = v;
    }
    const extra: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(body)) if (!known.has(k)) extra[k] = v;
    return { values, extra };
}

function sameJson(a: unknown, b: unknown) {
    return JSON.stringify(a) === JSON.stringify(b);
}

/**
 * The request body: fields that go in the body, empty ones left out (a list emptied on an edit is sent empty, so it
 * clears), keys only Raw JSON knew kept. A PATCH sends only what changed.
 */
export function toBody(form: FormSchema, values: Values, extra: Record<string, unknown> = {}, initial?: Values,
    method?: string): Record<string, unknown> {
    const editing = form.mode === 'update' || form.mode === 'upsert';
    const out: Record<string, unknown> = { ...extra };
    for (const f of form.fields) {
        if (f.in_body === false) continue;
        let v = fromEditor(f, values[f.name]);
        if (v === undefined && editing && initial && initial[f.name] !== undefined &&
            (f.type === 'list' || f.type === 'multiselect' || (f.type === 'picker' && f.multiple))) v = [];
        if (v === undefined) {
            delete out[f.name];
            continue;
        }
        if (method === 'PATCH' && initial && sameJson(v, fromEditorSafe(f, initial[f.name]))) continue;
        out[f.name] = v;
    }
    return out;
}

function fromEditorSafe(f: FormField, v: unknown) {
    try {
        return fromEditor(f, v);
    } catch {
        return undefined;
    }
}

/** Raw JSON back into the form: known fields become values, the rest is kept as it is. */
export function fromBody(form: FormSchema, body: unknown, current: Values = {}): { values: Values; extra: Record<string, unknown> } {
    if (!isPlainObject(body)) throw new Error('not an object');
    const { values, extra } = fromBodyFields(form.fields.filter((f) => f.in_body !== false), body);
    for (const f of form.fields) if (f.in_body === false && current[f.name] !== undefined) values[f.name] = current[f.name];
    return { values, extra };
}

/**
 * Where the form starts. An edit (update or upsert on a row) starts from the record, read under each field's read
 * name, never a write-only one; otherwise from the field's default, and for an action from the example's numbers,
 * switches and choices (its text is a placeholder). A row action's path field starts from the row.
 */
export function initialValues(form: FormSchema, action: ResourceAction, row: Record<string, unknown> | null,
    record?: Record<string, unknown> | null, edit?: boolean): Values {
    const editing = edit ?? (!!row && (form.mode === 'update' || form.mode === 'upsert'));
    const source = record ?? row;
    const example = isPlainObject(action.body) ? action.body : {};
    const firstPlace = /\{(\w+)\}/.exec(action.path)?.[1];
    const out: Values = {};
    for (const f of form.fields) {
        let v: unknown;
        if (editing && source && !f.write_only) {
            v = getPath(source, f.read_as ?? f.name);
            if (v === undefined && f.path && row) v = row[f.path];
        } else if (row && f.path && f.path === firstPlace) {
            v = row[f.path] ?? row[f.name];
        }
        if (v === undefined && f.default !== undefined && !f.write_only) v = f.default;
        if (v === undefined && form.mode === 'action' && f.name in example) {
            const e = example[f.name];
            const choice = f.type === 'select' || f.type === 'boolean' || f.type === 'number' || f.type === 'integer' ||
                f.type === 'duration' || (f.type === 'list' && (f.item === 'integer' || f.item === 'number'));
            if (choice && e !== null && (typeof e !== 'string' || f.type === 'select')) v = e;
        }
        const ed = toEditor(f, v);
        if (ed !== undefined) out[f.name] = ed;
    }
    return out;
}

/** The example's text, shown as a field's placeholder. */
export function placeholderOf(action: ResourceAction, f: FormField): string | undefined {
    const e = isPlainObject(action.body) ? action.body[f.name] : undefined;
    if (typeof e === 'string' && !e.includes('base64') && e !== '…') return e;
    if (Array.isArray(e) && e.every((x) => typeof x === 'string')) return e.join(', ');
    return undefined;
}

/** The action's path with each placeholder filled from its field or the row; null names the first one missing. */
export function fillPath(action: ResourceAction, form: FormSchema | null, values: Values, row: Record<string, unknown> | null):
    { path: string; missing: null } | { path: null; missing: string } {
    let missing: string | null = null;
    const [bare, query] = action.path.split('?');
    const out = bare.replace(/\{(\w+)\}/g, (_m, k: string) => {
        const f = form?.fields.find((x) => x.path === k);
        let v: unknown = f ? fromEditorSafe(f, values[f.name]) : undefined;
        if ((v === undefined || v === '') && row) v = row[k];
        if (v === undefined || v === null || v === '') {
            missing = missing ?? (f?.label ?? k);
            return '';
        }
        return encodeURIComponent(String(v));
    });
    if (missing) return { path: null, missing };
    return { path: query ? `${out}?${query}` : out, missing: null };
}

// ---- validation -------------------------------------------------------------------------------------------------
export type Problem =
    | { code: 'required' } | { code: 'pattern' } | { code: 'min'; n: number } | { code: 'max'; n: number }
    | { code: 'minLength'; n: number } | { code: 'maxLength'; n: number } | { code: 'json' } | { code: 'number' }
    | { code: 'cron' } | { code: 'option' };

function empty(f: FormField, v: unknown): boolean {
    if (v === undefined || v === null) return true;
    if (typeof v === 'string') return v.trim() === '';
    if (Array.isArray(v)) return v.length === 0;
    if (f.type === 'group' && isPlainObject(v)) return Object.values(v).every((x) => x === undefined || x === '' ||
        (Array.isArray(x) && !x.length));
    return false;
}

function check(f: FormField, v: unknown, key: string, out: Record<string, Problem>) {
    if (empty(f, v)) {
        if (f.required && f.type !== 'boolean') out[key] = { code: 'required' };
        return;
    }
    if (f.type === 'number' || f.type === 'integer' || f.type === 'duration') {
        const n = v as number;
        if (typeof n !== 'number' || !Number.isFinite(n)) out[key] = { code: 'number' };
        else if (f.min !== undefined && n < f.min) out[key] = { code: 'min', n: f.min };
        else if (f.max !== undefined && n > f.max) out[key] = { code: 'max', n: f.max };
        return;
    }
    if (f.type === 'json') {
        try {
            JSON.parse(String(v));
        } catch {
            out[key] = { code: 'json' };
        }
        return;
    }
    if (f.type === 'cron') {
        if (!validCron(String(v))) out[key] = { code: 'cron' };
        return;
    }
    if (f.type === 'select' && f.enum && !f.open && !f.enum.map(String).includes(String(v))) {
        out[key] = { code: 'option' };
        return;
    }
    if (f.type === 'group') {
        for (const sub of f.fields ?? []) check(sub, (v as Values)[sub.name], `${key}.${sub.name}`, out);
        return;
    }
    if (f.type === 'objects') {
        (v as Values[]).forEach((item, i) => {
            for (const sub of f.fields ?? []) check(sub, item[sub.name], `${key}[${i}].${sub.name}`, out);
        });
        return;
    }
    if (f.type === 'keyvalue' && f.value === 'integer') {
        if ((v as Pair[]).some((p) => p.key.trim() && !Number.isFinite(Number(p.value)))) out[key] = { code: 'number' };
        return;
    }
    const strings = Array.isArray(v) ? (v as string[]) : TEXTLIKE.has(f.type) || f.type === 'picker' ? [String(v)] : [];
    if ((f.type === 'list' || f.type === 'multiselect') && (f.item === 'integer' || f.item === 'number') &&
        strings.some((s) => !Number.isFinite(Number(s)))) {
        out[key] = { code: 'number' };
        return;
    }
    if (f.pattern) {
        let re: RegExp | null = null;
        try {
            re = new RegExp(f.pattern);
        } catch {
            re = null;
        }
        if (re && strings.some((s) => !re!.test(s))) {
            out[key] = { code: 'pattern' };
            return;
        }
    }
    if (typeof v === 'string') {
        if (f.min_length !== undefined && v.length < f.min_length) out[key] = { code: 'minLength', n: f.min_length };
        else if (f.max_length !== undefined && v.length > f.max_length) out[key] = { code: 'maxLength', n: f.max_length };
    }
}

/** Problems by field key (`name`, `group.sub`, `items[0].sub`); empty when the form can be sent. */
export function validate(form: FormSchema, values: Values): Record<string, Problem> {
    const out: Record<string, Problem> = {};
    for (const f of form.fields) check(f, values[f.name], f.name, out);
    return out;
}

// ---- the API's errors on fields ---------------------------------------------------------------------------------
function escapeRe(s: string) {
    return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * Where the API's refusal belongs. FastAPI's 422 says it in `loc` (["body", "name", ...]); other refusals are a
 * sentence, which is placed on a field when it starts with the field's name or quotes it. The rest is the form's.
 */
export function mapServerErrors(form: FormSchema, status: number | undefined, body: unknown):
    { fields: Record<string, string>; rest: string[] } {
    const fields: Record<string, string> = {};
    const rest: string[] = [];
    const detail = isPlainObject(body) ? body.detail : typeof body === 'string' ? body : undefined;
    const names = new Set(form.fields.map((f) => f.name));
    const pathOf = (loc: unknown[]): string | null => {
        const parts = loc[0] === 'body' ? loc.slice(1) : loc[0] === 'path' || loc[0] === 'query' ? loc.slice(1) : loc;
        if (!parts.length) return null;
        let key = '';
        for (const p of parts) key += typeof p === 'number' ? `[${p}]` : key ? `.${String(p)}` : String(p);
        const top = String(parts[0]);
        if (names.has(top)) return key;
        const byPath = form.fields.find((f) => f.path === top);
        return byPath ? byPath.name : null;
    };
    const bySentence = (text: string): string | null => {
        const byLength = [...form.fields].sort((a, b) => b.name.length - a.name.length);
        const hit = byLength.find((f) => {
            const n = escapeRe(f.name);
            return new RegExp(`^(value error, )?${n}\\b`, 'i').test(text) || new RegExp(`['"\`]${n}['"\`]`).test(text) ||
                new RegExp(`\\b${n}(:|\\.\\w| is | must | should | needs? | requires? )`).test(text);
        });
        return hit ? hit.name : null;
    };
    if (Array.isArray(detail)) {
        for (const d of detail) {
            if (isPlainObject(d) && Array.isArray(d.loc)) {
                const msg = String(d.msg ?? 'Not valid');
                const key = pathOf(d.loc) ?? bySentence(msg);
                if (key && !fields[key]) fields[key] = msg;
                else if (!key) rest.push(msg);
            } else rest.push(typeof d === 'string' ? d : JSON.stringify(d));
        }
        return { fields, rest };
    }
    if (typeof detail === 'string' && status !== undefined && status >= 400 && status < 500 && status !== 401 && status !== 403) {
        const key = bySentence(detail);
        if (key) fields[key] = detail;
        else rest.push(detail);
        return { fields, rest };
    }
    if (detail !== undefined) rest.push(typeof detail === 'string' ? detail : JSON.stringify(detail));
    return { fields, rest };
}

// ---- cron -------------------------------------------------------------------------------------------------------
const CRON_RANGES: [number, number][] = [[0, 59], [0, 23], [1, 31], [1, 12], [0, 7]];
const NAMES: Record<number, string[]> = {
    3: ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'],
    4: ['SUN', 'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT'],
};

function cronNumber(tok: string, i: number): number | null {
    const names = NAMES[i];
    if (names) {
        const at = names.indexOf(tok.toUpperCase());
        if (at >= 0) return i === 3 ? at + 1 : at;
    }
    if (!/^\d+$/.test(tok)) return null;
    const n = Number(tok);
    const [lo, hi] = CRON_RANGES[i];
    return n >= lo && n <= hi ? n : null;
}

/** Five fields (minute hour day-of-month month day-of-week), each `*`, numbers, ranges, lists and steps. */
export function validCron(expr: string): boolean {
    const parts = expr.trim().split(/\s+/);
    if (parts.length !== 5) return false;
    return parts.every((p, i) => p.split(',').every((item) => {
        const [base, step] = item.split('/');
        if (step !== undefined && !/^\d+$/.test(step)) return false;
        if (step !== undefined && Number(step) === 0) return false;
        if (base === '*') return true;
        const [a, b] = base.split('-');
        if (cronNumber(a, i) === null) return false;
        return b === undefined || cronNumber(b, i) !== null;
    }));
}

export type CronWords =
    | { key: 'cron.everyMinute' }
    | { key: 'cron.everyNMinutes'; n: number }
    | { key: 'cron.hourly'; minute: string }
    | { key: 'cron.everyNHours'; n: number; minute: string }
    | { key: 'cron.daily'; time: string }
    | { key: 'cron.weekdays'; time: string }
    | { key: 'cron.weekly'; days: number[]; time: string }
    | { key: 'cron.monthly'; day: number; time: string }
    | { key: 'cron.custom' };

/** A cron schedule in words (UTC), for the common shapes; anything else is "custom". */
export function describeCron(expr: string): CronWords | null {
    if (!validCron(expr)) return null;
    const [mi, h, dom, mon, dow] = expr.trim().split(/\s+/);
    const num = (s: string) => /^\d+$/.test(s);
    const time = () => `${h.padStart(2, '0')}:${mi.padStart(2, '0')}`;
    if (mon !== '*') return { key: 'cron.custom' };
    if (mi === '*' && h === '*' && dom === '*' && dow === '*') return { key: 'cron.everyMinute' };
    let m = /^\*\/(\d+)$/.exec(mi);
    if (m && h === '*' && dom === '*' && dow === '*') return { key: 'cron.everyNMinutes', n: Number(m[1]) };
    if (num(mi) && h === '*' && dom === '*' && dow === '*') return { key: 'cron.hourly', minute: mi.padStart(2, '0') };
    m = /^\*\/(\d+)$/.exec(h);
    if (num(mi) && m && dom === '*' && dow === '*') return { key: 'cron.everyNHours', n: Number(m[1]), minute: mi.padStart(2, '0') };
    if (!num(mi) || !num(h)) return { key: 'cron.custom' };
    if (dom === '*' && dow === '*') return { key: 'cron.daily', time: time() };
    if (dom === '*') {
        if (dow === '1-5' || dow.toUpperCase() === 'MON-FRI') return { key: 'cron.weekdays', time: time() };
        const days: number[] = [];
        for (const item of dow.split(',')) {
            const [a, b] = item.split('-');
            const x = cronNumber(a, 4);
            const y = b === undefined ? x : cronNumber(b, 4);
            if (x === null || y === null || item.includes('/')) return { key: 'cron.custom' };
            for (let d = x; d <= y; d++) days.push(d % 7);
        }
        return { key: 'cron.weekly', days, time: time() };
    }
    if (num(dom) && dow === '*') return { key: 'cron.monthly', day: Number(dom), time: time() };
    return { key: 'cron.custom' };
}

// ---- durations and sizes ----------------------------------------------------------------------------------------
const SECONDS: Record<string, number> = { milliseconds: 0.001, seconds: 1, minutes: 60, hours: 3600, days: 86400 };

/** A duration in the largest whole units: 1440 minutes → { n: 1, unit: 'days' }. */
export function durationParts(value: number, unit: string): { n: number; unit: string } | null {
    const s = value * (SECONDS[unit] ?? 1);
    if (!Number.isFinite(s) || s <= 0) return null;
    const u = ['days', 'hours', 'minutes', 'seconds'].find((x) => s / SECONDS[x] >= 1) ?? 'seconds';
    if (u === unit) return null;
    return { n: Math.round((s / SECONDS[u]) * 10) / 10, unit: u };
}

export function bytesText(n: number): string {
    const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
    let i = 0;
    let v = n;
    while (v >= 1024 && i < units.length - 1) {
        v /= 1024;
        i++;
    }
    return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}
