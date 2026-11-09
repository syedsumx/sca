/**
 * The explorer's graph, without React: nodes (terms, bridge tables, tables without a term) and edges (relationships)
 * from the API's data; filters; clustering by domain (or schema) when a graph is too big to read; a deterministic
 * force-directed layout; and neighbourhoods for highlighting.
 */

import { ElementStatus, ExplorerData, cardinalityLabel, relationshipType, tableLabel } from './api';

export type NodeKind = 'term' | 'bridge' | 'table' | 'cluster';

export interface GNode {
    id: string;
    kind: NodeKind;
    label: string;
    /** Domain name, or the schema when the dataset has no domain. */
    group: string;
    schema: string;
    status: ElementStatus;
    confidence: number;
    table: string | null;
    /** For a cluster: how many terms it stands for. */
    size: number;
}

export interface GEdge {
    id: string;
    source: string;
    target: string;
    label: string;
    type: 'key' | 'queries' | 'inferred' | 'bridge' | 'aggregate';
    cardinality: string;
    confidence: number;
    status: ElementStatus;
    /** For an aggregate edge between clusters: how many relationships it stands for. */
    count: number;
}

export interface Graph {
    nodes: GNode[];
    edges: GEdge[];
    clustered: boolean;
}

export interface Filters {
    domains: string[];
    schemas: string[];
    statuses: ElementStatus[];
    minConfidence: number;
}

export const NO_FILTERS: Filters = { domains: [], schemas: [], statuses: [], minConfidence: 0 };

/** Above this many nodes the graph starts as one node per group (expand a group to see its terms). */
export const CLUSTER_THRESHOLD = 150;

export function groupOf(x: { domain_name: string | null; schema: string }): string {
    return x.domain_name || (x.schema ? `schema ${x.schema}` : 'no domain');
}

/** Every node and edge of the ontology, before filtering. */
export function buildGraph(d: ExplorerData): Graph {
    const nodes = new Map<string, GNode>();
    const nodeOfTable = new Map<string, string>();
    const tables = new Map(d.tables.map((t) => [t.id, t]));
    for (const t of d.terms) {
        nodes.set(t.id, {
            id: t.id, kind: 'term', label: t.name, group: groupOf(t), schema: t.schema, status: t.status,
            confidence: t.confidence, table: t.table, size: 1,
        });
        if (!nodeOfTable.has(t.table)) nodeOfTable.set(t.table, t.id);
    }
    for (const b of d.bridges) {
        if (b.term && nodes.has(b.term)) continue;          // a bridge with payload has a term: drawn as that term
        const id = `bridge:${b.table}`;
        nodes.set(id, {
            id, kind: 'bridge', label: b.table_name || tableLabel(b.table), group: groupOf(b), schema: b.schema,
            status: 'compiled', confidence: 1, table: b.table, size: 1,
        });
        nodeOfTable.set(b.table, id);
    }
    const tableNode = (urn: string): string => {
        const known = nodeOfTable.get(urn);
        if (known) return known;
        const t = tables.get(urn);
        const id = `table:${urn}`;
        nodes.set(id, {
            id, kind: t?.bridge ? 'bridge' : 'table', label: t?.table_label || tableLabel(urn),
            group: t ? groupOf(t) : 'no domain', schema: t?.schema ?? '', status: 'compiled', confidence: 1, table: urn, size: 1,
        });
        nodeOfTable.set(urn, id);
        return id;
    };
    const edges: GEdge[] = d.relationships.map((r) => {
        const source = tableNode(r.source);
        const target = tableNode(r.target);
        const bridge = r.bridge || nodes.get(source)?.kind === 'bridge';
        return {
            id: r.id, source, target, label: `${r.source_column} → ${r.target_column} · ${cardinalityLabel(r.cardinality)}`,
            type: bridge ? 'bridge' : relationshipType(r.provenance), cardinality: r.cardinality, confidence: r.confidence,
            status: r.status, count: 1,
        };
    });
    return { nodes: [...nodes.values()], edges, clustered: false };
}

/** Domains (or schemas) and schemas present, for the filter pickers. */
export function facets(g: Graph): { groups: string[]; schemas: string[] } {
    const groups = new Set<string>();
    const schemas = new Set<string>();
    for (const n of g.nodes) {
        groups.add(n.group);
        if (n.schema) schemas.add(n.schema);
    }
    return { groups: [...groups].sort(), schemas: [...schemas].sort() };
}

/** The graph under the filters: a term must pass all of them; an edge needs both ends and its own status and
 * confidence to pass; bridge and table nodes stay only while an edge still reaches them. */
