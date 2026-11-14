import React, { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react';

import { CairnApiError } from '../../api/client';
import { Icon } from '../components/Icons';
import type { FlowDetail, FlowRun, FlowRunRow, FlowStepRun, FlowsApi } from './api';
import { CanvasHandle, Edge, FlowCanvas } from './FlowCanvas';
import { GAP_X, GRID, NODE_H, NODE_W, Pos, layered, positions, snap } from './layout';
import {
    FlowDef, FlowStep, FlowTrigger, GROUPS, Problem, STEP_TYPES, TYPE_BY, compact, diff, findCycle, newStep, problemsByStep, validate,
} from './model';
import { StepForm } from './StepForm';
import { parse as parseYaml, stringify } from './yaml';
import { describeError } from '../components/ErrorNotice';
import { Picker } from '../resources/pickers';
import { useT } from '../i18n';
import { StatusBadge } from '../components/StatusBadge';
import { When } from '../utils/time';

// ---- the definition, with undo and redo ------------------------------------------------------------------------
interface History {
    def: FlowDef;
    past: FlowDef[];
    future: FlowDef[];
}

type Act = { t: 'set'; def: FlowDef; record?: boolean } | { t: 'undo' } | { t: 'redo' } | { t: 'reset'; def: FlowDef };

function reduce(h: History, a: Act): History {
    if (a.t === 'reset') return { def: a.def, past: [], future: [] };
    if (a.t === 'undo') return h.past.length ? { def: h.past[h.past.length - 1], past: h.past.slice(0, -1), future: [h.def, ...h.future] } : h;
    if (a.t === 'redo') return h.future.length ? { def: h.future[0], past: [...h.past, h.def], future: h.future.slice(1) } : h;
    if (a.record === false) return { ...h, def: a.def };
    return { def: a.def, past: [...h.past.slice(-99), h.def], future: [] };
}

/** The definition with a place for every step on the canvas. */
export function withLayout(def: FlowDef): FlowDef {
    const pos = positions(def.steps, def.layout);
    return { ...def, layout: Object.fromEntries(def.steps.map((s) => [s.name, pos[s.name]])) };
}

/** Renames a step everywhere: its dependents, branches, templates that read it, and its place. */
export function renameStep(def: FlowDef, from: string, to: string): FlowDef {
    const re = new RegExp(`\\bsteps\\.${from.replace(/[-]/g, '\\-')}\\b(?![-\\w])`, 'g');
    const fix = (v: unknown): unknown => {
        if (typeof v === 'string') return v.replace(re, `steps.${to}`);
        if (Array.isArray(v)) return v.map(fix);
        if (v && typeof v === 'object') return Object.fromEntries(Object.entries(v as object).map(([k, x]) => [k, fix(x)]));
        return v;
    };
    const steps = def.steps.map((s) => {
        const n = (fix(s) as FlowStep);
        n.name = s.name === from ? to : s.name;
        if (n.after) n.after = n.after.map((a) => (a === from ? to : a));
        for (const side of ['then', 'else']) if (Array.isArray(n[side])) n[side] = (n[side] as string[]).map((a) => (a === from ? to : a));
        return n;
    });
    const layout = { ...(def.layout ?? {}) };
    if (layout[from]) {
        layout[to] = layout[from];
        delete layout[from];
    }
    return { ...def, steps, layout };
}

export function removeSteps(def: FlowDef, names: string[]): FlowDef {
    const gone = new Set(names);
    const layout = { ...(def.layout ?? {}) };
    names.forEach((n) => delete layout[n]);
    return {
        ...def,
        layout,
        steps: def.steps.filter((s) => !gone.has(s.name)).map((s) => {
            const n: FlowStep = { ...s, after: (s.after ?? []).filter((a) => !gone.has(a)) };
            for (const side of ['then', 'else']) if (Array.isArray(n[side])) n[side] = (n[side] as string[]).filter((a) => !gone.has(a));
            return n;
        }),
    };
}

/** Adds `from` to `to`'s dependencies; refused (a message) when it would make a cycle. */
export function connect(def: FlowDef, from: string, to: string): FlowDef | string {
    if (from === to) return 'a step cannot come after itself';
    const target = def.steps.find((s) => s.name === to);
    if (!target) return `no step ${to}`;
    if ((target.after ?? []).includes(from)) return `${to} already comes after ${from}`;
    const steps = def.steps.map((s) => (s.name === to ? { ...s, after: [...(s.after ?? []), from] } : s));
    const cycle = findCycle(steps);
    if (cycle) return `that would make a cycle: ${cycle.join(' → ')}`;
    return { ...def, steps };
}

let clipboard: { steps: FlowStep[]; layout: Record<string, Pos> } | null = null;

export function paste(def: FlowDef, clip: { steps: FlowStep[]; layout: Record<string, Pos> }): { def: FlowDef; names: string[] } {
    const taken = new Set(def.steps.map((s) => s.name));
    const rename = new Map<string, string>();
    for (const s of clip.steps) {
        let n = `${s.name}-copy`;
        let i = 2;
        while (taken.has(n)) n = `${s.name}-copy${i++}`;
        taken.add(n);
        rename.set(s.name, n);
    }
    const copies = clip.steps.map((s) => ({
        ...JSON.parse(JSON.stringify(s)),
        name: rename.get(s.name)!,
        after: (s.after ?? []).filter((a) => rename.has(a)).map((a) => rename.get(a)!),
    }));
    const layout = { ...(def.layout ?? {}) };
    for (const s of clip.steps) {
        const p = clip.layout[s.name] ?? { x: 0, y: 0 };
        layout[rename.get(s.name)!] = { x: p.x + 2 * GRID, y: p.y + 2 * GRID + NODE_H };
    }
    return { def: { ...def, steps: [...def.steps, ...copies], layout }, names: copies.map((c) => c.name) };
}

// ---- the editor -------------------------------------------------------------------------------------------------
type Tab = 'canvas' | 'code' | 'settings' | 'runs';

interface Props {
    detail: FlowDetail;
    api: FlowsApi;
    me: string;
    onSaved: (d: FlowDetail) => void;
    onDeleted: () => void;
    theme: string;
    /** Unused since the theme moved to the profile menu (L150); kept for callers. */
    onTheme?: (t: string) => void;
}

const ACTIVE = ['queued', 'running', 'waiting'];
const fmt = (ms: number | null | undefined) => <When value={ms} />;
const took = (a: number | null, b: number | null) => (a && b ? `${((b - a) / 1000).toFixed(1)} s` : '');

function apiProblems(e: unknown): Problem[] {
    if (e instanceof CairnApiError && e.body && typeof e.body === 'object' && Array.isArray((e.body as { errors?: unknown }).errors))
        return (e.body as { errors: Problem[] }).errors;
    return [{ path: '', message: describeError(e) }];
}

export function FlowEditor({ detail, api, me, onSaved, onDeleted, theme }: Props) {
    const [h, dispatch] = useReducer(reduce, undefined, () => ({ def: withLayout(detail.draft), past: [], future: [] }));
    const def = h.def;
    const [saved, setSaved] = useState(() => JSON.stringify(withLayout(detail.draft)));
    const [selected, setSelected] = useState<string[]>([]);
    const [edge, setEdge] = useState<Edge | null>(null);
    const [tab, setTab] = useState<Tab>('canvas');
    const [snapOn, setSnapOn] = useState(true);
    const [notice, setNotice] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);
    const [serverProblems, setServerProblems] = useState<Problem[]>(detail.errors ?? []);
    const [busy, setBusy] = useState(false);
    const [run, setRun] = useState<FlowRun | null>(null);
    const [runs, setRuns] = useState<FlowRunRow[] | null>(null);
    const [modal, setModal] = useState<null | 'publish' | 'run' | 'backfill'>(null);
    const [note, setNote] = useState('');
    const [params, setParams] = useState<Record<string, string>>({});
    const [stepLog, setStepLog] = useState<{ key: string; log: string } | null>(null);
    const canvas = useRef<CanvasHandle>(null);
    const canEdit = detail.role === 'owner' || detail.role === 'admin' || detail.role === 'editor';
    const canPublish = detail.role === 'owner' || detail.role === 'admin';
    const readOnly = !canEdit || !!run;
    const problems = useMemo(() => validate(def), [def]);
    const byStep = useMemo(() => problemsByStep(problems), [problems]);
    const pos = def.layout as Record<string, Pos>;
    const dirty = JSON.stringify(def) !== saved;
    const set = useCallback((next: FlowDef, record = true) => dispatch({ t: 'set', def: next, record }), []);

    useEffect(() => {
        dispatch({ t: 'reset', def: withLayout(detail.draft) });
        setSaved(JSON.stringify(withLayout(detail.draft)));
        setServerProblems(detail.errors ?? []);
        setRun(null);
        setSelected([]);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [detail.id]);

    const flash = (kind: 'ok' | 'err', text: string) => setNotice({ kind, text });

    // ---- steps ---------------------------------------------------------------------------------------------
    const addStep = (type: string, at?: Pos) => {
        const step = newStep(type, def.steps.map((s) => s.name));
        const anchor = selected.length === 1 ? selected[0] : null;
        if (anchor && !at) step.after = [anchor];
        let p = at;
        if (!p) {
            const base = anchor ? pos[anchor] : null;
            const maxX = Math.max(-NODE_W - GAP_X, ...Object.values(pos).map((q) => q.x));
            p = base ? { x: base.x + NODE_W + GAP_X, y: base.y } : { x: snap(maxX + NODE_W + GAP_X), y: 0 };
            while (Object.values(pos).some((q) => q.x === p!.x && q.y === p!.y)) p = { x: p.x, y: p.y + NODE_H + GRID * 2 };
        }
        set({ ...def, steps: [...def.steps, step], layout: { ...pos, [step.name]: p } });
        setSelected([step.name]);
        setEdge(null);
    };

    const updateStep = (orig: string, next: FlowStep, rename?: string) => {
        let d: FlowDef = { ...def, steps: def.steps.map((s) => (s.name === orig ? next : s)) };
        if (rename && rename !== orig) {
            d = renameStep({ ...d, steps: d.steps.map((s) => (s.name === rename ? { ...s, name: orig } : s)) }, orig, rename);
            setSelected([rename]);
        }
        set(d);
    };

    const deleteSelection = () => {
        if (readOnly) return;
        if (edge) {
            set({ ...def, steps: def.steps.map((s) => (s.name === edge.to ? { ...s, after: (s.after ?? []).filter((a) => a !== edge.from) } : s)) });
            setEdge(null);
        } else if (selected.length) {
            set(removeSteps(def, selected));
            setSelected([]);
        }
    };

    const onConnect = (from: string, to: string) => {
        const out = connect(def, from, to);
        if (typeof out === 'string') flash('err', out);
        else {
            set(out);
            flash('ok', `${to} now comes after ${from}`);
        }
    };

    const autoLayout = () => {
        const layout = layered(def.steps);
        set({ ...def, layout });
        canvas.current?.fit(layout);
    };

    // ---- keyboard ------------------------------------------------------------------------------------------
    const onKeyDown = (e: React.KeyboardEvent) => {
        const el = e.target as HTMLElement;
        if (['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName) || tab !== 'canvas') return;
        const mod = e.ctrlKey || e.metaKey;
        const k = e.key.toLowerCase();
        if (mod && k === 'z') {
            e.preventDefault();
            dispatch({ t: e.shiftKey ? 'redo' : 'undo' });
        } else if (mod && k === 'y') {
            e.preventDefault();
            dispatch({ t: 'redo' });
        } else if (mod && k === 'a') {
            e.preventDefault();
            setSelected(def.steps.map((s) => s.name));
        } else if (mod && k === 'c' && selected.length) {
            const steps = def.steps.filter((s) => selected.includes(s.name));
            clipboard = { steps: JSON.parse(JSON.stringify(steps)), layout: Object.fromEntries(steps.map((s) => [s.name, pos[s.name]])) };
            flash('ok', `Copied ${steps.length} step${steps.length > 1 ? 's' : ''}`);
        } else if (mod && k === 'v' && clipboard && !readOnly) {
            e.preventDefault();
            const out = paste(def, clipboard);
            set(out.def);
            setSelected(out.names);
        } else if ((e.key === 'Delete' || e.key === 'Backspace') && !readOnly) {
            e.preventDefault();
            deleteSelection();
        } else if (e.key === 'Escape') {
            setSelected([]);
            setEdge(null);
        } else if (!mod && (k === 'f' || k === '0')) {
            canvas.current?.fit();
        } else if (!mod && (e.key === '+' || e.key === '=')) {
            canvas.current?.zoom(1.2);
        } else if (!mod && e.key === '-') {
            canvas.current?.zoom(1 / 1.2);
        } else if (e.key.startsWith('Arrow') && selected.length && !readOnly) {
            e.preventDefault();
            const step = GRID * (e.shiftKey ? 5 : 1);
            const [dx, dy] = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[e.key] ?? [0, 0];
            const layout = { ...pos };
            selected.forEach((n) => (layout[n] = { x: layout[n].x + dx, y: layout[n].y + dy }));
            set({ ...def, layout });
        }
    };

    // ---- saving, publishing, running ---------------------------------------------------------------------------
    const save = async (): Promise<FlowDetail | null> => {
        setBusy(true);
        try {
            const out = await api.save(detail.name, def);
            setSaved(JSON.stringify(def));
            setServerProblems(out.errors ?? []);
            onSaved(out);
            flash(out.errors?.length ? 'err' : 'ok', out.errors?.length ? `Saved as a draft with ${out.errors.length} problem(s)` : 'Draft saved');
            return out;
        } catch (e) {
            setServerProblems(apiProblems(e));
            flash('err', describeError(e));
            return null;
        } finally {
            setBusy(false);
        }
    };

    const publish = async () => {
        setBusy(true);
        try {
            if (dirty) await api.save(detail.name, def);
            const out = await api.publish(detail.name, note);
            setSaved(JSON.stringify(def));
            setModal(null);
            setNote('');
            flash('ok', `Published version ${out.version}`);
            onSaved(await api.get(detail.name));
        } catch (e) {
            setServerProblems(apiProblems(e));
            flash('err', describeError(e));
        } finally {
            setBusy(false);
        }
    };

    const loadRuns = useCallback(async () => {
        try {
            setRuns(await api.runs(detail.name));
        } catch (e) {
            flash('err', describeError(e));
        }
    }, [api, detail.name]);

    const openRun = async (id: string) => {
        try {
            setRun(await api.runDetail(id));
            setTab('canvas');
            setSelected([]);
        } catch (e) {
            flash('err', describeError(e));
        }
    };

    const startRun = async (draft = false) => {
        setBusy(true);
        try {
            const p: Record<string, unknown> = {};
            for (const [k, v] of Object.entries(params)) if (v !== '') p[k] = v;
            const r = await api.run(detail.name, p, draft);
            setModal(null);
            await openRun(r.id);
        } catch (e) {
            flash('err', describeError(e));
        } finally {
            setBusy(false);
        }
    };

    useEffect(() => {
        if (!run || !ACTIVE.includes(run.status)) return undefined;
        const t = setInterval(async () => {
            try {
                setRun(await api.runDetail(run.id));
            } catch {
                /* the next poll tries again */
            }
        }, 1500);
        return () => clearInterval(t);
    }, [api, run]);

    useEffect(() => {
        if (tab === 'runs') void loadRuns();
    }, [tab, loadRuns]);

    const statuses = useMemo(() => (run ? Object.fromEntries(run.steps.map((s) => [s.key, s.status])) : undefined), [run]);
    const runDef = run?.definition ? withLayout({ ...run.definition, layout: { ...(def.layout ?? {}), ...(run.definition.layout ?? {}) } }) : null;
    const shown = runDef ?? def;
    const sel = selected.length === 1 ? shown.steps.find((s) => s.name === selected[0]) : undefined;
    const selRun = run && sel ? run.steps.find((s) => s.key === sel.name) : undefined;

    // ---- the code view: YAML both ways --------------------------------------------------------------------------
    const [code, setCode] = useState(() => stringify(compact(def)));
    const [codeErr, setCodeErr] = useState<string | null>(null);
    const fromCode = useRef(false);
    useEffect(() => {
        if (fromCode.current) {
            fromCode.current = false;
            return;
        }
        setCode(stringify(compact(def)));
        setCodeErr(null);
    }, [def]);
    const onCode = (text: string) => {
        setCode(text);
        try {
            const raw = parseYaml(text) as FlowDef;
            if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('a flow is a mapping (kind: flow, name, steps)');
            if (raw.name !== detail.name) throw new Error(`name: is ${detail.name} (a flow's name does not change)`);
            if (!Array.isArray(raw.steps)) throw new Error('steps: is a list of steps');
            setCodeErr(null);
            fromCode.current = true;
            set(withLayout({ ...raw, kind: 'flow' }));
        } catch (e) {
            setCodeErr((e as Error).message); // presenter-exempt: the YAML the person typed, checked here
        }
    };

    const allProblems = [...problems, ...serverProblems.filter((p) => !problems.some((q) => q.path === p.path && q.message === p.message))];
    const changes = useMemo(() => diff(detail.published, def), [detail.published, def]);

    return (
        <div onKeyDown={onKeyDown} style={{ display: 'flex', flexDirection: 'column', flex: 1 }}>
            <div className="fl-top">
                <h2 className="fl-title">{detail.name}</h2>
                <span className="fl-muted">
                    {detail.published_version ? `v${detail.published_version} published` : 'not published'}
                    {dirty ? ' · unsaved changes' : ''}
                </span>
                <div className="fl-tabs" role="tablist" aria-label="Flow views">
                    {(['canvas', 'code', 'settings', 'runs'] as Tab[]).map((t) => (
                        <button key={t} type="button" role="tab" id={`fl-tab-${t}`} aria-controls={`fl-panel-${t}`} aria-selected={tab === t} className="fl-tab" onClick={() => setTab(t)}>
                            {{ canvas: 'Canvas', code: 'Code', settings: 'Settings', runs: 'Runs' }[t]}
                        </button>
                    ))}
                </div>
                <span style={{ flex: 1 }} />
                <button type="button" className="fl-btn" aria-label="Undo" title="Undo (Ctrl+Z)" disabled={!h.past.length || readOnly} onClick={() => dispatch({ t: 'undo' })}>
                    <Icon name="undo" size={16} />
                </button>
                <button type="button" className="fl-btn" aria-label="Redo" title="Redo (Ctrl+Shift+Z)" disabled={!h.future.length || readOnly} onClick={() => dispatch({ t: 'redo' })}>
                    <Icon name="redo" size={16} />
                </button>
                <button type="button" className="fl-btn" onClick={autoLayout} disabled={readOnly} title="Arrange the steps in layers">
                    <Icon name="layers" size={16} /> Auto-layout
                </button>
                <label className="fl-muted" style={{ display: 'inline-flex', gap: 4, alignItems: 'center' }}>
                    <input type="checkbox" checked={snapOn} onChange={(e) => setSnapOn(e.target.checked)} /> Snap to grid
                </label>
                {canEdit && (
                    <button type="button" className="fl-btn" disabled={busy || !dirty} onClick={() => void save()}>
                        Save draft
                    </button>
                )}
                {canPublish && (
                    <button type="button" className="fl-btn" disabled={busy || problems.length > 0} title={problems.length ? 'Fix the problems first' : 'Publish a new version'}
                        onClick={() => setModal('publish')}>
                        Publish…
                    </button>
                )}
                {canEdit && (
                    <button type="button" className="fl-btn primary" disabled={busy || (!detail.published_version && !canPublish)}
                        onClick={() => (def.params?.length ? setModal('run') : void startRun(!detail.published_version))}>
                        <Icon name="play" size={14} /> {detail.published_version ? 'Run' : 'Run draft'}
                    </button>
                )}
            </div>
            {notice && (
                <div role={notice.kind === 'err' ? 'alert' : 'status'} className="fl-muted"
                    style={{ padding: '6px 20px', color: notice.kind === 'err' ? 'var(--fl-bad)' : 'var(--fl-good)', background: 'var(--fl-surface)', borderBottom: '1px solid var(--fl-border)' }}>
                    {notice.text}
                    <button type="button" className="fl-btn" style={{ marginLeft: 8, padding: '0 6px' }} aria-label="Dismiss" onClick={() => setNotice(null)}>{'×'}</button>
                </div>
            )}
            {run && (
                <div className="fl-top" style={{ background: 'var(--fl-primary-soft)' }} aria-live="polite">
                    <strong>Run {run.id.slice(0, 8)}</strong>
                    <StatusBadge state={run.status} />
                    <span className="fl-muted">v{run.version || 'draft'} · {run.trigger} · as {run.run_as.split(':').pop()} · started {fmt(run.started_ms ?? run.created_ms)}</span>
                    {run.error && <span className="fl-err">{run.error}</span>}
                    <span style={{ flex: 1 }} />
                    {ACTIVE.includes(run.status) && canEdit && (
                        <button type="button" className="fl-btn danger" onClick={async () => setRun(await api.cancel(run.id))}>Cancel run</button>
                    )}
                    {!ACTIVE.includes(run.status) && canEdit && (
                        <button type="button" className="fl-btn" onClick={async () => openRun((await api.rerun(run.id)).id)}>Rerun failed</button>
                    )}
                    <button type="button" className="fl-btn" onClick={() => setRun(null)}>Back to editing</button>
                </div>
            )}

            {tab === 'canvas' && (
                <div className="fl-body" id="fl-panel-canvas" role="tabpanel" aria-labelledby="fl-tab-canvas">
                    <nav className="fl-palette" aria-label="Step palette">
                        <div className="fl-muted">Drag onto the canvas, or press Enter to add{selected.length === 1 ? ` after ${selected[0]}` : ''}.</div>
                        {GROUPS.map((g) => (
                            <div key={g.name}>
                                <div className="fl-group" style={{ color: g.color }}>{g.name}</div>
                                {STEP_TYPES.filter((t) => t.group === g.name).map((t) => (
                                    <button
                                        key={t.type}
                                        type="button"
                                        className="fl-pal-item"
                                        draggable={!readOnly}
                                        disabled={readOnly}
                                        aria-label={`Add ${t.label} step`}
                                        title={t.blurb}
                                        onDragStart={(e) => e.dataTransfer.setData('application/x-cairn-step', t.type)}
                                        onClick={() => addStep(t.type)}
                                    >
                                        <span className="fl-chip" style={{ background: g.color }} aria-hidden="true">
                                            <Icon name={t.icon} size={15} />
                                        </span>
                                        {t.label}
                                    </button>
                                ))}
                            </div>
                        ))}
                    </nav>
                    <FlowCanvas
                        ref={canvas}
                        steps={shown.steps}
                        pos={(shown.layout ?? {}) as Record<string, Pos>}
                        selected={selected}
                        selectedEdge={edge}
                        problems={byStep}
                        statuses={statuses}
                        readOnly={readOnly}
                        snapOn={snapOn}
                        onSelect={(names, e) => {
                            setSelected(names);
                            setEdge(e ?? null);
                            setStepLog(null);
                        }}
                        onMove={(updates) => set({ ...def, layout: { ...pos, ...updates } })}
                        onConnect={onConnect}
                        onDropType={(type, at) => addStep(type, at)}
                    />
                    <aside className="fl-side" aria-label={sel ? `Step ${sel.name}` : 'Steps'}>
                        {run && sel && selRun ? (
                            <StepRunPanel
                                step={selRun}
                                run={run}
                                me={me}
                                canEdit={canEdit}
                                log={stepLog?.key === selRun.key ? stepLog.log : null}
                                onLog={async () => setStepLog({ key: selRun.key, log: (await api.stepLog(run.id, selRun.key)).log || '(no log)' })}
                                onRerun={async () => openRun((await api.rerun(run.id, selRun.key)).id)}
                                onDecide={async (d, comment) => {
                                    try {
                                        setRun(await api.decide(run.id, selRun.key, d, comment));
                                        flash('ok', d === 'approve' ? 'Approved' : 'Rejected');
                                    } catch (e) {
                                        flash('err', describeError(e));
                                    }
                                }}
                            />
                        ) : sel ? (
                            <StepForm
                                key={sel.name}
                                step={sel}
                                others={def.steps.map((s) => s.name).filter((n) => n !== sel.name)}
                                problems={byStep.get(sel.name) ?? []}
                                readOnly={readOnly}
                                onChange={(next, rename) => updateStep(sel.name, next, rename)}
                                onDelete={() => {
                                    set(removeSteps(def, [sel.name]));
                                    setSelected([]);
                                }}
                            />
                        ) : (
                            <StepList def={shown} problems={allProblems} run={run} onPick={(n) => setSelected([n])} edge={edge} onDeleteEdge={deleteSelection} readOnly={readOnly}
                                onChange={(d) => set({ ...def, runner: d.runner })} />
                        )}
                    </aside>
                </div>
            )}

            {tab === 'code' && (
                <div className="fl-code-wrap" id="fl-panel-code" role="tabpanel" aria-labelledby="fl-tab-code">
                    <label htmlFor="fl-yaml" className="fl-sr">Flow definition (YAML)</label>
                    <textarea id="fl-yaml" className="fl-code" spellCheck={false} value={code} readOnly={readOnly}
                        aria-invalid={!!codeErr} aria-describedby="fl-yaml-problems" onChange={(e) => onCode(e.target.value)} />
                    <div id="fl-yaml-problems" className="fl-card" aria-live="polite">
                        <strong>YAML</strong>
                        <p className="fl-muted">The same definition as the canvas: edit either; positions are kept under <code>layout</code>.</p>
                        {codeErr && <div className="fl-err" role="alert">{codeErr}</div>}
                        {!codeErr && <ProblemList problems={allProblems} />}
                    </div>
                </div>
            )}

            {tab === 'settings' && (
                <div className="fl-pane" id="fl-panel-settings" role="tabpanel" aria-labelledby="fl-tab-settings">
                    <Settings def={def} readOnly={readOnly} onChange={(d) => set(d)} problems={allProblems} />
                    {canPublish && (
                        <div className="fl-card" style={{ marginTop: 16 }}>
                            <strong>Danger zone</strong>
                            <p className="fl-muted">Deleting a flow deletes its versions and its runs.</p>
                            <button type="button" className="fl-btn danger" onClick={async () => {
                                if (typeof window !== 'undefined' && window.confirm && !window.confirm(`Delete ${detail.name}?`)) return;
                                await api.remove(detail.name);
                                onDeleted();
                            }}>Delete flow</button>
                            <button type="button" className="fl-btn" style={{ marginLeft: 8 }} onClick={async () => onSaved(await api.setEnabled(detail.name, !detail.enabled))}>
                                {detail.enabled ? 'Turn off (no scheduled or triggered runs)' : 'Turn on'}
                            </button>
                        </div>
                    )}
                </div>
            )}

            {tab === 'runs' && (
                <div className="fl-pane" id="fl-panel-runs" role="tabpanel" aria-labelledby="fl-tab-runs">
                    <div style={{ display: 'flex', gap: 8, marginBottom: 10 }}>
                        <button type="button" className="fl-btn" onClick={() => void loadRuns()}>Refresh</button>
                        {canEdit && detail.published_version && <button type="button" className="fl-btn" onClick={() => setModal('backfill')}>Backfill…</button>}
                    </div>
                    {!runs ? <p className="fl-muted">Loading runs…</p> : runs.length === 0 ? <p className="fl-muted">No runs yet.</p> : (
                        <table className="fl-table">
                            <caption className="fl-sr">Run history</caption>
                            <thead>
                                <tr><th>Run</th><th>Status</th><th>Trigger</th><th>Version</th><th>Started</th><th>Took</th><th>Date</th></tr>
                            </thead>
                            <tbody>
                                {runs.map((r) => (
                                    <tr key={r.id}>
                                        <td><button type="button" className="fl-list-item" style={{ padding: 0 }} onClick={() => void openRun(r.id)}>{r.id.slice(0, 8)}</button></td>
                                        <td><StatusBadge state={r.status} /></td>
                                        <td>{r.trigger}{r.triggered_by ? ` (${r.triggered_by.split(':').pop()})` : ''}</td>
                                        <td>{r.version || 'draft'}</td>
                                        <td>{fmt(r.started_ms ?? r.created_ms)}</td>
                                        <td>{took(r.started_ms, r.finished_ms)}</td>
                                        <td>{r.logical_date}</td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    )}
                </div>
            )}

            {modal === 'publish' && (
                <Modal title={`Publish ${detail.name}`} onClose={() => setModal(null)}>
                    <p className="fl-muted">Version {(detail.published_version ?? 0) + 1}. Runs from now on use it; runs going on keep theirs.</p>
                    <DiffView changes={changes} />
                    <label className="fl-field" htmlFor="fl-note">
                        <span>Note</span>
                        <input id="fl-note" value={note} onChange={(e) => setNote(e.target.value)} placeholder="What changed" />
                    </label>
                    <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
                        <button type="button" className="fl-btn" onClick={() => setModal(null)}>Cancel</button>
                        <button type="button" className="fl-btn primary" disabled={busy} onClick={() => void publish()}>Publish</button>
                    </div>
                </Modal>
            )}
            {modal === 'run' && (
                <Modal title={`Run ${detail.name}`} onClose={() => setModal(null)}>
                    {(def.params ?? []).map((p) => (
                        <label key={p.name} className="fl-field" htmlFor={`fl-p-${p.name}`}>
                            <span>{p.name} ({p.type ?? 'string'}){p.required ? ' *' : ''}</span>
                            <input id={`fl-p-${p.name}`} placeholder={p.default !== undefined && p.default !== null ? String(p.default) : ''}
                                value={params[p.name] ?? ''} onChange={(e) => setParams({ ...params, [p.name]: e.target.value })} />
                        </label>
                    ))}
                    <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
                        <button type="button" className="fl-btn" onClick={() => setModal(null)}>Cancel</button>
                        <button type="button" className="fl-btn primary" disabled={busy} onClick={() => void startRun(!detail.published_version)}>Start run</button>
                    </div>
                </Modal>
            )}
            {modal === 'backfill' && <Backfill name={detail.name} params={(def.params ?? []).filter((p) => p.type === 'date').map((p) => p.name)} api={api}
                onClose={() => setModal(null)} onDone={(n) => { setModal(null); flash('ok', `${n} runs queued`); void loadRuns(); }} />}
        </div>
    );
}

function ProblemList({ problems }: { problems: Problem[] }) {
    if (!problems.length) return <p style={{ color: 'var(--fl-good)' }}>No problems.</p>;
    return (
        <ul className="fl-list" aria-label="Problems">
            {problems.map((p) => (
                <li key={p.path + p.message} className="fl-err">
                    <code>{p.path || 'flow'}</code>: {p.message}
                </li>
            ))}
        </ul>
    );
}

/** Where the flow runs: in Cairn, or in Airflow from the DAG Cairn deploys (`runner` in the definition). */
function Runner({ def, readOnly, onChange }: { def: FlowDef; readOnly: boolean; onChange: (d: FlowDef) => void }) {
    const t = useT();
    const current = def.runner ?? 'cairn';
    return (
        <fieldset className="fl-runner">
            <legend>{t('flows.runsOn')}</legend>
            {(['cairn', 'airflow'] as const).map((r) => (
                <label key={r} className={current === r ? 'on' : undefined}>
                    <input type="radio" name="fl-runner" value={r} checked={current === r} disabled={readOnly}
                        onChange={() => onChange({ ...def, runner: r === 'cairn' ? undefined : r })} />
                    <span><strong>{t(r === 'cairn' ? 'flows.runner.cairn' : 'flows.runner.airflow')}</strong>
                        <span>{t(r === 'cairn' ? 'flows.runner.cairnHint' : 'flows.runner.airflowHint')}</span></span>
                </label>
            ))}
        </fieldset>
    );
}

function StepList({ def, problems, run, onPick, edge, onDeleteEdge, readOnly, onChange }: {
    def: FlowDef; problems: Problem[]; run: FlowRun | null; onPick: (n: string) => void; edge: Edge | null; onDeleteEdge: () => void; readOnly: boolean;
    onChange: (d: FlowDef) => void;
}) {
    const st = run ? Object.fromEntries(run.steps.map((s) => [s.key, s.status])) : {};
    return (
        <div>
            {edge && (
                <div className="fl-card" style={{ marginBottom: 12 }}>
                    <div><strong>{edge.to}</strong> comes after <strong>{edge.from}</strong></div>
                    {!readOnly && <button type="button" className="fl-btn danger" style={{ marginTop: 8 }} onClick={onDeleteEdge}>Remove this link</button>}
                </div>
            )}
            <Runner def={def} readOnly={readOnly} onChange={onChange} />
            <h3 style={{ fontSize: 14, margin: '0 0 6px' }}>Steps</h3>
            <ul className="fl-list" aria-label="Steps in the flow">
                {def.steps.map((s) => (
                    <li key={s.name}>
                        <button type="button" className="fl-list-item" onClick={() => onPick(s.name)}>
                            <span className="fl-chip" style={{ background: GROUPS.find((g) => g.name === TYPE_BY[s.type]?.group)?.color ?? '#888', width: 18, height: 18 }} aria-hidden="true" />
                            <span style={{ flex: 1 }}>{s.name}</span>
                            {(s.after ?? []).length > 0 && <span className="fl-muted">after {(s.after ?? []).join(', ')}</span>}
                            {st[s.name] && <StatusBadge state={st[s.name]} />}
                        </button>
                    </li>
                ))}
            </ul>
            <h3 style={{ fontSize: 14, margin: '14px 0 6px' }}>Problems</h3>
            <ProblemList problems={problems} />
            <p className="fl-muted" style={{ marginTop: 14 }}>
                Keys: Delete removes, Ctrl+Z / Ctrl+Shift+Z undo and redo, Ctrl+C / Ctrl+V copy and paste, arrows move, F fits. Link steps by dragging
                from a step&apos;s right handle onto another, or with &ldquo;Runs after&rdquo; in its form.
            </p>
        </div>
    );
}

function StepRunPanel({ step, run, me, canEdit, log, onLog, onRerun, onDecide }: {
    step: FlowStepRun; run: FlowRun; me: string; canEdit: boolean; log: string | null; onLog: () => void; onRerun: () => void;
    onDecide: (d: 'approve' | 'reject', comment: string) => void;
}) {
    const [comment, setComment] = useState('');
    const waitingApproval = step.type === 'approval' && ['waiting', 'running'].includes(step.status) && !step.decision;
    const mayDecide = waitingApproval && ((step.approvers ?? []).includes(me) || (step.groups ?? []).length > 0);
    return (
        <section aria-label={`Run of step ${step.key}`}>
            <h3 style={{ fontSize: 15, margin: '0 0 6px' }}>{step.key} <StatusBadge state={step.status} /></h3>
            <div className="fl-muted">
                Attempt {step.attempt || 0} · {took(step.started_ms, step.finished_ms) || fmt(step.started_ms)}
                {step.reused ? ' · kept from the run it reran' : ''}
            </div>
            {step.error && <div className="fl-err" role="alert" style={{ margin: '8px 0' }}>{step.error}</div>}
            {mayDecide && (
                <div className="fl-card" style={{ margin: '10px 0' }}>
                    <strong>Waiting for approval</strong>
                    <label className="fl-field" htmlFor="fl-decide-comment">
                        <span>Comment</span>
                        <input id="fl-decide-comment" value={comment} onChange={(e) => setComment(e.target.value)} />
                    </label>
                    <div style={{ display: 'flex', gap: 8 }}>
                        <button type="button" className="fl-btn primary" onClick={() => onDecide('approve', comment)}>Approve</button>
                        <button type="button" className="fl-btn danger" onClick={() => onDecide('reject', comment)}>Reject</button>
                    </div>
                </div>
            )}
            {waitingApproval && !mayDecide && <p className="fl-muted">Waiting for {(step.approvers ?? []).join(', ')}.</p>}
            {step.decision && <p className="fl-muted">{step.decision.decision === 'approve' ? 'Approved' : 'Rejected'} by {step.decision.by}{step.decision.comment ? `: ${step.decision.comment}` : ''}</p>}
            {step.items && (
                <>
                    <h4 style={{ fontSize: 13 }}>Items</h4>
                    <ul className="fl-list">
                        {step.items.map((i) => <li key={i.key}><StatusBadge state={i.status} /> {i.key}{i.error ? ` — ${i.error}` : ''}</li>)}
                    </ul>
                </>
            )}
            {step.output && (
                <>
                    <h4 style={{ fontSize: 13 }}>Output</h4>
                    <pre className="fl-pre">{JSON.stringify(step.output, null, 2)}</pre>
                </>
            )}
            <div style={{ display: 'flex', gap: 8, marginTop: 8, flexWrap: 'wrap' }}>
                <button type="button" className="fl-btn" onClick={onLog}>Show log</button>
                {canEdit && !ACTIVE.includes(run.status) && <button type="button" className="fl-btn" onClick={onRerun}>Rerun from here</button>}
            </div>
            {log !== null && <pre className="fl-pre" aria-label="Step log">{log}</pre>}
        </section>
    );
}

export function DiffView({ changes }: { changes: ReturnType<typeof diff> }) {
    const none = !changes.added.length && !changes.removed.length && !changes.changed.length && !changes.settings.length;
    if (none) return <p className="fl-muted">No changes from the published version (positions on the canvas aside).</p>;
    return (
        <ul className="fl-list" aria-label="Changes from the published version">
            {changes.added.map((n) => <li key={`a${n}`} className="fl-diff-add">+ step {n} added</li>)}
            {changes.removed.map((n) => <li key={`r${n}`} className="fl-diff-del">− step {n} removed</li>)}
            {changes.changed.map((c) => <li key={`c${c.name}`} className="fl-diff-chg">~ step {c.name}: {c.fields.join(', ')}</li>)}
            {changes.settings.map((s) => <li key={`s${s}`} className="fl-diff-chg">~ {s}</li>)}
        </ul>
    );
}

function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
    const ref = useRef<HTMLDivElement>(null);
    useEffect(() => {
        ref.current?.querySelector<HTMLElement>('input, button, select, textarea')?.focus();
    }, []);
    return (
        <div style={{ position: 'fixed', inset: 0, background: 'rgba(8,10,20,.45)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000 }}
            onKeyDown={(e) => e.key === 'Escape' && onClose()}>
            <div ref={ref} role="dialog" aria-modal="true" aria-label={title} className="fl-card" style={{ width: 520, maxWidth: '92vw', maxHeight: '86vh', overflow: 'auto' }}>
                <h2 style={{ fontSize: 17, marginTop: 0 }}>{title}</h2>
                {children}
            </div>
        </div>
    );
}

function Backfill({ name, params, api, onClose, onDone }: { name: string; params: string[]; api: FlowsApi; onClose: () => void; onDone: (n: number) => void }) {
    const [start, setStart] = useState('');
    const [end, setEnd] = useState('');
    const [param, setParam] = useState(params[0] ?? '');
    const [err, setErr] = useState<string | null>(null);
    return (
        <Modal title={`Backfill ${name}`} onClose={onClose}>
            <p className="fl-muted">One run per day, each with its logical date{params.length ? ' (and the parameter you choose)' : ''}.</p>
            <label className="fl-field" htmlFor="fl-bf-start"><span>From</span><input id="fl-bf-start" type="date" value={start} onChange={(e) => setStart(e.target.value)} /></label>
            <label className="fl-field" htmlFor="fl-bf-end"><span>To</span><input id="fl-bf-end" type="date" value={end} onChange={(e) => setEnd(e.target.value)} /></label>
            {params.length > 0 && (
                <label className="fl-field" htmlFor="fl-bf-param"><span>Date parameter</span>
                    <select id="fl-bf-param" value={param} onChange={(e) => setParam(e.target.value)}>
                        <option value="">(none)</option>
                        {params.map((p) => <option key={p} value={p}>{p}</option>)}
                    </select>
                </label>
            )}
            {err && <div className="fl-err" role="alert">{err}</div>}
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
                <button type="button" className="fl-btn" onClick={onClose}>Cancel</button>
                <button type="button" className="fl-btn primary" onClick={async () => {
                    try {
                        onDone((await api.backfill(name, start, end, param || undefined)).runs.length);
                    } catch (e) {
                        setErr(describeError(e, { action: 'Queue runs' }));
                    }
                }}>Queue runs</button>
            </div>
        </Modal>
    );
}

function Settings({ def, readOnly, onChange, problems }: { def: FlowDef; readOnly: boolean; onChange: (d: FlowDef) => void; problems: Problem[] }) {
    const triggers = def.triggers ?? [];
    const setTriggers = (t: FlowTrigger[]) => onChange({ ...def, triggers: t });
    const sched = triggers.find((t) => t.type === 'schedule');
    const dataset = triggers.find((t) => t.type === 'dataset');
    const after = triggers.find((t) => t.type === 'flow');
    const put = (type: string, t: FlowTrigger | null) => setTriggers([...triggers.filter((x) => x.type !== type), ...(t ? [t] : [])]);
    const err = (path: string) => problems.find((p) => p.path.startsWith(path))?.message;
    return (
        <div style={{ display: 'grid', gap: 16, gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))' }}>
            <section className="fl-card" aria-label="About">
                <strong>About</strong>
                <label className="fl-field" htmlFor="fl-desc"><span>Description</span>
                    <textarea id="fl-desc" rows={3} readOnly={readOnly} value={def.description ?? ''} onChange={(e) => onChange({ ...def, description: e.target.value || undefined })} />
                </label>
                <label className="fl-field" htmlFor="fl-sla"><span>SLA (minutes; tell when a run takes longer)</span>
                    <input id="fl-sla" type="number" min={1} readOnly={readOnly} value={def.sla_minutes ?? ''}
                        onChange={(e) => onChange({ ...def, sla_minutes: e.target.value ? Number(e.target.value) : undefined })} />
                </label>
                <label className="fl-field" htmlFor="fl-conc"><span>When a run is due while one is going</span>
                    <select id="fl-conc" disabled={readOnly} value={def.concurrency?.policy ?? 'queue'}
                        onChange={(e) => onChange({ ...def, concurrency: { ...def.concurrency, policy: e.target.value } })}>
                        <option value="queue">Queue it</option>
                        <option value="skip">Skip it</option>
                        <option value="allow">Run it too (up to the limit)</option>
                    </select>
                </label>
                <label className="fl-field" htmlFor="fl-max"><span>Runs at once</span>
                    <input id="fl-max" type="number" min={1} max={50} readOnly={readOnly} value={def.concurrency?.max_active ?? ''}
                        onChange={(e) => onChange({ ...def, concurrency: { ...def.concurrency, max_active: e.target.value ? Number(e.target.value) : undefined } })} />
                </label>
            </section>
            <section className="fl-card" aria-label="Triggers">
                <strong>Triggers</strong>
                <p className="fl-muted">Manual and API runs are always possible.</p>
                <label className="fl-field" htmlFor="fl-cron"><span>Schedule (cron)</span>
                    <input id="fl-cron" readOnly={readOnly} placeholder="0 2 * * *" value={sched?.cron ?? ''} aria-invalid={!!err('triggers') && !!sched}
                        onChange={(e) => put('schedule', e.target.value ? { ...(sched ?? { type: 'schedule', timezone: 'UTC' }), cron: e.target.value } : null)} />
                </label>
                <label className="fl-field" htmlFor="fl-tz"><span>Time zone</span>
                    <input id="fl-tz" readOnly={readOnly || !sched} value={sched?.timezone ?? 'UTC'}
                        onChange={(e) => sched && put('schedule', { ...sched, timezone: e.target.value })} />
                </label>
                <label className="fl-field" htmlFor="fl-tables"><span>When these tables change</span>
                    <input id="fl-tables" readOnly={readOnly} placeholder="sales.orders, raw.events" value={(dataset?.tables ?? []).join(', ')}
                        onChange={(e) => {
                            const tables = e.target.value.split(',').map((x) => x.trim()).filter(Boolean);
                            put('dataset', tables.length ? { type: 'dataset', tables } : null);
                        }} />
                </label>
                <label className="fl-field" htmlFor="fl-after"><span>After another flow succeeds</span>
                    <Picker id="fl-after" disabled={readOnly} placeholder="ingest-orders" value={after?.flow ?? ''}
                        field={{ name: 'flow', label: 'After another flow succeeds', type: 'picker', picker: 'flow' }}
                        onChange={(v) => put('flow', v ? { type: 'flow', flow: String(v), on: after?.on ?? ['succeeded'] } : null)} />
                </label>
                {err('triggers') && <div className="fl-err">{err('triggers')}</div>}
            </section>
            <section className="fl-card" aria-label="Notifications">
                <strong>Notifications</strong>
                <div className="fl-checks" style={{ margin: '8px 0' }}>
                    {['failure', 'success', 'sla'].map((o) => (
                        <label key={o}>
                            <input type="checkbox" disabled={readOnly} checked={(def.notify?.on ?? ['failure', 'sla']).includes(o)}
                                onChange={(e) => {
                                    const on = def.notify?.on ?? ['failure', 'sla'];
                                    onChange({ ...def, notify: { ...def.notify, on: e.target.checked ? [...on, o] : on.filter((x) => x !== o) } });
                                }} /> on {o}
                        </label>
                    ))}
                </div>
                <label className="fl-field" htmlFor="fl-notify-to"><span>Also tell (the owner always hears)</span>
                    <input id="fl-notify-to" readOnly={readOnly} placeholder="urn:li:corpuser:ana" value={(def.notify?.to ?? []).join(', ')}
                        onChange={(e) => onChange({ ...def, notify: { ...def.notify, to: e.target.value.split(',').map((x) => x.trim()).filter(Boolean) } })} />
                </label>
            </section>
            <section className="fl-card" aria-label="Parameters">
                <strong>Parameters</strong>
                <table className="fl-table">
                    <caption className="fl-sr">Parameters</caption>
                    <thead><tr><th>Name</th><th>Type</th><th>Default</th><th><span className="fl-sr">Remove</span></th></tr></thead>
                    <tbody>
                        {(def.params ?? []).map((p, i) => (
                            <tr key={i}>
                                <td><input aria-label={`Parameter ${i + 1} name`} readOnly={readOnly} value={p.name}
                                    onChange={(e) => onChange({ ...def, params: (def.params ?? []).map((q, j) => (j === i ? { ...q, name: e.target.value } : q)) })} /></td>
                                <td><select aria-label={`Parameter ${i + 1} type`} disabled={readOnly} value={p.type ?? 'string'}
                                    onChange={(e) => onChange({ ...def, params: (def.params ?? []).map((q, j) => (j === i ? { ...q, type: e.target.value } : q)) })}>
                                    {['string', 'int', 'float', 'bool', 'date', 'list'].map((t) => <option key={t}>{t}</option>)}
                                </select></td>
                                <td><input aria-label={`Parameter ${i + 1} default`} readOnly={readOnly} value={p.default === undefined || p.default === null ? '' : String(p.default)}
                                    onChange={(e) => onChange({ ...def, params: (def.params ?? []).map((q, j) => (j === i ? { ...q, default: e.target.value || undefined } : q)) })} /></td>
                                <td>{!readOnly && <button type="button" className="fl-btn" aria-label={`Remove parameter ${p.name}`}
                                    onClick={() => onChange({ ...def, params: (def.params ?? []).filter((_, j) => j !== i) })}>{'×'}</button>}</td>
                            </tr>
                        ))}
                    </tbody>
                </table>
                {!readOnly && <button type="button" className="fl-btn" style={{ marginTop: 8 }}
                    onClick={() => onChange({ ...def, params: [...(def.params ?? []), { name: `param${(def.params ?? []).length + 1}`, type: 'string' }] })}>Add parameter</button>}
                <p className="fl-muted">Defaults may use {'{{ run.logical_date }}'}; steps read {'{{ params.name }}'}.</p>
            </section>
        </div>
    );
}
