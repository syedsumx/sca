/**
 * The form for one action, drawn from its schema (forms.ts): every field labelled, with its help, its problem
 * (announced), an "Advanced" disclosure for rarely used fields and a "Raw JSON" view that round-trips with the form.
 */
import { AutoComplete, Button, Input, InputNumber, Select, Switch } from 'antd';
import React, { useState } from 'react';
import styled from 'styled-components';

import type { SchemaTable } from '../api/types';
import { useDheQuery } from '../api/hooks';
import { SqlEditor } from '../components/SqlEditor';
import { tokens } from '../components/tokens';
import { useI18n, useT } from '../i18n';
import type { MessageKey } from '../i18n/messages';
import {
    FormField, FormSchema, Pair, Problem, Values, bytesText, describeCron, durationParts, fromBody, placeholderOf, toBody,
} from './forms';
import { Picker } from './pickers';
import type { ResourceAction } from './types';

const Wrap = styled.div`
    .cairn-field {
        margin-bottom: 14px;
    }
    .cairn-field > label,
    .cairn-field legend {
        display: block;
        font-weight: 600;
        margin-bottom: 4px;
        font-size: 13px;
    }
    .cairn-field .cairn-req {
        color: #a8071a;
        margin-left: 2px;
    }
    .cairn-sr {
        position: absolute;
        width: 1px;
        height: 1px;
        overflow: hidden;
        clip: rect(0 0 0 0);
        white-space: nowrap;
    }
    .cairn-help {
        color: ${tokens.color.textMuted};
        font-size: 12px;
        margin-top: 3px;
    }
    .cairn-error {
        color: #a8071a;
        font-size: 12px;
        margin-top: 3px;
    }
    fieldset {
        border: 1px solid ${tokens.color.border};
        border-radius: ${tokens.radius.md};
        padding: 8px 12px 0;
        margin: 0;
    }
    fieldset legend {
        width: auto;
        padding: 0 4px;
        border: 0;
        margin-bottom: 0;
    }
    .cairn-row {
        display: flex;
        gap: 6px;
        align-items: flex-start;
        margin-bottom: 6px;
    }
    .cairn-row > *:not(button) {
        flex: 1;
    }
    textarea.cairn-mono,
    .cairn-mono textarea {
        font-family: ui-monospace, Menlo, monospace;
        font-size: 12px;
    }
    .cairn-form-bar {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 12px;
        margin-bottom: 12px;
    }
`;

export type Translate = ReturnType<typeof useT>;

/** The required mark: a star to see, a word to hear. */
function Req() {
    const t = useT();
    return (
        <>
            <span className="cairn-req" aria-hidden>*</span>
            <span className="cairn-sr"> ({t('form.requiredSr')})</span>
        </>
    );
}

/** A field's problem as a sentence. */
export function problemText(t: Translate, label: string, p: Problem | string): string {
    if (typeof p === 'string') return p;
    const vars = { label, n: 'n' in p ? p.n : 0 };
    return t(`form.err.${p.code}` as MessageKey, vars);
}

interface Ctx {
    action: ResourceAction;
    errors: Record<string, Problem | string>;
    editing: boolean;
    rowAction: boolean;
    firstPlace?: string;
    tables: SchemaTable[];
    idPrefix: string;
}

function fieldId(ctx: Ctx, key: string) {
    return `${ctx.idPrefix}-${key.replace(/[^\w-]/g, '_')}`;
}

function CronPreview({ value }: { value: string }) {
    const t = useT();
    const { locale } = useI18n();
    const words = value.trim() ? describeCron(value) : null;
    if (!value.trim()) return null;
    if (!words) return <span>{t('cron.invalid')}</span>;
    if (words.key === 'cron.weekly') {
        const fmt = new Intl.DateTimeFormat(locale, { weekday: 'long', timeZone: 'UTC' });
        const days = words.days.map((d) => fmt.format(new Date(Date.UTC(2023, 0, 1 + d)))).join(', ');
        return <span>{t('cron.weekly', { days, time: words.time })}</span>;
    }
    const { key, ...vars } = words;
    return <span>{t(key, vars as Record<string, string | number>)}</span>;
}