export function applyFilters(g: Graph, f: Filters): Graph {
    const keep = new Set<string>();
    for (const n of g.nodes) {
        if (f.domains.length && !f.domains.includes(n.group)) continue;
        if (f.schemas.length && !f.schemas.includes(n.schema)) continue;
        if (n.kind === 'term') {
            if (f.statuses.length && !f.statuses.includes(n.status)) continue;
            if (n.confidence < f.minConfidence) continue;
        }
        keep.add(n.id);
    }
    const edges = g.edges.filter(
        (e) => keep.has(e.source) && keep.has(e.target) && e.confidence >= f.minConfidence
            && (!f.statuses.length || f.statuses.includes(e.status) || e.type === 'bridge'),
    );
    const reached = new Set(edges.flatMap((e) => [e.source, e.target]));
    const nodes = g.nodes.filter((n) => keep.has(n.id) && (n.kind === 'term' || reached.has(n.id)));
    return { nodes, edges, clustered: g.clustered };
}

/** One node per group, edges summed between groups (the level of detail of a big ontology). */
export function cluster(g: Graph): Graph {
    const groups = new Map<string, GNode>();
    for (const n of g.nodes) {
        const id = `cluster:${n.group}`;
        const c = groups.get(id);
        if (c) c.size += n.kind === 'term' ? 1 : 0;
        else groups.set(id, { id, kind: 'cluster', label: n.group, group: n.group, schema: '', status: 'compiled', confidence: 1,
            table: null, size: n.kind === 'term' ? 1 : 0 });
    }
    const groupOfNode = new Map(g.nodes.map((n) => [n.id, `cluster:${n.group}`]));
    const agg = new Map<string, GEdge>();
    for (const e of g.edges) {
        const a = groupOfNode.get(e.source) as string;
        const b = groupOfNode.get(e.target) as string;
        if (a === b) continue;
        const [s, t] = a < b ? [a, b] : [b, a];
        const key = `${s}|${t}`;
        const x = agg.get(key);
        if (x) {
            x.count += 1;
            x.label = `${x.count} relationships`;
            x.confidence = Math.max(x.confidence, e.confidence);
        } else {
            agg.set(key, { id: `agg:${key}`, source: s, target: t, label: '1 relationship', type: 'aggregate', cardinality: '',
                confidence: e.confidence, status: 'compiled', count: 1 });
        }
    }
    return { nodes: [...groups.values()], edges: [...agg.values()], clustered: true };
}

/** A node's neighbours (and itself). */
export function neighbours(g: Graph, id: string): Set<string> {
    const out = new Set([id]);
    for (const e of g.edges) {
        if (e.source === id) out.add(e.target);
        if (e.target === id) out.add(e.source);
    }
    return out;
}

/** Terms (and bridge tables) whose name starts with or contains `q`, best first. */
export function searchNodes(g: Graph, q: string, limit = 8): GNode[] {
    const s = q.trim().toLowerCase();
    if (!s) return [];
    return g.nodes
        .filter((n) => n.kind !== 'cluster' && n.label.toLowerCase().includes(s))
        .sort((a, b) => Number(!a.label.toLowerCase().startsWith(s)) - Number(!b.label.toLowerCase().startsWith(s))
            || a.label.localeCompare(b.label))
        .slice(0, limit);
}

export interface Point {
    x: number;
    y: number;
}

/** A small deterministic pseudo-random sequence (the same graph always lays out the same way). */
function rng(seed: number) {
    let s = seed >>> 0 || 1;
    return () => {
        s = (s * 1664525 + 1013904223) >>> 0;
        return s / 2 ** 32;
    };
}

function hash(s: string): number {
    let h = 2166136261;
    for (let i = 0; i < s.length; i += 1) h = Math.imul(h ^ s.charCodeAt(i), 16777619);
    return h >>> 0;
}

/**
 * Force-directed layout (Fruchterman–Reingold) seeded with each group on its own circle, so domains stay
 * together; repulsion between nodes, springs along edges, gravity to the centre. Returns positions in a square
 * of side `size`.
 */
