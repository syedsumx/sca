/**
 * Pickers: fields whose value names something in Cairn, chosen from what the API lists or finds (tables, people and
 * groups, connections, warehouses, secret scopes and references, git folders, flows, agents, another resource).
 * A picker never blocks: when its list cannot be read, or the thing is not listed, the typed value is used.
 */
import { Select } from 'antd';
import React, { useEffect, useMemo, useState } from 'react';

import type { ConsumerClient } from '../api/client';
import { useConsumerApi, useDheQuery } from '../api/hooks';
import { useT } from '../i18n';
import { actorName } from '../utils/format';
import type { FormField, PickerKind } from './forms';

export interface PickerOption {
    value: string;
    label: string;
}

type Row = Record<string, unknown>;

function listOf(data: unknown, key = ''): Row[] {
    if (Array.isArray(data)) return data as Row[];
    if (data && typeof data === 'object') {
        const obj = data as Row;
        if (key && Array.isArray(obj[key])) return obj[key] as Row[];
        const first = Object.values(obj).find(Array.isArray);
        if (first) return first as Row[];
    }
    return [];
}

const str = (v: unknown) => (v === undefined || v === null ? '' : String(v));

/** `urn:li:dataset:(urn:li:dataPlatform:iceberg,lakehouse.sales.orders,PROD)` → `sales.orders` (namespace.table). */
export function tableOfUrn(urn: string): string {
    const m = /^urn:li:dataset:\(urn:li:dataPlatform:[^,]+,([^,]+),/.exec(urn);
    if (!m) return urn;
    const parts = m[1].split('.');
    return parts.length > 2 ? parts.slice(-2).join('.') : m[1];
}

/** The tables the person may query, by connection (`/data/sources`, then `/sql/schema`): what the catalog search falls back to. */
async function schemaTables(c: ConsumerClient, signal: AbortSignal): Promise<{ urn: string; name: string }[]> {
    const sources = listOf(await c.request<unknown>('GET', '/data/sources', undefined, { signal }));
    const out: { urn: string; name: string }[] = [];
    for (const s of sources.slice(0, 6)) {
        const conn = str(s.connection ?? s.name);
        if (!conn) continue;
        try {
            const tables = listOf(await c.request<unknown>('GET', '/sql/schema', undefined, { signal, query: { connection: conn } }));
            for (const t of tables) out.push({ urn: str(t.urn), name: str(t.name) });
        } catch {
            // a connection that cannot list its tables is skipped
        }
    }
    return out;
}

/** The fixed list a picker offers before anything is typed. */
async function loadList(c: ConsumerClient, kind: PickerKind, field: FormField, signal: AbortSignal): Promise<PickerOption[]> {
    const get = (path: string) => c.request<unknown>('GET', path, undefined, { signal });
    switch (kind) {
        case 'connection':
            return listOf(await get('/data/sources')).map((r) => ({ value: str(r.connection ?? r.name),
                label: r.platform ? `${str(r.connection ?? r.name)} (${str(r.platform)})` : str(r.connection ?? r.name) }));
        case 'warehouse':
            return listOf(await get('/v1/warehouses')).map((r) => ({ value: str(r.name), label: str(r.name) }));
        case 'table':
            return (await schemaTables(c, signal)).map((t) => ({ value: t.name, label: t.name }));
        case 'dataset':
            return (await schemaTables(c, signal)).filter((t) => t.urn).map((t) => ({ value: t.urn, label: t.name }));
        case 'secret_scope':
            return listOf(await get('/v1/secret-scopes')).map((r) => ({ value: str(r.name), label: str(r.name) }));
        case 'secret_ref': {
            const scopes = listOf(await get('/v1/secret-scopes')).slice(0, 12);
            const out: PickerOption[] = [];
            for (const s of scopes) {
                try {
                    const d = (await get(`/v1/secret-scopes/${encodeURIComponent(str(s.name))}`)) as Row;
                    for (const k of listOf(d, 'secrets')) {
                        const ref = `scope:${str(s.name)}/${str(k.key)}`;
                        out.push({ value: ref, label: `${str(s.name)} / ${str(k.key)}` });
                    }
                } catch {
                    // a scope the person may not read is left out
                }
            }
            return out;
        }
        case 'git_folder':
            return listOf(await get('/v1/git-folders')).map((r) => ({ value: str(r.id ?? r.name), label: str(r.name ?? r.id) }));
        case 'flow':
            return listOf(await get('/v1/flows')).map((r) => ({ value: str(r.name), label: str(r.title ?? r.name) }));
        case 'agent': {
            const out: PickerOption[] = [];
            for (const path of ['/v1/custom-agents', '/v1/agent-apps']) {
                try {
                    for (const r of listOf(await get(path))) out.push({ value: str(r.name), label: str(r.name) });
                } catch {
                    // agent apps or custom agents may be off here
                }
            }
            return out;
        }
        case 'principal': case 'user': case 'group': {
            const me = await get('/me').catch(() => null) as Row | null;
            return me?.urn && kind !== 'group' ? [{ value: str(me.urn), label: actorName(str(me.urn)) }] : [];
        }
        case 'ref': {
            const ref = field.ref;
            if (!ref) return [];
            return listOf(await get(ref.read), ref.list_key).map((r) => ({ value: str(r[ref.value]), label: str(r[ref.label] ?? r[ref.value]) }));
        }
        default:
            return [];
    }
}

/** What the catalog search finds for typed text (Cairn Core's search; none without it). */
async function search(c: ConsumerClient, kind: PickerKind, q: string, signal: AbortSignal): Promise<PickerOption[]> {
    const types = kind === 'table' || kind === 'dataset' ? ['DATASET'] : kind === 'user' ? ['CORP_USER']
        : kind === 'group' ? ['CORP_GROUP'] : kind === 'principal' ? ['CORP_USER', 'CORP_GROUP'] : [];
    if (!types.length) return [];
    const r = await c.search({ query: q, filters: { _entityType: types }, count: 10 }, signal);
    return r.results.map((e) => {
        if (kind === 'table') return { value: tableOfUrn(e.urn), label: `${tableOfUrn(e.urn)}${e.platform ? ` (${e.platform})` : ''}` };
        if (kind === 'dataset') return { value: e.urn, label: e.name || tableOfUrn(e.urn) };
        return { value: e.urn, label: e.name || actorName(e.urn) };
    });
}

/** Options for what was typed: a person or a group by name, or the text itself. */
function typed(kind: PickerKind, q: string, t: ReturnType<typeof useT>): PickerOption[] {
    const v = q.trim();
    if (!v) return [];
    if (kind === 'principal' || kind === 'user' || kind === 'group') {
        if (v.startsWith('urn:li:')) return [{ value: v, label: v }];
        const out: PickerOption[] = [];
        if (kind !== 'group') out.push({ value: `urn:li:corpuser:${v}`, label: t('form.person', { name: v }) });
        if (kind !== 'user') out.push({ value: `urn:li:corpGroup:${v}`, label: t('form.group', { name: v }) });
        return out;
    }
    return [{ value: v, label: t('form.useValue', { value: v }) }];
}

export function labelOf(kind: PickerKind | undefined, value: string): string {
    if (kind === 'principal' || kind === 'user' || kind === 'group') return actorName(value);
    if (kind === 'dataset') return tableOfUrn(value);
    return value;
}

export function Picker({ id, field, value, onChange, describedBy, invalid, disabled, placeholder, ariaLabel }: {
    id: string;
    field: FormField;
    value: unknown;
    onChange: (v: unknown) => void;
    describedBy?: string;
    invalid?: boolean;
    disabled?: boolean;
    placeholder?: string;
    /** Where no <label> names it. */
    ariaLabel?: string;
}) {
    const t = useT();
    const { client } = useConsumerApi();
    const kind = (field.picker ?? 'ref') as PickerKind;
    const [q, setQ] = useState('');
    const [found, setFound] = useState<PickerOption[]>([]);
    const [opened, setOpened] = useState(false);
    const listKey = kind === 'ref' ? `picker:ref:${field.ref?.read}` : `picker:${kind}`;
    const list = useDheQuery<PickerOption[]>(opened ? listKey : null, (c, signal) => loadList(c, kind, field, signal), { staleMs: 60_000 });
    useEffect(() => {
        if (q.trim().length < 2) {
            setFound([]);
            return undefined;
        }
        const ctrl = new AbortController();
        const timer = setTimeout(() => {
            search(client, kind, q.trim(), ctrl.signal).then(setFound).catch(() => setFound([]));
        }, 250);
        return () => {
            clearTimeout(timer);
            ctrl.abort();
        };
    }, [client, kind, q]);
    const multiple = !!field.multiple;
    const selected = multiple ? ((value as string[]) ?? []) : value ? [String(value)] : [];
    const options = useMemo(() => {
        const seen = new Set<string>();
        const out: PickerOption[] = [];
        const needle = q.trim().toLowerCase();
        const add = (o: PickerOption) => {
            if (!o.value || seen.has(o.value)) return;
            seen.add(o.value);
            out.push(o);
        };
        for (const o of list.data ?? []) if (!needle || o.label.toLowerCase().includes(needle) || o.value.toLowerCase().includes(needle)) add(o);
        found.forEach(add);
        typed(kind, q, t).forEach(add);
        for (const v of selected) add({ value: v, label: labelOf(kind, v) });
        return out;
    }, [list.data, found, q, kind, t, selected]);
    return (
        <Select
            id={id}
            aria-describedby={describedBy}
            aria-label={ariaLabel}
            data-invalid={invalid || undefined}
            showSearch
            allowClear
            disabled={disabled}
            mode={multiple ? 'multiple' : undefined}
            style={{ width: '100%' }}
            value={multiple ? selected : (selected[0] ?? undefined)}
            placeholder={placeholder}
            filterOption={false}
            searchValue={q}
            onSearch={setQ}
            onDropdownVisibleChange={(o) => o && setOpened(true)}
            onFocus={() => setOpened(true)}
            loading={list.loading}
            notFoundContent={list.loading ? t('form.searching') : list.error ? t('form.pickerFailed') : t('form.noOptions')}
            options={options}
            onChange={(v: string | string[] | undefined) => {
                setQ('');
                onChange(multiple ? (v as string[]) ?? [] : (v as string | undefined) ?? undefined);
            }}
        />
    );
}