/** A unit's name, singular for one (`1 hour`, `2 hours`). */
export function unitText(t: Translate, unit: string, n?: number) {
    const known = ['milliseconds', 'seconds', 'minutes', 'hours', 'days'];
    if (!known.includes(unit)) return unit;
    return t((n === 1 && unit !== 'milliseconds' ? `unit.${unit.slice(0, -1)}` : `unit.${unit}`) as MessageKey);
}

function FieldControl({ f, value, set, ctx, k, describedBy, invalid }: {
    f: FormField; value: unknown; set: (v: unknown) => void; ctx: Ctx; k: string; describedBy: string; invalid: boolean;
}) {
    const t = useT();
    const id = fieldId(ctx, k);
    const placeholder = placeholderOf(ctx.action, f);
    const fixedByRow = ctx.rowAction && !!f.path && f.path === ctx.firstPlace;
    const disabled = (ctx.editing && !!f.immutable) || fixedByRow;
    const aria = { id, 'aria-describedby': describedBy, 'aria-invalid': invalid || undefined, 'aria-required': f.required || undefined };
    // antd puts a Select's ARIA props on its wrapper, where aria-required and aria-invalid are not allowed.
    const selectAria = { id, 'aria-describedby': describedBy };
    switch (f.type) {
        case 'boolean':
            return <Switch {...aria} checked={!!value} disabled={disabled} onChange={(v) => set(v)} />;
        case 'number': case 'integer': case 'duration': {
            const unit = f.type === 'duration' ? unitText(t, f.unit ?? '') : f.unit;
            const n = typeof value === 'number' ? value : undefined;
            const parts = f.type === 'duration' && n !== undefined ? durationParts(n, f.unit ?? '') : null;
            return (
                <div>
                    <InputNumber {...aria} style={{ width: '100%', maxWidth: 260 }} value={n} disabled={disabled}
                        min={f.min} max={f.max} precision={f.type === 'integer' || f.number === 'integer' ? 0 : undefined}
                        placeholder={placeholder} addonAfter={unit || undefined} onChange={(v) => set(v === null ? undefined : v)} />
                    {parts && <div className="cairn-help" aria-live="polite">{t('duration.equals', { n: parts.n, unit: unitText(t, parts.unit, parts.n) })}</div>}
                    {f.unit === 'bytes' && n !== undefined && n > 0 && <div className="cairn-help">= {bytesText(n)}</div>}
                </div>
            );
        }
        case 'select':
            if (f.open) {
                return (
                    <AutoComplete {...selectAria} style={{ width: '100%' }} disabled={disabled} value={(value as string) ?? ''}
                        options={(f.enum ?? []).map((v) => ({ value: String(v) }))} placeholder={placeholder}
                        filterOption={(input, o) => String(o?.value ?? '').toLowerCase().includes(input.toLowerCase())}
                        onChange={(v) => set(v || undefined)} />
                );
            }
            return (
                <Select {...selectAria} style={{ width: '100%' }} allowClear={!f.required} disabled={disabled} value={value as string | undefined}
                    options={(f.enum ?? []).map((v) => ({ value: v, label: String(v) }))} onChange={(v) => set(v ?? undefined)} />
            );
        case 'multiselect':
            return (
                <Select {...selectAria} mode="multiple" style={{ width: '100%' }} disabled={disabled} value={(value as string[]) ?? []}
                    options={(f.enum ?? []).map((v) => ({ value: String(v), label: String(v) }))} onChange={(v) => set(v)} />
            );
        case 'picker':
            return <Picker id={id} field={f} value={value} onChange={set} describedBy={describedBy} invalid={invalid}
                disabled={disabled} placeholder={placeholder} />;
        case 'list': {
            if (f.item === 'sql' || f.item === 'textarea' || f.sensitive) {
                return <ItemList f={f} value={(value as string[]) ?? []} set={set} ctx={ctx} k={k} disabled={disabled} />;
            }
            return (
                <Select {...selectAria} mode="tags" style={{ width: '100%' }} disabled={disabled} value={(value as string[]) ?? []}
                    open={false} tokenSeparators={[',']} placeholder={placeholder ?? t('form.listHint')} onChange={(v) => set(v)} />
            );
        }
        case 'keyvalue':
            return <KeyValue f={f} value={(value as Pair[]) ?? []} set={set} ctx={ctx} k={k} disabled={disabled} />;
        case 'cron':
            return (
                <div>
                    <Input {...aria} className="cairn-mono" value={(value as string) ?? ''} disabled={disabled}
                        placeholder={placeholder ?? '0 2 * * *'} onChange={(e) => set(e.target.value)} />
                    <div className="cairn-help" aria-live="polite"><CronPreview value={(value as string) ?? ''} /></div>
                </div>
            );
        case 'sql':
            return (
                <SqlEditor id={id} labelledBy={`${id}-label`} describedBy={describedBy} invalid={invalid}
                    value={(value as string) ?? ''} onChange={(v) => set(v)} tables={ctx.tables} placeholder={placeholder} />
            );
        case 'code': case 'json':
            return (
                <Input.TextArea {...aria} className="cairn-mono" autoSize={{ minRows: 3, maxRows: 16 }} disabled={disabled}
                    value={(value as string) ?? ''} placeholder={placeholder} onChange={(e) => set(e.target.value)} />
            );
        case 'textarea':
            return (
                <Input.TextArea {...aria} autoSize={{ minRows: 2, maxRows: 10 }} disabled={disabled} value={(value as string) ?? ''}
                    placeholder={placeholder} maxLength={f.max_length} onChange={(e) => set(e.target.value)} />
            );
        case 'password':
            return (
                <Input.Password {...aria} autoComplete="new-password" disabled={disabled} value={(value as string) ?? ''}
                    visibilityToggle={false} onChange={(e) => set(e.target.value)} />
            );
        case 'date': case 'month': case 'datetime':
            return (
                <Input {...aria} type={f.type === 'datetime' ? 'datetime-local' : f.type} style={{ maxWidth: 260 }} disabled={disabled}
                    value={(value as string) ?? ''} onChange={(e) => set(e.target.value)} />
            );
        case 'file':
            return <FileInput id={id} describedBy={describedBy} accept={f.accept} value={value as string | undefined} set={set} />;
        default:
            return (
                <Input {...aria} disabled={disabled} value={(value as string) ?? ''} placeholder={placeholder}
                    maxLength={f.max_length} onChange={(e) => set(e.target.value)} />
            );
    }
}

