import { Alert, Radio } from 'antd';
import React, { useMemo, useState } from 'react';
import { useHistory } from 'react-router-dom';
import { useDheQuery } from '../api/hooks';
import type { LineageGraph as Graph, LineageGraphNode } from '../api/types';
import { platformName } from '../utils/format';
import { Muted, Skeleton, tokens } from './tokens';

const NODE_W = 180;
const NODE_H = 46;
const COL_GAP = 56;
const ROW_GAP = 18;
const PAD = 16;

export interface Placed {
    node: LineageGraphNode;
    layer: number;
    x: number;
    y: number;
}

/** Columns by distance from the asset: upstreams to the left, downstreams to the right; each column in
 *  name order so the same graph always draws the same way. */
export function layout(root: string, graph: Graph): { placed: Placed[]; width: number; height: number } {
    const layer = new Map<string, number>([[root, 0]]);
    const queue = [root];
    while (queue.length) {
        const cur = queue.shift() as string;
        const l = layer.get(cur) as number;
        for (const e of graph.edges) {
            if (e.source === cur && l >= 0 && !layer.has(e.target)) {
                layer.set(e.target, l + (e.indirect ? 2 : 1));
                queue.push(e.target);
            }
            if (e.target === cur && l <= 0 && !layer.has(e.source)) {
                layer.set(e.source, l - (e.indirect ? 2 : 1));
                queue.push(e.source);
            }
        }
    }
    const nodes = graph.nodes.filter((n) => layer.has(n.urn));
    const layers = Array.from(new Set(nodes.map((n) => layer.get(n.urn) as number))).sort((a, b) => a - b);
    const column = new Map(layers.map((l, i) => [l, i]));
    const rows = new Map<number, LineageGraphNode[]>();
    for (const n of nodes) {
        const l = layer.get(n.urn) as number;
        rows.set(l, [...(rows.get(l) ?? []), n]);
    }
    const tallest = Math.max(1, ...Array.from(rows.values()).map((r) => r.length));
    const height = PAD * 2 + tallest * NODE_H + (tallest - 1) * ROW_GAP;
    const placed: Placed[] = [];
    rows.forEach((members, l) => {
        members.sort((a, b) => (a.name ?? a.urn).localeCompare(b.name ?? b.urn));
        const top = (height - (members.length * NODE_H + (members.length - 1) * ROW_GAP)) / 2;
        members.forEach((node, i) => {
            placed.push({ node, layer: l, x: PAD + (column.get(l) as number) * (NODE_W + COL_GAP), y: top + i * (NODE_H + ROW_GAP) });
        });
    });
    return { placed, width: PAD * 2 + layers.length * NODE_W + (layers.length - 1) * COL_GAP, height };
}

function tone(status: string | null): { stroke: string; label: string } {
    const s = (status ?? '').toUpperCase();
    if (s === 'FAILING' || s === 'FAILED' || s === 'BAD') return { stroke: tokens.color.bad, label: 'quality checks failing' };
    if (s === 'WARNING' || s === 'WARN') return { stroke: tokens.color.warn, label: 'quality warning' };
    if (s === 'HEALTHY' || s === 'PASSING' || s === 'GOOD') return { stroke: tokens.color.good, label: 'quality checks passing' };
    return { stroke: tokens.color.border, label: 'no quality checks' };
}

function short(s: string, n: number) {
    return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}

function label(n: LineageGraphNode) {
    if (n.name) return n.name;
    const m = /,([^,]+),[A-Z]+\)$/.exec(n.urn) ?? /\(([^,]+),([^)]+)\)$/.exec(n.urn);
    return m ? m[m.length - 1] : n.urn;
}

/** The lineage around an asset, drawn: what feeds it on the left, what it feeds on the right. Assets the
 *  signed-in user may not see are left out and counted. */
