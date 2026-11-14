/**
 * A layered layout for a flow's DAG (left to right): each step in the column after its latest dependency, rows
 * ordered to cross fewer edges (barycenters, swept both ways), each column centred.
 */

export const NODE_W = 216;
export const NODE_H = 68;
export const GAP_X = 72;
export const GAP_Y = 28;
export const GRID = 20;

export interface Pos {
    x: number;
    y: number;
}

export function snap(v: number, grid = GRID): number {
    return Math.round(v / grid) * grid;
}

export function layered(steps: { name: string; after?: string[] }[]): Record<string, Pos> {
    const names = steps.map((s) => s.name);
    const known = new Set(names);
    const parents = new Map(steps.map((s) => [s.name, (s.after ?? []).filter((a) => known.has(a) && a !== s.name)]));
    const rank = new Map<string, number>();
    const visiting = new Set<string>();
    const rankOf = (n: string): number => {
        if (rank.has(n)) return rank.get(n)!;
        if (visiting.has(n)) return 0;                  // a cycle: laid out anyway, the checks report it
        visiting.add(n);
        const r = Math.max(-1, ...parents.get(n)!.map(rankOf)) + 1;
        visiting.delete(n);
        rank.set(n, r);
        return r;
    };
    names.forEach(rankOf);
    const layers: string[][] = [];
    for (const n of names) (layers[rank.get(n)!] ??= []).push(n);
    const children = new Map<string, string[]>(names.map((n) => [n, []]));
    for (const [n, ps] of parents) for (const p of ps) children.get(p)!.push(n);
    const index = new Map<string, number>();
    const reindex = () => layers.forEach((l) => l.forEach((n, i) => index.set(n, i)));
    reindex();
    const bary = (n: string, side: Map<string, string[]>) => {
        const xs = side.get(n)!.map((m) => index.get(m)!);
        return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : index.get(n)!;
    };
    for (let sweep = 0; sweep < 4; sweep++) {
        const down = sweep % 2 === 0;
        const order = down ? layers.slice(1) : layers.slice(0, -1).reverse();
        for (const layer of order) {
            const keyed = layer.map((n) => [bary(n, down ? parents : children), index.get(n)!, n] as const);
            keyed.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
            layer.splice(0, layer.length, ...keyed.map((k) => k[2]));
            reindex();
        }
    }
    const tallest = Math.max(1, ...layers.map((l) => l.length));
    const out: Record<string, Pos> = {};
    layers.forEach((layer, col) => {
        const offset = ((tallest - layer.length) * (NODE_H + GAP_Y)) / 2;
        layer.forEach((n, row) => {
            out[n] = { x: snap(col * (NODE_W + GAP_X)), y: snap(offset + row * (NODE_H + GAP_Y)) };
        });
    });
    return out;
}

/** Positions for every step: the saved ones, and a layered place for steps without one. */
export function positions(steps: { name: string; after?: string[] }[], saved?: Record<string, Pos>): Record<string, Pos> {
    const have = saved ?? {};
    if (steps.every((s) => have[s.name])) return Object.fromEntries(steps.map((s) => [s.name, have[s.name]]));
    const auto = layered(steps);
    if (!Object.keys(have).length) return auto;
    const out: Record<string, Pos> = {};
    const maxX = Math.max(0, ...Object.values(have).map((p) => p.x));
    for (const s of steps) {
        if (have[s.name]) out[s.name] = have[s.name];
        else {
            const dep = (s.after ?? []).map((a) => out[a] ?? have[a]).filter(Boolean) as Pos[];
            out[s.name] = dep.length
                ? { x: snap(Math.max(...dep.map((d) => d.x)) + NODE_W + GAP_X), y: snap(dep[0].y) }
                : { x: snap(maxX + NODE_W + GAP_X), y: auto[s.name].y };
        }
    }
    return out;
}

/** The box around the steps. */
export function bounds(pos: Record<string, Pos>): { x: number; y: number; w: number; h: number } {
    const ps = Object.values(pos);
    if (!ps.length) return { x: 0, y: 0, w: NODE_W, h: NODE_H };
    const x = Math.min(...ps.map((p) => p.x));
    const y = Math.min(...ps.map((p) => p.y));
    return { x, y, w: Math.max(...ps.map((p) => p.x)) + NODE_W - x, h: Math.max(...ps.map((p) => p.y)) + NODE_H - y };
}

/** An edge between two steps, as an SVG path (a horizontal S curve from the right side to the left side). */
export function edgePath(a: Pos, b: Pos): string {
    const x1 = a.x + NODE_W;
    const y1 = a.y + NODE_H / 2;
    const x2 = b.x;
    const y2 = b.y + NODE_H / 2;
    const dx = Math.max(40, Math.abs(x2 - x1) / 2);
    return `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
}