function FileInput({ id, describedBy, accept, value, set }: {
    id: string; describedBy: string; accept?: string; value?: string; set: (v: unknown) => void;
}) {
    const t = useT();
    const [name, setName] = useState<string | null>(null);
    return (
        <div>
            <input id={id} type="file" aria-describedby={describedBy} accept={accept} onChange={(e) => {
                const file = e.target.files?.[0];
                if (!file) return;
                const reader = new FileReader();
                reader.onload = () => {
                    const url = String(reader.result ?? '');
                    set(url.slice(url.indexOf(',') + 1));
                    setName(t('form.fileChosen', { name: file.name, size: bytesText(file.size) }));
                };
                reader.readAsDataURL(file);
            }} />
            {name && value && <div className="cairn-help">{name}</div>}
        </div>
    );
}

function ItemList({ f, value, set, ctx, k, disabled }: { f: FormField; value: string[]; set: (v: unknown) => void; ctx: Ctx; k: string; disabled: boolean }) {
    const t = useT();
    const items = value.length ? value : [''];
    const put = (i: number, v: string) => set(items.map((x, j) => (j === i ? v : x)));
    return (
        <div>
            {items.map((item, i) => {
                const id = fieldId(ctx, `${k}[${i}]`);
                const label = `${f.label} ${i + 1}`;
                return (
                    <div className="cairn-row" key={i}>
                        {f.item === 'sql' ? (
                            <div aria-label={label} role="group"><SqlEditor id={id} ariaLabel={label} value={item} onChange={(v) => put(i, v)} tables={ctx.tables} /></div>
                        ) : f.sensitive ? (
                            <Input.Password id={id} aria-label={label} autoComplete="new-password" visibilityToggle={false} value={item}
                                disabled={disabled} onChange={(e) => put(i, e.target.value)} />
                        ) : (
                            <Input.TextArea id={id} aria-label={label} autoSize={{ minRows: 2, maxRows: 8 }} value={item} disabled={disabled}
                                onChange={(e) => put(i, e.target.value)} />
                        )}
                        <Button aria-label={t('form.removeItem', { n: i + 1 })} disabled={disabled}
                            onClick={() => set(items.filter((_x, j) => j !== i))}>{t('form.remove')}</Button>
                    </div>
                );
            })}
            <Button size="small" disabled={disabled} onClick={() => set([...items, ''])}>{t('form.addItem', { label: f.label })}</Button>
        </div>
    );
}

