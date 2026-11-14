import React, { useEffect, useMemo, useRef, useState } from 'react';
import styled from 'styled-components';

import { tokens } from '../components/tokens';
import { TYPE_LABEL } from './api';
import { GEdge, GNode, Graph, Point, View, centre, fit, groupColour, showLabel, zoomAt } from './graph';

const W = 960;
const H = 620;
const MINI = 150;

const Frame = styled.div`
    position: relative;
    border: 1px solid ${tokens.color.border};
    border-radius: ${tokens.radius.md};
    background: ${tokens.color.surface};
    overflow: hidden;
    svg.main {
        display: block;
        width: 100%;
        height: auto;
        cursor: grab;
        touch-action: none;
    }
    svg.main.dragging {
        cursor: grabbing;
    }
    svg.main:focus-visible {
        outline: 2px solid ${tokens.color.focus};
        outline-offset: -2px;
    }
    text {
        font-size: 12px;
        fill: ${tokens.color.text};
        pointer-events: none;
        paint-order: stroke;
        stroke: #fff;
        stroke-width: 3px;
    }
    .edge-label {
        font-size: 10px;
        fill: ${tokens.color.textMuted};
    }
    .node {
        cursor: pointer;
    }
    .node:focus-visible {
        outline: none;
    }
    .node:focus-visible .shape {
        stroke: ${tokens.color.focus};
        stroke-width: 4px;
    }
`;

const Controls = styled.div`
    position: absolute;
    top: 8px;
    left: 8px;
    display: flex;
    gap: 4px;
    button {
        min-width: 30px;
        height: 30px;
        border: 1px solid ${tokens.color.border};
        border-radius: 6px;
        background: ${tokens.color.surface};
        color: ${tokens.color.text};
        cursor: pointer;
    }
    button:focus-visible {
        outline: 2px solid ${tokens.color.focus};
    }
`;

const Mini = styled.svg`
    position: absolute;
    right: 8px;
    bottom: 8px;
    width: ${MINI}px;
    height: ${MINI}px;
    border: 1px solid ${tokens.color.border};
    border-radius: 8px;
    background: rgba(255, 255, 255, 0.92);
    cursor: pointer;
`;

const Legend = styled.div`
    position: absolute;
    left: 8px;
    bottom: 8px;
    background: rgba(255, 255, 255, 0.92);
    border: 1px solid ${tokens.color.border};
    border-radius: 8px;
    padding: 4px 8px;
    font-size: 11px;
    color: ${tokens.color.text};
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
    max-width: 70%;
`;

const DASH: Record<GEdge['type'], string | undefined> = { key: undefined, queries: '6 4', inferred: '2 3', bridge: '8 3 2 3', aggregate: undefined };

function nodeRadius(n: GNode): number {
    if (n.kind === 'cluster') return 14 + Math.min(30, 4 * Math.sqrt(n.size));
    return n.kind === 'term' ? 10 : 8;
}

function Shape({ n, fill, stroke, selected }: { n: GNode; fill: string; stroke: string; selected: boolean }) {
    const r = nodeRadius(n);
    const sw = selected ? 4 : n.status === 'proposed' ? 2 : 1.5;
    const dash = n.status === 'proposed' ? '3 2' : undefined;
    if (n.kind === 'bridge') {
        return <polygon className="shape" points={`0,${-r - 3} ${r + 3},0 0,${r + 3} ${-r - 3},0`} fill="#fff7ed" stroke="#c2410c" strokeWidth={sw} />;
    }
    if (n.kind === 'table') return <rect className="shape" x={-r} y={-r} width={2 * r} height={2 * r} rx={2} fill="#f1f5f9" stroke="#64748b" strokeWidth={sw} />;
    return <circle className="shape" r={r} fill={n.status === 'accepted' || n.kind === 'cluster' ? fill : '#fff'} stroke={stroke} strokeWidth={sw} strokeDasharray={dash} />;
}

export interface CanvasProps {
    graph: Graph;
    positions: Map<string, Point>;
    groups: string[];
    selected: string | null;
    highlight: Set<string> | null;
    /** Changing the nonce centres the view on `id` (search → zoom to it). */
    focus: { id: string; nonce: number } | null;
    onSelect: (n: GNode) => void;
}

/** The ontology as an SVG graph: pan (drag, arrow keys), zoom (wheel, + and -), fit, a minimap, edge labels with the
 * relationship's columns and cardinality, bridge tables as diamonds, proposed terms with a dashed ring. */
