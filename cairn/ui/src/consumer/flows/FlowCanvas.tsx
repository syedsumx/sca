import React, { forwardRef, useCallback, useEffect, useImperativeHandle, useLayoutEffect, useMemo, useRef, useState } from 'react';

import { Icon } from '../components/Icons';
import { GRID, NODE_H, NODE_W, Pos, bounds, edgePath, snap } from './layout';
import { FlowStep, GROUP_BY, Problem, TYPE_BY } from './model';

export interface Edge {
    from: string;
    to: string;
}

export interface CanvasHandle {
    fit: (positions?: Record<string, Pos>) => void;
    zoom: (factor: number) => void;
}

interface Props {
    steps: FlowStep[];
    pos: Record<string, Pos>;
    selected: string[];
    selectedEdge: Edge | null;
    problems: Map<string, Problem[]>;
    statuses?: Record<string, string>;
    readOnly?: boolean;
    snapOn: boolean;
    onSelect: (names: string[], edge?: Edge | null) => void;
    onMove: (updates: Record<string, Pos>) => void;
    onConnect: (from: string, to: string) => void;
    onDropType: (type: string, at: Pos) => void;
}

interface View {
    x: number;
    y: number;
    k: number;
}

type Drag =
    | { kind: 'pan'; sx: number; sy: number; vx: number; vy: number }
    | { kind: 'band'; x0: number; y0: number; x1: number; y1: number }
    | { kind: 'nodes'; sx: number; sy: number; dx: number; dy: number; moved: boolean }
    | { kind: 'connect'; from: string; x: number; y: number };

const MINI_W = 180;
const MINI_H = 116;

function subtitle(s: FlowStep): string {
    const v = s.table ?? s.app ?? s.dag_id ?? s.pipe ?? s.stream ?? s.job ?? s.application ?? s.condition ?? s.query ?? s.sql ?? s.message ?? s.until;
    if (s.type === 'wait' && s.seconds) return `${s.seconds} s`;
    if (s.type === 'approval') return [...((s.approvers as string[]) ?? []).map((a) => a.split(':').pop()), ...((s.groups as string[]) ?? [])].join(', ');
    if (s.type === 'for_each') return `each of ${typeof s.items === 'string' ? s.items : JSON.stringify(s.items)}`;
    if (s.type === 'http' && typeof s.url === 'string') return `${String(s.method ?? 'GET')} ${s.url.replace(/^https?:\/\//, '')}`;
    if (s.type === 'databricks') return s.job_id ? `job ${String(s.job_id)}` : 'one-time run';
    if (s.type === 'snowflake') return String(s.task ? `task ${String(s.task)}` : s.statement ?? '').replace(/\s+/g, ' ');
    return typeof v === 'string' ? v.replace(/\s+/g, ' ') : TYPE_BY[s.type]?.label ?? s.type;
}