function KeyValue({ f, value, set, ctx, k, disabled }: { f: FormField; value: Pair[]; set: (v: unknown) => void; ctx: Ctx; k: string; disabled: boolean }) {
    const t = useT();
    const rows = value.length ? value : [{ key: '', value: '' }];
    const put = (i: number, p: Partial<Pair>) => set(rows.map((x, j) => (j === i ? { ...x, ...p } : x)));
    return (
        <div>
            {rows.map((p, i) => (
                <div className="cairn-row" key={i}>
                    <Input id={i === 0 ? fieldId(ctx, k) : undefined} aria-label={`${f.label}: ${t('form.keyN', { n: i + 1 })}`} value={p.key}
                        placeholder={t('form.key')} disabled={disabled} onChange={(e) => put(i, { key: e.target.value })} />
                    <Input aria-label={`${f.label}: ${t('form.valueN', { n: i + 1 })}`} value={p.value} placeholder={t('form.value')}
                        disabled={disabled} inputMode={f.value === 'integer' ? 'numeric' : undefined} onChange={(e) => put(i, { value: e.target.value })} />
                    <Button aria-label={t('form.removeItem', { n: i + 1 })} disabled={disabled}
                        onClick={() => set(rows.filter((_x, j) => j !== i))}>{t('form.remove')}</Button>
                </div>
            ))}
            <Button size="small" disabled={disabled} onClick={() => set([...rows, { key: '', value: '' }])}>{t('form.addItem', { label: f.label })}</Button>
        </div>
    );
}

