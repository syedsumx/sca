/**
 * Small deterministic force-directed layout (Fruchterman–Reingold) for the ontology explorer.
 * Pure and seeded so the same graph always renders the same way (stable for tests and users).
 */

export interface LayoutNode {
    id: string;
}

export interface LayoutEdge {
    source: string;
    target: string;
    weight?: number;
}

export interface Point {
    x: number;
    y: number;
}

function seeded(seed: number): () => number {
    let s = seed >>> 0 || 1;
    return () => {
        s = (s * 1664525 + 1013904223) >>> 0;
        return s / 4294967296;
    };
}

export function forceLayout(
    nodes: LayoutNode[],
    edges: LayoutEdge[],
    opts: { width?: number; height?: number; iterations?: number; seed?: number } = {},
): Record<string, Point> {
    const width = opts.width ?? 900;
    const height = opts.height ?? 560;
    const iterations = opts.iterations ?? 220;
    const rand = seeded(opts.seed ?? 42);
    const pos: Record<string, Point> = {};
    const n = Math.max(nodes.length, 1);
    nodes.forEach((node, i) => {
        const angle = (2 * Math.PI * i) / n;
        pos[node.id] = {
            x: width / 2 + (width / 3) * Math.cos(angle) + (rand() - 0.5) * 10,
            y: height / 2 + (height / 3) * Math.sin(angle) + (rand() - 0.5) * 10,
        };
    });
    const k = Math.sqrt((width * height) / n) * 0.75;
    const ids = nodes.map((x) => x.id);
    const valid = edges.filter((e) => pos[e.source] && pos[e.target] && e.source !== e.target);
    let temp = width / 8;
    for (let it = 0; it < iterations; it += 1) {
        const disp: Record<string, Point> = {};
        ids.forEach((id) => {
            disp[id] = { x: 0, y: 0 };
        });
        for (let i = 0; i < ids.length; i += 1) {
            for (let j = i + 1; j < ids.length; j += 1) {
                const a = pos[ids[i]];
                const b = pos[ids[j]];
                let dx = a.x - b.x;
                let dy = a.y - b.y;
                let d = Math.sqrt(dx * dx + dy * dy);
                if (d < 0.01) {
                    dx = 0.01;
                    dy = 0.01;
                    d = 0.014;
                }
                const f = (k * k) / d;
                disp[ids[i]].x += (dx / d) * f;
                disp[ids[i]].y += (dy / d) * f;
                disp[ids[j]].x -= (dx / d) * f;
                disp[ids[j]].y -= (dy / d) * f;
            }
        }
        valid.forEach((e) => {
            const a = pos[e.source];
            const b = pos[e.target];
            const dx = a.x - b.x;
            const dy = a.y - b.y;
            const d = Math.max(Math.sqrt(dx * dx + dy * dy), 0.01);
            const f = ((d * d) / k) * Math.min(1 + (e.weight ?? 1) * 0.1, 2);
            disp[e.source].x -= (dx / d) * f;
            disp[e.source].y -= (dy / d) * f;
            disp[e.target].x += (dx / d) * f;
            disp[e.target].y += (dy / d) * f;
        });
        ids.forEach((id) => {
            const dd = disp[id];
            const len = Math.max(Math.sqrt(dd.x * dd.x + dd.y * dd.y), 0.01);
            pos[id].x = Math.min(width - 40, Math.max(40, pos[id].x + (dd.x / len) * Math.min(len, temp)));
            pos[id].y = Math.min(height - 30, Math.max(30, pos[id].y + (dd.y / len) * Math.min(len, temp)));
        });
        temp = Math.max(temp * 0.97, 1);
    }
    return pos;
}