export function LineageGraphView({ urn }: { urn: string }) {
    const [depth, setDepth] = useState(2);
    const history = useHistory();
    const q = useDheQuery(`/embed/v1/graph?urn=${urn}&depth=${depth}`, (c, signal) => c.lineageGraph(urn, depth, { signal }));
    const drawn = useMemo(() => (q.data ? layout(urn, q.data) : null), [q.data, urn]);
    if (q.error) return <Alert type="warning" showIcon message="Lineage is not available right now." />;
    const header = (
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 12, flexWrap: 'wrap' }}>
            <strong>Lineage</strong>
            <Radio.Group
                size="small"
                optionType="button"
                value={depth}
                onChange={(e) => setDepth(Number(e.target.value))}
                options={[
                    { label: '1 hop', value: 1 },
                    { label: '2 hops', value: 2 },
                    { label: '3 hops', value: 3 },
                ]}
                aria-label="How far to follow lineage"
            />
            <Muted>Sources on the left, what uses this asset on the right. Select an asset to open it.</Muted>
        </div>
    );
    if (q.loading || !drawn || !q.data) return <>{header}<Skeleton h={160} /></>;
    if (q.data.nodes.length <= 1) {
        return (
            <>
                {header}
                <Muted>
                    No lineage recorded yet.{' '}
                    {q.data.hidden_count > 0 && `${q.data.hidden_count} related asset(s) are hidden by access policy.`}
                </Muted>
            </>
        );
    }
    const at = new Map(drawn.placed.map((p) => [p.node.urn, p]));
    const open = (u: string) => u !== urn && history.push(`/asset/${encodeURIComponent(u)}`);
    return (
        <>
            {header}
            <div style={{ overflowX: 'auto', border: `1px solid ${tokens.color.border}`, borderRadius: tokens.radius.md, background: tokens.color.surfaceAlt }}>
                <svg
                    width={drawn.width}
                    height={drawn.height}
                    viewBox={`0 0 ${drawn.width} ${drawn.height}`}
                    role="img"
                    aria-label={`Lineage of ${label(at.get(urn)?.node ?? { urn, name: null, type: null, platform: null, quality_status: null })}`}
                    data-testid="lineage-graph"
                >
                    <defs>
                        <marker id="lineage-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                            <path d="M0,0 L10,5 L0,10 z" fill={tokens.color.textMuted} />
                        </marker>
                    </defs>
                    {q.data.edges.map((e) => {
                        const a = at.get(e.source);
                        const b = at.get(e.target);
                        if (!a || !b) return null;
                        const x1 = a.x + NODE_W;
                        const y1 = a.y + NODE_H / 2;
                        const x2 = b.x;
                        const y2 = b.y + NODE_H / 2;
                        const mid = (x1 + x2) / 2;
                        return (
                            <path
                                key={`${e.source}>${e.target}`}
                                d={`M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2 - 2},${y2}`}
                                fill="none"
                                stroke={tokens.color.textMuted}
                                strokeOpacity={0.55}
                                strokeWidth={1.5}
                                strokeDasharray={e.indirect ? '5 4' : undefined}
                                markerEnd="url(#lineage-arrow)"
                            >
                                <title>{`${label(a.node)} feeds ${label(b.node)}${e.indirect ? ' (through other assets)' : ''}`}</title>
                            </path>
                        );
                    })}
                    {drawn.placed.map(({ node, x, y }) => {
                        const root = node.urn === urn;
                        const t = tone(node.quality_status);
                        const name = label(node);
                        const sub = [node.platform ? platformName(node.platform) : null, node.type?.toLowerCase().replace(/_/g, ' ')]
                            .filter(Boolean)
                            .join(' · ');
                        return (
                            <g
                                key={node.urn}
                                transform={`translate(${x},${y})`}
                                role={root ? undefined : 'link'}
                                tabIndex={root ? undefined : 0}
                                aria-label={root ? undefined : `Open ${name}`}
                                style={{ cursor: root ? 'default' : 'pointer' }}
                                onClick={() => open(node.urn)}
                                onKeyDown={(ev) => (ev.key === 'Enter' || ev.key === ' ') && open(node.urn)}
                                data-urn={node.urn}
                            >
                                <title>{`${name}${sub ? ` (${sub})` : ''}: ${t.label}`}</title>
                                <rect
                                    width={NODE_W}
                                    height={NODE_H}
                                    rx={8}
                                    fill={root ? tokens.color.primarySoft : tokens.color.surface}
                                    stroke={root ? tokens.color.primary : tokens.color.border}
                                    strokeWidth={root ? 2 : 1}
                                />
                                <rect x={0} y={6} width={4} height={NODE_H - 12} rx={2} fill={t.stroke} />
                                <text x={14} y={19} fontSize={13} fontWeight={root ? 600 : 500} fill={tokens.color.text}>
                                    {short(name, 22)}
                                </text>
                                <text x={14} y={35} fontSize={11} fill={tokens.color.textMuted}>
                                    {short(sub, 27)}
                                </text>
                            </g>
                        );
                    })}
                </svg>
            </div>
            <div style={{ display: 'flex', gap: 16, marginTop: 8, flexWrap: 'wrap', fontSize: 12, color: tokens.color.textMuted }}>
                <span><Swatch color={tokens.color.good} /> checks passing</span>
                <span><Swatch color={tokens.color.warn} /> warning</span>
                <span><Swatch color={tokens.color.bad} /> failing</span>
                <span>– – through other assets</span>
                {q.data.hidden_count > 0 && (
                    <span data-testid="lineage-hidden">
                        {q.data.hidden_count} more asset(s) hidden by access policy
                    </span>
                )}
            </div>
        </>
    );
}

function Swatch({ color }: { color: string }) {
    return <span style={{ display: 'inline-block', width: 10, height: 10, borderRadius: 2, background: color, verticalAlign: 'middle' }} />;
}