function Field({ f, value, set, ctx, k }: { f: FormField; value: unknown; set: (v: unknown) => void; ctx: Ctx; k: string }) {
    const t = useT();
    const id = fieldId(ctx, k);
    const err = ctx.errors[k];
    const helpId = `${id}-help`;
    const errId = `${id}-error`;
    const help = [f.description, ctx.editing && f.immutable ? t('form.immutable') : null,
        f.type === 'password' && ctx.editing ? t('form.passwordKeep') : f.write_only ? t('form.passwordNew') : null]
        .filter(Boolean).join(' ');
    const describedBy = [help ? helpId : null, err ? errId : null].filter(Boolean).join(' ') || undefined;
    if (f.type === 'group') {
        const v = (value as Values) ?? {};
        return (
            <fieldset className="cairn-field" aria-describedby={describedBy}>
                <legend>{f.label}{f.required && <Req />}</legend>
                {help && <div className="cairn-help" id={helpId} style={{ marginBottom: 8 }}>{help}</div>}
                {(f.fields ?? []).map((sub) => (
                    <Field key={sub.name} f={sub} value={v[sub.name]} ctx={ctx} k={`${k}.${sub.name}`}
                        set={(x) => set({ ...v, [sub.name]: x })} />
                ))}
                {err && <div className="cairn-error" id={errId} role="alert">{problemText(t, f.label, err)}</div>}
            </fieldset>
        );
    }
    if (f.type === 'objects') {
        const items = (value as Values[]) ?? [];
        return (
            <fieldset className="cairn-field" aria-describedby={describedBy}>
                <legend>{f.label}{f.required && <Req />}</legend>
                {help && <div className="cairn-help" id={helpId} style={{ marginBottom: 8 }}>{help}</div>}
                {items.map((item, i) => (
                    <fieldset key={i} style={{ marginBottom: 8 }}>
                        <legend>{t('form.item', { n: i + 1 })}</legend>
                        {(f.fields ?? []).map((sub) => (
                            <Field key={sub.name} f={sub} value={item[sub.name]} ctx={ctx} k={`${k}[${i}].${sub.name}`}
                                set={(x) => set(items.map((it, j) => (j === i ? { ...it, [sub.name]: x } : it)))} />
                        ))}
                        <Button size="small" style={{ marginBottom: 8 }} aria-label={t('form.removeItem', { n: i + 1 })}
                            onClick={() => set(items.filter((_x, j) => j !== i))}>{t('form.remove')}</Button>
                    </fieldset>
                ))}
                <Button size="small" style={{ marginBottom: 10 }} onClick={() => set([...items, {}])}>{t('form.addItem', { label: f.label })}</Button>
                {err && <div className="cairn-error" id={errId} role="alert">{problemText(t, f.label, err)}</div>}
            </fieldset>
        );
    }
    const labelFor = f.type === 'keyvalue' || (f.type === 'list' && (f.item === 'sql' || f.item === 'textarea' || f.sensitive)) ? undefined : id;
    return (
        <div className="cairn-field" data-field={k}>
            {labelFor ? (
                <label htmlFor={labelFor} id={`${id}-label`}>{f.label}{f.required && <Req />}</label>
            ) : (
                <span style={{ display: 'block', fontWeight: 600, marginBottom: 4, fontSize: 13 }} id={`${id}-label`}>
                    {f.label}{f.required && <Req />}
                </span>
            )}
            <FieldControl f={f} value={value} set={set} ctx={ctx} k={k} describedBy={describedBy ?? ''} invalid={!!err} />
            {help && <div className="cairn-help" id={helpId}>{help}</div>}
            {err && <div className="cairn-error" id={errId} role="alert">{problemText(t, f.label, err)}</div>}
        </div>
    );
}

export interface FormState {
    values: Values;
    /** Keys only Raw JSON knew, kept in the body. */
    extra: Record<string, unknown>;
    /** The Raw JSON text while that view is open (it is then what is sent). */
    raw: string | null;
}