export function layout(g: Graph, size = 1000, iterations?: number): Map<string, Point> {
    const n = g.nodes.length;
    const pos = new Map<string, Point>();
    if (!n) return pos;
    const groups = [...new Set(g.nodes.map((x) => x.group))].sort();
    const rand = rng(hash(g.nodes.map((x) => x.id).join('|')));
    const c = size / 2;
    const ring = groups.length > 1 ? size * 0.3 : 0;
    const members = new Map<string, number>();
    for (const node of g.nodes) {
        const gi = groups.indexOf(node.group);
        const a = (2 * Math.PI * gi) / groups.length;
        const k = members.get(node.group) ?? 0;
        members.set(node.group, k + 1);
        const r = 30 + 14 * Math.sqrt(k);
        const b = k * 2.4;
        pos.set(node.id, { x: c + ring * Math.cos(a) + r * Math.cos(b) + rand(), y: c + ring * Math.sin(a) + r * Math.sin(b) + rand() });
    }
    if (n === 1) {
        pos.set(g.nodes[0].id, { x: c, y: c });
        return pos;
    }
    const ids = g.nodes.map((x) => x.id);
    const index = new Map(ids.map((id, i) => [id, i]));
    const xs = new Float64Array(n);
    const ys = new Float64Array(n);
    ids.forEach((id, i) => {
        const p = pos.get(id) as Point;
        xs[i] = p.x;
        ys[i] = p.y;
    });
    const k = Math.sqrt((size * size) / n) * 0.6;
    const steps = iterations ?? (n > 400 ? 50 : n > 150 ? 90 : 160);
    let temp = size / 8;
    const dx = new Float64Array(n);
    const dy = new Float64Array(n);
    const links = g.edges
        .map((e) => [index.get(e.source), index.get(e.target)] as const)
        .filter((l): l is readonly [number, number] => l[0] !== undefined && l[1] !== undefined && l[0] !== l[1]);
    for (let it = 0; it < steps; it += 1) {
        dx.fill(0);
        dy.fill(0);
        for (let i = 0; i < n; i += 1) {
            for (let j = i + 1; j < n; j += 1) {
                let ddx = xs[i] - xs[j];
                let ddy = ys[i] - ys[j];
                let d2 = ddx * ddx + ddy * ddy;
                if (d2 < 0.01) {
                    ddx = 0.1 * (i - j);
                    ddy = 0.1;
                    d2 = ddx * ddx + ddy * ddy;
                }
                if (d2 > 9 * k * k) continue;               // far apart: no push worth computing
                const f = (k * k) / d2;
                dx[i] += ddx * f;
                dy[i] += ddy * f;
                dx[j] -= ddx * f;
                dy[j] -= ddy * f;
            }
        }
        for (const [a, b] of links) {
            const ddx = xs[a] - xs[b];
            const ddy = ys[a] - ys[b];
            const d = Math.sqrt(ddx * ddx + ddy * ddy) || 0.01;
            const f = d / k;
            dx[a] -= ddx * f;
            dy[a] -= ddy * f;
            dx[b] += ddx * f;
            dy[b] += ddy * f;
        }
        for (let i = 0; i < n; i += 1) {
            dx[i] += (c - xs[i]) * 0.02;
            dy[i] += (c - ys[i]) * 0.02;
            const d = Math.sqrt(dx[i] * dx[i] + dy[i] * dy[i]) || 1;
            const m = Math.min(d, temp);
            xs[i] += (dx[i] / d) * m;
            ys[i] += (dy[i] / d) * m;
        }
        temp *= 0.96;
    }
    // Into the square, with a margin.
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (let i = 0; i < n; i += 1) {
        minX = Math.min(minX, xs[i]);
        maxX = Math.max(maxX, xs[i]);
        minY = Math.min(minY, ys[i]);
        maxY = Math.max(maxY, ys[i]);
    }
    const span = Math.max(maxX - minX, maxY - minY, 1);
    const scale = (size - 120) / span;
    ids.forEach((id, i) => pos.set(id, { x: 60 + (xs[i] - minX) * scale, y: 60 + (ys[i] - minY) * scale }));
    return pos;
}

export interface View {
    x: number;
    y: number;
    k: number;
}

/** The view (translate + scale) that fits `points` into a `w` × `h` viewport. */
export function fit(points: Point[], w: number, h: number, pad = 40): View {
    if (!points.length) return { x: 0, y: 0, k: 1 };
    const xs = points.map((p) => p.x);
    const ys = points.map((p) => p.y);
    const minX = Math.min(...xs);
    const maxX = Math.max(...xs);
    const minY = Math.min(...ys);
    const maxY = Math.max(...ys);
    const k = Math.min(4, Math.max(0.05, Math.min((w - 2 * pad) / Math.max(maxX - minX, 1), (h - 2 * pad) / Math.max(maxY - minY, 1))));
    return { k, x: w / 2 - ((minX + maxX) / 2) * k, y: h / 2 - ((minY + maxY) / 2) * k };
}

/** The view centred on `p` at scale `k`. */
export function centre(p: Point, w: number, h: number, k: number): View {
    return { k, x: w / 2 - p.x * k, y: h / 2 - p.y * k };
}

/** Zooms by `factor` around the screen point (`sx`, `sy`). */
export function zoomAt(v: View, factor: number, sx: number, sy: number): View {
    const k = Math.min(6, Math.max(0.05, v.k * factor));
    const f = k / v.k;
    return { k, x: sx - (sx - v.x) * f, y: sy - (sy - v.y) * f };
}

/** Labels are drawn when zoomed in enough, or for highlighted nodes, or when the graph is small. */
export function showLabel(v: View, nodes: number, highlighted: boolean): boolean {
    return highlighted || nodes <= 60 || v.k >= 0.8;
}

const PALETTE = ['#5145e5', '#0e7490', '#b45309', '#15803d', '#be185d', '#7c3aed', '#0369a1', '#a16207', '#4d7c0f', '#9f1239'];

/** A stable colour per group. */
export function groupColour(groups: string[], group: string): string {
    const i = groups.indexOf(group);
    return PALETTE[(i < 0 ? hash(group) : i) % PALETTE.length];
}