export function OntologyCanvas({ graph, positions, groups, selected, highlight, focus, onSelect }: CanvasProps) {
    const svgRef = useRef<SVGSVGElement | null>(null);
    const [view, setView] = useState<View>(() => fit([...positions.values()], W, H));
    const drag = useRef<{ x: number; y: number; vx: number; vy: number; moved: boolean } | null>(null);
    const [dragging, setDragging] = useState(false);

    useEffect(() => setView(fit([...positions.values()], W, H)), [positions]);
    useEffect(() => {
        if (!focus) return;
        const p = positions.get(focus.id);
        if (p) setView((v) => centre(p, W, H, Math.max(v.k, 1.4)));
    }, [focus, positions]);

    // Non-passive wheel listener: the page must not scroll while zooming the graph.
    useEffect(() => {
        const el = svgRef.current;
        if (!el) return undefined;
        const onWheel = (e: WheelEvent) => {
            e.preventDefault();
            const rect = el.getBoundingClientRect();
            const sx = ((e.clientX - rect.left) / (rect.width || W)) * W;
            const sy = ((e.clientY - rect.top) / (rect.height || H)) * H;
            setView((v) => zoomAt(v, e.deltaY < 0 ? 1.15 : 1 / 1.15, sx, sy));
        };
        el.addEventListener('wheel', onWheel, { passive: false });
        return () => el.removeEventListener('wheel', onWheel);
    }, []);

    const scale = () => {
        const rect = svgRef.current?.getBoundingClientRect();
        return rect && rect.width ? W / rect.width : 1;
    };

    const onPointerDown = (e: React.PointerEvent) => {
        if ((e.target as Element).closest('.node')) return;
        drag.current = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y, moved: false };
        setDragging(true);
    };
    const onPointerMove = (e: React.PointerEvent) => {
        const d = drag.current;
        if (!d) return;
        const s = scale();
        const dx = (e.clientX - d.x) * s;
        const dy = (e.clientY - d.y) * s;
        if (Math.abs(dx) + Math.abs(dy) > 2) d.moved = true;
        setView((v) => ({ ...v, x: d.vx + dx, y: d.vy + dy }));
    };
    const endDrag = () => {
        drag.current = null;
        setDragging(false);
    };

    const onKeyDown = (e: React.KeyboardEvent) => {
        const step = 60;
        const keys: Record<string, () => void> = {
            ArrowLeft: () => setView((v) => ({ ...v, x: v.x + step })),
            ArrowRight: () => setView((v) => ({ ...v, x: v.x - step })),
            ArrowUp: () => setView((v) => ({ ...v, y: v.y + step })),
            ArrowDown: () => setView((v) => ({ ...v, y: v.y - step })),
            '+': () => setView((v) => zoomAt(v, 1.25, W / 2, H / 2)),
            '=': () => setView((v) => zoomAt(v, 1.25, W / 2, H / 2)),
            '-': () => setView((v) => zoomAt(v, 0.8, W / 2, H / 2)),
            '0': () => setView(fit([...positions.values()], W, H)),
        };
        if ((e.target as Element).classList?.contains('node')) return;
        const f = keys[e.key];
        if (f) {
            e.preventDefault();
            f();
        }
    };

    const extent = useMemo(() => {
        const pts = [...positions.values()];
        if (!pts.length) return { x: 0, y: 0, w: 1000, h: 1000 };
        const xs = pts.map((p) => p.x);
        const ys = pts.map((p) => p.y);
        const x = Math.min(...xs) - 60;
        const y = Math.min(...ys) - 60;
        return { x, y, w: Math.max(...xs) - x + 60, h: Math.max(...ys) - y + 60 };
    }, [positions]);
    const miniScale = MINI / Math.max(extent.w, extent.h);

    const onMini = (e: React.MouseEvent<SVGSVGElement>) => {
        const rect = e.currentTarget.getBoundingClientRect();
        const p = { x: extent.x + (e.clientX - rect.left) / miniScale, y: extent.y + (e.clientY - rect.top) / miniScale };
        setView((v) => centre(p, W, H, v.k));
    };

    const nodeById = useMemo(() => new Map(graph.nodes.map((n) => [n.id, n])), [graph]);
    const dim = (id: string) => highlight !== null && !highlight.has(id);
    const many = graph.nodes.length;

    return (
        <Frame>
            <svg
                ref={svgRef}
                className={`main${dragging ? ' dragging' : ''}`}
                viewBox={`0 0 ${W} ${H}`}
                role="group"
                aria-label={`Ontology graph: ${graph.nodes.length} ${graph.clustered ? 'groups' : 'nodes'} and ${graph.edges.length} relationships. Drag or use the arrow keys to pan, + and - to zoom, 0 to fit. The list view has the same content as a table.`}
                tabIndex={0}
                onPointerDown={onPointerDown}
                onPointerMove={onPointerMove}
                onPointerUp={endDrag}
                onPointerLeave={endDrag}
                onKeyDown={onKeyDown}
                data-testid="ontology-canvas"
            >
                <defs>
                    <marker id="onto-arrow" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
                        <path d="M 0 0 L 10 5 L 0 10 z" fill="#94a3b8" />
                    </marker>
                </defs>
                <rect x={0} y={0} width={W} height={H} fill="transparent" />
                <g transform={`translate(${view.x},${view.y}) scale(${view.k})`}>
                    {graph.edges.map((e) => {
                        const a = positions.get(e.source);
                        const b = positions.get(e.target);
                        if (!a || !b) return null;
                        const lit = highlight !== null && highlight.has(e.source) && highlight.has(e.target);
                        const faded = highlight !== null && !lit;
                        const nb = nodeById.get(e.target);
                        const r = nb ? nodeRadius(nb) + 3 : 12;
                        const dx = b.x - a.x;
                        const dy = b.y - a.y;
                        const d = Math.sqrt(dx * dx + dy * dy) || 1;
                        const tx = b.x - (dx / d) * r;
                        const ty = b.y - (dy / d) * r;
                        // Edge labels (columns and cardinality) when they can be read: for the selected term's
                        // relationships, between groups, or once zoomed in.
                        const label = lit || e.type === 'aggregate' || view.k >= 1.3 || many <= 12;
                        return (
                            <g key={e.id} opacity={faded ? 0.12 : 1} data-edge={e.id}>
                                <line
                                    x1={a.x}
                                    y1={a.y}
                                    x2={tx}
                                    y2={ty}
                                    stroke={e.type === 'bridge' ? '#c2410c' : lit ? tokens.color.primary : '#94a3b8'}
                                    strokeWidth={(e.type === 'aggregate' ? 1 + Math.min(6, Math.sqrt(e.count)) : 1 + 1.5 * e.confidence) / Math.sqrt(view.k)}
                                    strokeDasharray={DASH[e.type]}
                                    markerEnd={e.type === 'aggregate' ? undefined : 'url(#onto-arrow)'}
                                >
                                    <title>{`${e.label}${e.type !== 'aggregate' ? ` (${e.type === 'bridge' ? 'through a bridge table' : TYPE_LABEL[e.type]}, confidence ${e.confidence})` : ''}`}</title>
                                </line>
                                {label && (
                                    <text className="edge-label" x={(a.x + b.x) / 2} y={(a.y + b.y) / 2 - 4} textAnchor="middle" fontSize={10 / Math.sqrt(view.k)}>
                                        {e.label}
                                    </text>
                                )}
                            </g>
                        );
                    })}
                    {graph.nodes.map((n) => {
                        const p = positions.get(n.id);
                        if (!p) return null;
                        const colour = groupColour(groups, n.group);
                        const isSel = selected === n.id;
                        const lit = highlight !== null && highlight.has(n.id);
                        return (
                            <g
                                key={n.id}
                                className="node"
                                transform={`translate(${p.x},${p.y})`}
                                opacity={dim(n.id) ? 0.2 : 1}
                                role="button"
                                tabIndex={0}
                                aria-label={n.kind === 'cluster' ? `${n.label}: ${n.size} terms. Open the group` : `${n.label} (${n.kind === 'term' ? `term, ${n.status}` : n.kind === 'bridge' ? 'bridge table' : 'table'})`}
                                aria-pressed={isSel}
                                data-node={n.id}
                                onClick={() => onSelect(n)}
                                onKeyDown={(e) => {
                                    if (e.key === 'Enter' || e.key === ' ') {
                                        e.preventDefault();
                                        onSelect(n);
                                    }
                                }}
                            >
                                <Shape n={n} fill={colour} stroke={colour} selected={isSel} />
                                {n.kind === 'cluster' && (
                                    <text y={4} textAnchor="middle" style={{ fill: '#fff', stroke: 'none', fontWeight: 600 }}>{n.size}</text>
                                )}
                                {showLabel(view, many, lit || isSel) && (
                                    <text y={-nodeRadius(n) - 6} textAnchor="middle" fontSize={12 / Math.sqrt(Math.max(view.k, 0.3))} fontWeight={isSel ? 700 : 400}>
                                        {n.label}
                                    </text>
                                )}
                            </g>
                        );
                    })}
                </g>
            </svg>
            <Controls>
                <button type="button" aria-label="Zoom in" onClick={() => setView((v) => zoomAt(v, 1.25, W / 2, H / 2))}>+</button>
                <button type="button" aria-label="Zoom out" onClick={() => setView((v) => zoomAt(v, 0.8, W / 2, H / 2))}>−</button>
                <button type="button" aria-label="Fit the graph" onClick={() => setView(fit([...positions.values()], W, H))}>Fit</button>
            </Controls>
            <Mini viewBox={`0 0 ${MINI} ${MINI}`} onClick={onMini} role="img" aria-label="Minimap: click to move the view">
                {graph.nodes.map((n) => {
                    const p = positions.get(n.id);
                    return p ? (
                        <circle key={n.id} cx={(p.x - extent.x) * miniScale} cy={(p.y - extent.y) * miniScale} r={n.kind === 'cluster' ? 4 : 1.8}
                            fill={groupColour(groups, n.group)} opacity={dim(n.id) ? 0.25 : 1} />
                    ) : null;
                })}
                <rect
                    x={(-view.x / view.k - extent.x) * miniScale}
                    y={(-view.y / view.k - extent.y) * miniScale}
                    width={(W / view.k) * miniScale}
                    height={(H / view.k) * miniScale}
                    fill="none"
                    stroke={tokens.color.primary}
                    strokeWidth={1.5}
                    data-testid="minimap-viewport"
                />
            </Mini>
            <Legend aria-hidden="true">
                <span>● accepted</span>
                <span>◌ proposed</span>
                <span style={{ color: '#c2410c' }}>◆ bridge table</span>
                <span>— foreign key</span>
                <span>- - seen in queries</span>
                <span>··· inferred</span>
            </Legend>
        </Frame>
    );
}