/** The fields of one action, an Advanced disclosure, and the Raw JSON view of the same body. */
export function FormRenderer({ form, action, state, setState, errors, editing, rowAction, initial, idPrefix = 'cairn-form' }: {
    form: FormSchema;
    action: ResourceAction;
    state: FormState;
    setState: (s: FormState) => void;
    errors: Record<string, Problem | string>;
    editing: boolean;
    rowAction: boolean;
    initial?: Values;
    idPrefix?: string;
}) {
    const t = useT();
    const [advanced, setAdvanced] = useState(false);
    const raw = state.raw;
    const setRaw = (r: string | null) => setState({ ...state, raw: r });
    const [rawError, setRawError] = useState<string | null>(null);
    const connection = typeof state.values.connection === 'string' ? state.values.connection : '';
    const schema = useDheQuery<SchemaTable[]>(
        form.fields.some((f) => f.type === 'sql' || f.item === 'sql') && connection ? `/sql/schema?${connection}` : null,
        (c, signal) => c.request<SchemaTable[]>('GET', '/sql/schema', undefined, { signal, query: { connection } }),
    );
    const ctx: Ctx = {
        action, errors, editing, rowAction, tables: schema.data ?? [], idPrefix,
        firstPlace: /\{(\w+)\}/.exec(action.path)?.[1],
    };
    const basic = form.fields.filter((f) => !f.advanced);
    const more = form.fields.filter((f) => f.advanced);
    const moreHasError = more.some((f) => Object.keys(errors).some((k) => k === f.name || k.startsWith(`${f.name}.`) || k.startsWith(`${f.name}[`)));
    const showMore = advanced || moreHasError;
    const set = (name: string) => (v: unknown) => setState({ ...state, values: { ...state.values, [name]: v } });
    const rawToggleId = `${idPrefix}-raw`;

    const openRaw = () => {
        try {
            setRaw(JSON.stringify(toBody(form, state.values, state.extra, initial), null, 2));
            setRawError(null);
        } catch {
            setRaw(JSON.stringify({ ...state.extra }, null, 2));
            setRawError(t('form.rawJsonFromForm'));
        }
    };
    const closeRaw = () => {
        if (raw === null) return;
        let parsed: unknown;
        try {
            parsed = raw.trim() ? JSON.parse(raw) : {};
        } catch (e) {
            setRawError(t('form.rawJsonInvalid', { error: (e as Error).message })); // presenter-exempt: the JSON the person typed
            return;
        }
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
            setRawError(t('form.rawJsonNotObject'));
            return;
        }
        const back = fromBody(form, parsed, state.values);
        setState({ values: back.values, extra: back.extra, raw: null });
        setRawError(null);
    };

    const fields = { basic, more };
    return (
        <Wrap>
            <div className="cairn-form-bar">
                <span className="cairn-help" style={{ marginTop: 0 }}>{t('form.requiredNote')}</span>
                <span>
                    <label htmlFor={rawToggleId} style={{ marginRight: 6, fontSize: 13 }}>{t('form.rawJson')}</label>
                    <Switch id={rawToggleId} size="small" checked={raw !== null} onChange={(on) => (on ? openRaw() : closeRaw())} />
                </span>
            </div>
            {raw !== null ? (
                <div className="cairn-field">
                    <label htmlFor={`${idPrefix}-raw-body`}>{t('form.rawJsonLabel')}</label>
                    <Input.TextArea id={`${idPrefix}-raw-body`} className="cairn-mono" autoSize={{ minRows: 6, maxRows: 22 }} value={raw}
                        aria-describedby={`${idPrefix}-raw-help`} aria-invalid={!!rawError || undefined}
                        onChange={(e) => setRaw(e.target.value)} />
                    <div className="cairn-help" id={`${idPrefix}-raw-help`}>{t('form.rawJsonHelp')}</div>
                    {rawError && <div className="cairn-error" role="alert">{rawError}</div>}
                </div>
            ) : (
                <>
                    {fields.basic.map((f) => <Field key={f.name} f={f} value={state.values[f.name]} set={set(f.name)} ctx={ctx} k={f.name} />)}
                    {fields.more.length > 0 && (
                        <div style={{ marginTop: 4 }}>
                            <Button type="link" style={{ padding: 0, marginBottom: 8 }} aria-expanded={showMore}
                                aria-controls={`${idPrefix}-advanced`} onClick={() => setAdvanced(!showMore)}>
                                {showMore ? '▾' : '▸'} {t('form.advanced', { n: fields.more.length })}
                            </Button>
                            {showMore && (
                                <div id={`${idPrefix}-advanced`}>
                                    {fields.more.map((f) => <Field key={f.name} f={f} value={state.values[f.name]} set={set(f.name)} ctx={ctx} k={f.name} />)}
                                </div>
                            )}
                        </div>
                    )}
                </>
            )}
        </Wrap>
    );
}