export const FlowCanvas = forwardRef<CanvasHandle, Props>(function FlowCanvas(props, ref) {
    const { steps, pos, selected, selectedEdge, problems, statuses, readOnly, snapOn, onSelect, onMove, onConnect, onDropType } = props;
    const box = useRef<HTMLDivElement>(null);
    const [view, setView] = useState<View>({ x: 40, y: 40, k: 1 });
    const [drag, setDrag] = useState<Drag | null>(null);
    const [size, setSize] = useState({ w: 900, h: 560, measured: false });

    useLayoutEffect(() => {
        const el = box.current;
        if (!el) return undefined;
        const measure = () => el.clientWidth > 0 && setSize({ w: el.clientWidth, h: el.clientHeight || 560, measured: true });
        measure();
        if (typeof ResizeObserver === 'undefined') return undefined;
        const ro = new ResizeObserver(measure);
        ro.observe(el);
        return () => ro.disconnect();
    }, []);

    const fit = useCallback((override?: Record<string, Pos>, minK = 0.25) => {
        const b = bounds(override ?? pos);
        // The first view is readable (minK 0.8: a wide flow starts at its left end); "zoom to fit" shows it all.
        const k = Math.max(minK, Math.min(1.2, Math.min((size.w - 80) / b.w, (size.h - 80) / b.h)));
        const x = b.w * k <= size.w - 80 ? (size.w - b.w * k) / 2 - b.x * k : 40 - b.x * k;
        const y = b.h * k <= size.h - 80 ? (size.h - b.h * k) / 2 - b.y * k : 40 - b.y * k;
        setView({ k, x, y });
    }, [pos, size]);

    const zoom = useCallback((factor: number, cx?: number, cy?: number) => {
        setView((v) => {
            const k = Math.max(0.2, Math.min(2.5, v.k * factor));
            const px = cx ?? size.w / 2;
            const py = cy ?? size.h / 2;
            return { k, x: px - ((px - v.x) * k) / v.k, y: py - ((py - v.y) * k) / v.k };
        });
    }, [size]);

    useImperativeHandle(ref, () => ({ fit: (p?: Record<string, Pos>) => fit(p), zoom: (f: number) => zoom(f) }), [fit, zoom]);

    const fitted = useRef(false);
    useEffect(() => {
        if (!fitted.current && steps.length && (size.measured || typeof ResizeObserver === 'undefined')) {
            fitted.current = true;
            fit(undefined, 0.8);
        }
    }, [steps.length, size, fit]);

    const local = (e: { clientX: number; clientY: number }) => {
        const r = box.current?.getBoundingClientRect();
        return { x: e.clientX - (r?.left ?? 0), y: e.clientY - (r?.top ?? 0) };
    };
    const world = (e: { clientX: number; clientY: number }) => {
        const p = local(e);
        return { x: (p.x - view.x) / view.k, y: (p.y - view.y) / view.k };
    };

    const onBackgroundDown = (e: React.PointerEvent) => {
        if (e.button > 0) return;               // the main button (or a pointer that does not say)
        box.current?.focus();
        if (e.shiftKey && !readOnly) {
            const w = world(e);
            setDrag({ kind: 'band', x0: w.x, y0: w.y, x1: w.x, y1: w.y });
        } else {
            setDrag({ kind: 'pan', sx: e.clientX, sy: e.clientY, vx: view.x, vy: view.y });
            onSelect([], null);
        }
    };

    const onMoveEvt = (e: React.PointerEvent) => {
        if (!drag) return;
        if (drag.kind === 'pan') setView((v) => ({ ...v, x: drag.vx + e.clientX - drag.sx, y: drag.vy + e.clientY - drag.sy }));
        else if (drag.kind === 'band') {
            const w = world(e);
            setDrag({ ...drag, x1: w.x, y1: w.y });
        } else if (drag.kind === 'nodes') {
            const dx = (e.clientX - drag.sx) / view.k;
            const dy = (e.clientY - drag.sy) / view.k;
            setDrag({ ...drag, dx, dy, moved: drag.moved || Math.abs(dx) + Math.abs(dy) > 2 });
        } else if (drag.kind === 'connect') {
            const w = world(e);
            setDrag({ ...drag, x: w.x, y: w.y });
        }
    };

    const moved = (name: string): Pos => {
        const p = pos[name] ?? { x: 0, y: 0 };
        if (drag?.kind === 'nodes' && drag.moved && selected.includes(name)) {
            const x = p.x + drag.dx;
            const y = p.y + drag.dy;
            return snapOn ? { x: snap(x), y: snap(y) } : { x: Math.round(x), y: Math.round(y) };
        }
        return p;
    };

    const onUp = () => {
        if (drag?.kind === 'band') {
            const [x0, x1] = [Math.min(drag.x0, drag.x1), Math.max(drag.x0, drag.x1)];
            const [y0, y1] = [Math.min(drag.y0, drag.y1), Math.max(drag.y0, drag.y1)];
            onSelect(steps.filter((s) => {
                const p = pos[s.name];
                return p && p.x < x1 && p.x + NODE_W > x0 && p.y < y1 && p.y + NODE_H > y0;
            }).map((s) => s.name));
        } else if (drag?.kind === 'nodes' && drag.moved) {
            onMove(Object.fromEntries(selected.map((n) => [n, moved(n)])));
        }
        setDrag(null);
    };

    const onWheel = (e: React.WheelEvent) => {
        const p = local(e);
        zoom(Math.exp(-e.deltaY * 0.0015), p.x, p.y);
    };

    const onNodeDown = (e: React.PointerEvent, name: string) => {
        if (e.button > 0) return;               // the main button (or a pointer that does not say)
        e.stopPropagation();
        const next = e.shiftKey || e.metaKey || e.ctrlKey
            ? (selected.includes(name) ? selected.filter((n) => n !== name) : [...selected, name])
            : selected.includes(name) ? selected : [name];
        onSelect(next, null);
        if (!readOnly) setDrag({ kind: 'nodes', sx: e.clientX, sy: e.clientY, dx: 0, dy: 0, moved: false });
    };

    const onNodeUp = (name: string) => {
        if (drag?.kind === 'connect') {
            if (drag.from !== name) onConnect(drag.from, name);
            setDrag(null);
        }
    };

    const edges = useMemo(() => {
        const known = new Set(steps.map((s) => s.name));
        return steps.flatMap((s) => (s.after ?? []).filter((a) => known.has(a)).map((a) => ({ from: a, to: s.name })));
    }, [steps]);

    const b = bounds(Object.fromEntries(steps.map((s) => [s.name, moved(s.name)])));
    const pad = 200;
    const mk = Math.min(MINI_W / (b.w + pad * 2), MINI_H / (b.h + pad * 2));
    const mini = (x: number, y: number) => ({ x: (x - b.x + pad) * mk, y: (y - b.y + pad) * mk });
    const vpW = (size.w / view.k) * mk;
    const vpH = (size.h / view.k) * mk;
    const vp = mini(-view.x / view.k, -view.y / view.k);

    const onMiniClick = (e: React.MouseEvent<SVGSVGElement>) => {
        const r = e.currentTarget.getBoundingClientRect();
        const wx = (e.clientX - r.left) / mk + b.x - pad;
        const wy = (e.clientY - r.top) / mk + b.y - pad;
        setView((v) => ({ ...v, x: size.w / 2 - wx * v.k, y: size.h / 2 - wy * v.k }));
    };

    const status = (n: string) => statuses?.[n];
    const live = (e: Edge) => ['running', 'retrying', 'waiting'].includes(status(e.to) ?? '');

    return (
        <div
            ref={box}
            className={`fl-viewport${drag?.kind === 'pan' ? ' panning' : ''}`}
            role="application"
            aria-label="Flow canvas: drag to pan, scroll to zoom, shift-drag to select; Delete removes, Ctrl+Z undoes"
            aria-roledescription="flow canvas"
            tabIndex={0}
            style={{ backgroundSize: `${GRID * view.k}px ${GRID * view.k}px`, backgroundPosition: `${view.x}px ${view.y}px` }}
            onPointerDown={onBackgroundDown}
            onPointerMove={onMoveEvt}
            onPointerUp={onUp}
            onPointerLeave={() => drag?.kind !== 'connect' && onUp()}
            onWheel={onWheel}
            onDragOver={(e) => {
                if (!readOnly && Array.from(e.dataTransfer.types).includes('application/x-cairn-step')) e.preventDefault();
            }}
            onDrop={(e) => {
                const type = e.dataTransfer.getData('application/x-cairn-step');
                if (!type || readOnly) return;
                e.preventDefault();
                const w = world(e);
                onDropType(type, { x: snap(w.x - NODE_W / 2), y: snap(w.y - NODE_H / 2) });
            }}
            data-testid="flow-canvas"
        >
            <div className="fl-world" style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.k})` }}>
                <svg className="fl-edges" width={1} height={1} aria-hidden="true">
                    {edges.map((e) => {
                        const d = edgePath(moved(e.from), moved(e.to));
                        const sel = selectedEdge && selectedEdge.from === e.from && selectedEdge.to === e.to;
                        const cls = `fl-edge${sel ? ' selected' : ''}${live(e) ? ' live' : status(e.from) === 'ok' && status(e.to) === 'ok' ? ' ok' : ''}`;
                        return (
                            <g key={`${e.from}->${e.to}`} data-edge={`${e.from}->${e.to}`}>
                                <path className={cls} d={d} markerEnd="url(#fl-arrow)" />
                                <path
                                    className="fl-edge-hit"
                                    d={d}
                                    onPointerDown={(ev) => {
                                        ev.stopPropagation();
                                        onSelect([], e);
                                    }}
                                />
                            </g>
                        );
                    })}
                    {drag?.kind === 'connect' && (
                        <path className="fl-edge temp" d={edgePath(moved(drag.from), { x: drag.x, y: drag.y - NODE_H / 2 })} />
                    )}
                    <defs>
                        <marker id="fl-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                            <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--fl-edge)" />
                        </marker>
                    </defs>
                </svg>
                {steps.map((s) => {
                    const t = TYPE_BY[s.type];
                    const g = GROUP_BY[t?.group ?? 'Control'];
                    const p = moved(s.name);
                    const st = status(s.name);
                    const errs = problems.get(s.name) ?? [];
                    const isSel = selected.includes(s.name);
                    return (
                        <div
                            key={s.name}
                            className={`fl-node${isSel ? ' selected' : ''}${errs.length && !statuses ? ' has-error' : ''}${st ? ` st-${st}` : ''}`}
                            style={{ left: p.x, top: p.y, width: NODE_W, height: NODE_H }}
                            role="button"
                            tabIndex={0}
                            aria-pressed={isSel}
                            aria-label={`Step ${s.name}, ${t?.label ?? s.type}${st ? `, ${st}` : ''}${errs.length ? `, ${errs.length} problem${errs.length > 1 ? 's' : ''}` : ''}`}
                            data-step={s.name}
                            onPointerDown={(e) => onNodeDown(e, s.name)}
                            onPointerUp={() => onNodeUp(s.name)}
                            onKeyDown={(e) => {
                                if (e.key === 'Enter' || e.key === ' ') {
                                    e.preventDefault();
                                    onSelect([s.name], null);
                                }
                            }}
                        >
                            <span className="fl-handle in" aria-hidden="true" />
                            <span className="fl-chip" style={{ background: g.color }} aria-hidden="true">
                                <Icon name={t?.icon ?? 'puzzle'} size={16} />
                            </span>
                            <span style={{ minWidth: 0, flex: 1 }}>
                                <div className="fl-name">{s.name}</div>
                                <div className="fl-sub">{subtitle(s)}</div>
                            </span>
                            {st && <span className="fl-status">{st}</span>}
                            {!st && errs.length > 0 && <span className="fl-status" style={{ color: 'var(--fl-bad)', borderColor: 'var(--fl-bad)' }}>{errs.length} issue{errs.length > 1 ? 's' : ''}</span>}
                            {!readOnly && (
                                <span
                                    className="fl-handle out"
                                    aria-hidden="true"
                                    data-handle={s.name}
                                    onPointerDown={(e) => {
                                        e.stopPropagation();
                                        const w = world(e);
                                        setDrag({ kind: 'connect', from: s.name, x: w.x, y: w.y });
                                    }}
                                />
                            )}
                        </div>
                    );
                })}
                {drag?.kind === 'band' && (
                    <div
                        className="fl-band"
                        style={{
                            left: Math.min(drag.x0, drag.x1),
                            top: Math.min(drag.y0, drag.y1),
                            width: Math.abs(drag.x1 - drag.x0),
                            height: Math.abs(drag.y1 - drag.y0),
                        }}
                    />
                )}
            </div>
            {drag?.kind === 'connect' && <div className="fl-hint">Drop on a step to make it come after {drag.from}</div>}
            {!steps.length && <div className="fl-hint">Drag a step here from the palette, or press Enter on one</div>}
            <div className="fl-controls" onPointerDown={(e) => e.stopPropagation()}>
                <button type="button" className="fl-btn" aria-label="Zoom in" title="Zoom in (+)" onClick={() => zoom(1.2)}>
                    <Icon name="plus" size={16} />
                </button>
                <button type="button" className="fl-btn" aria-label="Zoom out" title="Zoom out (-)" onClick={() => zoom(1 / 1.2)}>
                    <Icon name="minus" size={16} />
                </button>
                <button type="button" className="fl-btn" aria-label="Zoom to fit" title="Zoom to fit (F)" onClick={() => fit()}>
                    <Icon name="fit" size={16} />
                </button>
            </div>
            <svg
                className="fl-minimap"
                width={MINI_W}
                height={MINI_H}
                role="img"
                aria-label="Minimap of the flow"
                onPointerDown={(e) => e.stopPropagation()}
                onClick={onMiniClick}
            >
                {steps.map((s) => {
                    const m = mini(moved(s.name).x, moved(s.name).y);
                    const g = GROUP_BY[TYPE_BY[s.type]?.group ?? 'Control'];
                    return <rect key={s.name} x={m.x} y={m.y} width={NODE_W * mk} height={NODE_H * mk} rx={2} fill={g.color} opacity={0.8} />;
                })}
                <rect className="vp" x={vp.x} y={vp.y} width={vpW} height={vpH} rx={3} />
            </svg>
        </div>
    );
});
