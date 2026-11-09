import React, { useEffect, useState } from 'react';

import { Icon } from '../components/Icons';
import { CHILD_TYPES, FieldSpec, FlowStep, GROUP_BY, Problem, STEP_TYPES, TYPE_BY, WHEN, WHEN_LABEL } from './model';

interface Props {
    step: FlowStep;
    others: string[];
    problems: Problem[];
    readOnly?: boolean;
    onChange: (next: FlowStep, rename?: string) => void;
    onDelete: () => void;
}

const id = (step: string, field: string) => `fl-${step}-${field}`.replace(/[^A-Za-z0-9_-]/g, '-');

function errorFor(problems: Problem[], field: string): string | undefined {
    return problems.find((p) => p.path.endsWith(`.${field}`))?.message;
}

/** A JSON value edited as text: kept as text while it does not parse, set when it does. */
function JsonField({ label, value, onChange, fid, readOnly }: { label: string; value: unknown; onChange: (v: unknown) => void; fid: string; readOnly?: boolean }) {
    const [text, setText] = useState(() => JSON.stringify(value ?? {}, null, 2));
    const [bad, setBad] = useState<string | null>(null);
    useEffect(() => {
        try {
            if (JSON.stringify(JSON.parse(text)) !== JSON.stringify(value ?? {})) setText(JSON.stringify(value ?? {}, null, 2));
        } catch {
            /* keep the text being typed */
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [value]);
    return (
        <label className="fl-field" htmlFor={fid}>
            <span>{label}</span>
            <textarea
                id={fid}
                rows={4}
                value={text}
                readOnly={readOnly}
                aria-invalid={!!bad}
                onChange={(e) => {
                    setText(e.target.value);
                    try {
                        onChange(JSON.parse(e.target.value || '{}'));
                        setBad(null);
                    } catch (err) {
                        setBad((err as Error).message); // presenter-exempt: the JSON the person typed
                    }
                }}
            />
            {bad && <div className="fl-err">Not JSON yet: {bad}</div>}
        </label>
    );
}

function Field({ spec, step, set, problems, others, readOnly, prefix = '' }: {
    spec: FieldSpec;
    step: Record<string, unknown>;
    set: (k: string, v: unknown) => void;
    problems: Problem[];
    others: string[];
    readOnly?: boolean;
    prefix?: string;
}) {
    const fid = id(String(step.name ?? 'child'), prefix + spec.name);
    const v = step[spec.name];
    const err = errorFor(problems, prefix + spec.name);
    const label = `${spec.label}${spec.required ? ' *' : ''}`;
    const errEl = err ? <div className="fl-err" id={`${fid}-err`}>{err}</div> : null;
    const common = { id: fid, 'aria-invalid': !!err, 'aria-describedby': err ? `${fid}-err` : undefined, readOnly };
    if (spec.kind === 'bool') {
        return (
            <label className="fl-field" htmlFor={fid} style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                <input id={fid} type="checkbox" style={{ width: 'auto' }} checked={v === undefined ? !!spec.default : !!v} disabled={readOnly}
                    onChange={(e) => set(spec.name, e.target.checked)} />
                <span style={{ margin: 0 }}>{spec.label}</span>
            </label>
        );
    }
    if (spec.kind === 'enum') {
        return (
            <label className="fl-field" htmlFor={fid}>
                <span>{label}</span>
                <select {...common} disabled={readOnly} value={String(v ?? spec.default ?? '')} onChange={(e) => set(spec.name, e.target.value)}>
                    {!spec.required && spec.default === undefined && <option value="">(none)</option>}
                    {spec.values!.map((x) => <option key={x} value={x}>{x}</option>)}
                </select>
                {errEl}
            </label>
        );
    }
    if (spec.kind === 'int' || spec.kind === 'num') {
        return (
            <label className="fl-field" htmlFor={fid}>
                <span>{label}</span>
                <input {...common} type="number" min={spec.min} max={spec.max} step={spec.kind === 'int' ? 1 : 'any'}
                    placeholder={spec.default !== undefined ? String(spec.default) : ''}
                    value={v === undefined || v === null ? '' : String(v)}
                    onChange={(e) => set(spec.name, e.target.value === '' ? undefined : Number(e.target.value))} />
                {errEl}
            </label>
        );
    }
    if (spec.kind === 'dict') return <JsonField label={label} value={v ?? spec.default} fid={fid} readOnly={readOnly} onChange={(x) => set(spec.name, x)} />;
    if (spec.kind === 'names') {
        const list = (v as string[] | undefined) ?? [];
        return (
            <fieldset className="fl-field" style={{ border: 0, padding: 0 }}>
                <legend style={{ fontSize: 12, fontWeight: 600 }}>{label}</legend>
                <div className="fl-checks">
                    {others.length === 0 && <span className="fl-muted">No other steps yet.</span>}
                    {others.map((o) => (
                        <label key={o}>
                            <input type="checkbox" disabled={readOnly} checked={list.includes(o)}
                                onChange={(e) => set(spec.name, e.target.checked ? [...list, o] : list.filter((x) => x !== o))} /> {o}
                        </label>
                    ))}
                </div>
                {errEl}
            </fieldset>
        );
    }
    if (spec.kind === 'people' || spec.kind === 'list') {
        const list = (v as unknown[] | undefined) ?? [];
        return (
            <label className="fl-field" htmlFor={fid}>
                <span>{label}</span>
                <input {...common} value={list.join(', ')}
                    placeholder={spec.kind === 'people' ? 'urn:li:corpuser:ana, urn:li:corpGroup:finance' : 'comma-separated'}
                    onChange={(e) => set(spec.name, e.target.value.split(',').map((x) => x.trim()).filter(Boolean))} />
                {errEl}
            </label>
        );
    }
    if (spec.kind === 'child') {
        const child = (v as Record<string, unknown> | undefined) ?? { type: 'notify', message: '{{ item }}' };
        const ct = TYPE_BY[String(child.type)];
        return (
            <fieldset className="fl-card" style={{ margin: '10px 0' }}>
                <legend style={{ fontSize: 12, fontWeight: 600 }}>{label}</legend>
                <label className="fl-field" htmlFor={`${fid}-type`}>
                    <span>Type</span>
                    <select id={`${fid}-type`} disabled={readOnly} value={String(child.type)}
                        onChange={(e) => set(spec.name, { type: e.target.value })}>
                        {CHILD_TYPES.map((t) => <option key={t} value={t}>{TYPE_BY[t].label}</option>)}
                    </select>
                </label>
                {ct?.fields.filter((f) => f.kind !== 'child').map((f) => (
                    <Field key={f.name} spec={f} step={{ ...child, name: `${String(step.name)}-item` }} others={[]} readOnly={readOnly} problems={problems}
                        prefix="step." set={(k, x) => set(spec.name, { ...child, [k]: x })} />
                ))}
                <div className="fl-muted">Use {'{{ item }}'} and {'{{ index }}'} in its fields.</div>
            </fieldset>
        );
    }
    const multiline = spec.kind === 'code' || spec.kind === 'text';
    return (
        <label className="fl-field" htmlFor={fid}>
            <span>{label}</span>
            {multiline ? (
                <textarea {...common} rows={spec.kind === 'code' ? 6 : 3} spellCheck={false} value={v === undefined || v === null ? '' : typeof v === 'string' ? v : JSON.stringify(v)}
                    onChange={(e) => set(spec.name, e.target.value)} />
            ) : (
                <input {...common} value={v === undefined || v === null ? '' : typeof v === 'string' ? v : JSON.stringify(v)}
                    placeholder={spec.help ?? (spec.default !== undefined && spec.default !== '' ? String(spec.default) : '')}
                    style={spec.kind === 'expr' || spec.kind === 'items' ? { fontFamily: 'ui-monospace, monospace' } : undefined}
                    onChange={(e) => {
                        const raw = e.target.value;
                        if (spec.kind === 'items' && raw.trim().startsWith('[')) {
                            try {
                                set(spec.name, JSON.parse(raw));
                                return;
                            } catch {
                                /* keep as an expression while typing */
                            }
                        }
                        set(spec.name, raw === '' ? undefined : raw);
                    }} />
            )}
            {spec.help && <div className="fl-muted">{spec.help}</div>}
            {errEl}
        </label>
    );
}

/** The selected step: its name, type, dependencies, settings and the fields of its type, checked as you type. */
export function StepForm({ step, others, problems, readOnly, onChange, onDelete }: Props) {
    const t = TYPE_BY[step.type];
    const g = GROUP_BY[t?.group ?? 'Control'];
    const [name, setName] = useState(step.name);
    useEffect(() => setName(step.name), [step.name]);
    const set = (k: string, v: unknown) => {
        const next = { ...step };
        if (v === undefined) delete next[k];
        else next[k] = v;
        onChange(next);
    };
    const nameErr = problems.find((p) => p.path.endsWith('.name'))?.message;
    const general = problems.filter((p) => !/\.[A-Za-z_]+$/.test(p.path) || p.path.endsWith('.after'));
    return (
        <section aria-label={`Step ${step.name}`}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
                <span className="fl-chip" style={{ background: g.color }} aria-hidden="true">
                    <Icon name={t?.icon ?? 'puzzle'} size={16} />
                </span>
                <div style={{ flex: 1 }}>
                    <div style={{ fontWeight: 650 }}>{t?.label ?? step.type}</div>
                    <div className="fl-muted">{t?.blurb}</div>
                </div>
                {!readOnly && (
                    <button type="button" className="fl-btn danger" onClick={onDelete} aria-label={`Delete step ${step.name}`}>
                        Delete
                    </button>
                )}
            </div>
            {general.map((p) => (
                <div key={p.path + p.message} className="fl-err" role="alert">{p.message}</div>
            ))}
            <label className="fl-field" htmlFor={id(step.name, 'name')}>
                <span>Name *</span>
                <input
                    id={id(step.name, 'name')}
                    value={name}
                    readOnly={readOnly}
                    aria-invalid={!!nameErr}
                    onChange={(e) => setName(e.target.value)}
                    onBlur={() => name !== step.name && onChange({ ...step, name }, name)}
                    onKeyDown={(e) => e.key === 'Enter' && name !== step.name && onChange({ ...step, name }, name)}
                />
                {nameErr && <div className="fl-err">{nameErr}</div>}
            </label>
            <label className="fl-field" htmlFor={id(step.name, 'type')}>
                <span>Type</span>
                <select id={id(step.name, 'type')} disabled={readOnly} value={step.type}
                    onChange={(e) => onChange({ name: step.name, type: e.target.value, after: step.after })}>
                    {STEP_TYPES.map((x) => <option key={x.type} value={x.type}>{x.group}: {x.label}</option>)}
                </select>
            </label>
            <Field spec={{ name: 'after', label: 'Runs after', kind: 'names' }} step={step} set={set} problems={problems} others={others} readOnly={readOnly} />
            <label className="fl-field" htmlFor={id(step.name, 'when')}>
                <span>Runs</span>
                <select id={id(step.name, 'when')} disabled={readOnly} value={step.when ?? 'all_ok'}
                    onChange={(e) => set('when', e.target.value === 'all_ok' ? undefined : e.target.value)}>
                    {WHEN.map((w) => <option key={w} value={w}>{WHEN_LABEL[w]}</option>)}
                </select>
            </label>
            {t?.fields.map((f) => (
                <Field key={f.name} spec={f} step={step} set={set} problems={problems} others={others} readOnly={readOnly} />
            ))}
            <details>
                <summary style={{ cursor: 'pointer', fontSize: 13, fontWeight: 600, margin: '8px 0' }}>Retries, time limit, lineage</summary>
                <Field spec={{ name: 'retries', label: 'Retries', kind: 'int', default: 0, min: 0, max: 10 }} step={step} set={set} problems={problems} others={others} readOnly={readOnly} />
                <Field spec={{ name: 'retry_delay_s', label: 'First retry after (s)', kind: 'int', default: 30, min: 0, max: 3600 }} step={step} set={set} problems={problems} others={others} readOnly={readOnly} />
                <Field spec={{ name: 'timeout_s', label: 'Time limit (s)', kind: 'int', default: t?.timeout, min: 1, max: 604800 }} step={step} set={set} problems={problems} others={others} readOnly={readOnly} />
                <Field spec={{ name: 'reads', label: 'Reads (tables, for lineage)', kind: 'list', default: [] }} step={step} set={set} problems={problems} others={others} readOnly={readOnly} />
                <Field spec={{ name: 'writes', label: 'Writes (tables, for lineage)', kind: 'list', default: [] }} step={step} set={set} problems={problems} others={others} readOnly={readOnly} />
                <Field spec={{ name: 'description', label: 'Description', kind: 'text', default: '' }} step={step} set={set} problems={problems} others={others} readOnly={readOnly} />
            </details>
        </section>
    );
}
