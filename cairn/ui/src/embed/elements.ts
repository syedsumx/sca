/**
 * Dependency-free Web Components for BI tools and portals (spec §4.8 / §6.6):
 *
 *   <cairn-asset-card urn="…" token="…"></cairn-asset-card>
 *   <cairn-quality-badge urn="…" token="…"></cairn-quality-badge>
 *   <cairn-lineage urn="…" token="…" depth="2"></cairn-lineage>
 *
 * Attributes: `urn`, `token` (embed token), `base-url` (Cairn API base, default derived from the script
 * URL), `theme` = light | dark. Rendering uses shadow DOM and textContent only (no innerHTML of data).
 */

import { EmbedAssetCard, EmbedLineage, EmbedQuality, defaultBaseUrl, embedFetch } from './api';

const SVG_NS = 'http://www.w3.org/2000/svg';

export type Tone = 'good' | 'warn' | 'bad' | 'neutral';

export function tone(status: string | null | undefined): Tone {
    const s = (status || '').toUpperCase();
    if (s === 'PASS' || s === 'HEALTHY') return 'good';
    if (s === 'WARN' || s === 'WARNING' || s === 'TRAINING' || s === 'LEARNING') return 'warn';
    if (s === 'FAIL' || s === 'FAILING' || s === 'ERROR' || s === 'UNHEALTHY') return 'bad';
    return 'neutral';
}

export const TONE_LABEL: Record<Tone, string> = { good: 'Healthy', warn: 'Warning', bad: 'Failing', neutral: 'Untested' };

const STYLES = `
:host { all: initial; display: inline-block; font: 13px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  --fg:var(--cairn-fg,#1f2937); --muted:#6b7280; --bg:var(--cairn-bg,#fff); --border:var(--cairn-border,#e5e7eb); --good:#2e7d32; --warn:#b26a00; --bad:#c62828; --neutral:#6b7280; --accent:var(--cairn-accent,#533fd1); }
:host([theme="dark"]) { --fg:var(--cairn-fg,#f3f4f6); --muted:#9ca3af; --bg:var(--cairn-bg,#111827); --border:var(--cairn-border,#374151); --accent:var(--cairn-accent,#a5b4fc); }
.box { color: var(--fg); background: var(--bg); border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; max-width: 420px; }
.title { font-weight: 600; font-size: 15px; margin: 0 0 2px; }
.meta { color: var(--muted); font-size: 12px; }
.desc { margin: 8px 0; color: var(--muted); }
.row { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; margin-top: 6px; }
.pill { display: inline-flex; gap: 4px; align-items: center; border-radius: 999px; padding: 1px 8px; font-size: 12px; border: 1px solid currentColor; }
.good { color: var(--good); } .warn { color: var(--warn); } .bad { color: var(--bad); } .neutral { color: var(--neutral); }
.dot { width: 7px; height: 7px; border-radius: 50%; background: currentColor; display: inline-block; }
a { color: var(--accent); text-decoration: none; } a:hover { text-decoration: underline; }
.err { color: var(--bad); font-size: 12px; }
svg text { fill: var(--fg); font-size: 11px; }
`;

function el<K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string, text?: string): HTMLElementTagNameMap[K] {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
}

export function qualityPill(status: string | null | undefined): HTMLSpanElement {
    const t = tone(status);
    const pill = el('span', `pill ${t}`);
    pill.setAttribute('title', `Data quality: ${TONE_LABEL[t]}`);
    pill.append(el('span', 'dot'), document.createTextNode(TONE_LABEL[t]));
    return pill;
}

export function relative(iso: string | null | undefined, now = Date.now()): string {
    if (!iso) return '';
    const s = Math.max(0, Math.round((now - Date.parse(iso)) / 1000));
    if (s < 60) return 'just now';
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    return `${Math.round(s / 86400)} d ago`;
}

abstract class CairnElement extends HTMLElement {
    static get observedAttributes() {
        return ['urn', 'token', 'base-url', 'theme', 'depth'];
    }

    protected root: ShadowRoot;

    protected body: HTMLDivElement;

    private seq = 0;

    constructor() {
        super();
        this.root = this.attachShadow({ mode: 'open' });
        const style = document.createElement('style');
        style.textContent = STYLES;
        this.body = el('div', 'box');
        this.body.setAttribute('role', 'region');
        this.root.append(style, this.body);
    }

    connectedCallback() {
        void this.refresh();
    }

    attributeChangedCallback(_name: string, old: string | null, next: string | null) {
        if (old !== next && this.isConnected) void this.refresh();
    }

    get baseUrl(): string {
        return this.getAttribute('base-url') || defaultBaseUrl();
    }

    protected abstract load(urn: string, token: string): Promise<void>;

    async refresh(): Promise<void> {
        const urn = this.getAttribute('urn');
        const token = this.getAttribute('token');
        if (!urn || !token) {
            this.body.replaceChildren(el('span', 'err', 'Missing urn or token attribute.'));
            return;
        }
        this.seq += 1;
        const seq = this.seq;
        this.body.setAttribute('aria-busy', 'true');
        this.body.replaceChildren(el('span', 'meta', 'Loading…'));
        try {
            await this.load(urn, token);
        } catch (e) {
            if (seq === this.seq) this.body.replaceChildren(el('span', 'err', (e as Error).message));
        } finally {
            if (seq === this.seq) this.body.removeAttribute('aria-busy');
        }
    }
}

export class CairnAssetCardElement extends CairnElement {
    protected async load(urn: string, token: string) {
        const card = await embedFetch<EmbedAssetCard>(this.baseUrl, `/assets/${encodeURIComponent(urn)}`, token);
        const title = el('div', 'title');
        const link = el('a', undefined, card.name);
        link.href = card.url;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        title.append(link);
        const meta = el('div', 'meta', [card.type, card.platform].filter(Boolean).join(' · '));
        const row = el('div', 'row');
        if ((card.certification || '').toUpperCase() === 'CERTIFIED') row.append(el('span', 'pill good', '✔ Certified'));
        row.append(qualityPill(card.quality_status));
        if (card.freshness?.last_updated) row.append(el('span', 'pill neutral', `⏱ ${relative(card.freshness.last_updated)}`));
        const nodes: Node[] = [title, meta];
        if (card.description) nodes.push(el('div', 'desc', card.description.slice(0, 280)));
        nodes.push(row);
        if (card.owners.length) nodes.push(el('div', 'meta', `Owners: ${card.owners.map((o) => o.replace(/^urn:li:(corpuser|corpGroup):/, '')).join(', ')}`));
        this.body.setAttribute('aria-label', `Data asset ${card.name}`);
        this.body.replaceChildren(...nodes);
    }
}

export class CairnQualityBadgeElement extends CairnElement {
    protected async load(urn: string, token: string) {
        const q = await embedFetch<EmbedQuality>(this.baseUrl, `/quality/${encodeURIComponent(urn)}`, token);
        const pill = qualityPill(q.status);
        const failing = q.monitors.filter((m) => tone(m.status) === 'bad');
        pill.setAttribute(
            'title',
            q.monitors.length
                ? q.monitors.map((m) => `${m.name}: ${m.status}`).join('\n')
                : 'No quality monitors on this asset',
        );
        const latest = q.monitors.map((m) => m.at).filter(Boolean).sort().pop() ?? null;
        this.body.style.padding = '4px 8px';
        this.body.setAttribute('aria-label', `Data quality ${TONE_LABEL[tone(q.status)]}`);
        this.body.replaceChildren(
            pill,
            el('span', 'meta', `  ${failing.length ? `${failing.length} failing · ` : ''}${latest ? relative(latest) : ''}`),
        );
    }
}

export interface LaidOutNode {
    urn: string;
    layer: number;
    index: number;
}

/** Layered layout: root at layer 0, upstreams negative, downstreams positive (BFS over edges). */
export function layerLineage(rootUrn: string, lineage: EmbedLineage): LaidOutNode[] {
    const layer = new Map<string, number>([[rootUrn, 0]]);
    const queue = [rootUrn];
    while (queue.length) {
        const cur = queue.shift() as string;
        const l = layer.get(cur) as number;
        lineage.edges.forEach((e) => {
            if (e.source === cur && !layer.has(e.target) && l >= 0) {
                layer.set(e.target, l + 1);
                queue.push(e.target);
            }
            if (e.target === cur && !layer.has(e.source) && l <= 0) {
                layer.set(e.source, l - 1);
                queue.push(e.source);
            }
        });
    }
    lineage.nodes.forEach((n) => {
        if (!layer.has(n.urn)) layer.set(n.urn, 1);
    });
    const counts = new Map<number, number>();
    return lineage.nodes
        .map((n) => n.urn)
        .sort()
        .map((urn) => {
            const l = layer.get(urn) as number;
            const index = counts.get(l) ?? 0;
            counts.set(l, index + 1);
            return { urn, layer: l, index };
        });
}

export class CairnLineageElement extends CairnElement {
    protected async load(urn: string, token: string) {
        const depth = this.getAttribute('depth') || '2';
        const lineage = await embedFetch<EmbedLineage>(this.baseUrl, `/lineage/${encodeURIComponent(urn)}`, token, { depth });
        const laid = layerLineage(urn, lineage);
        const byUrn = new Map(lineage.nodes.map((n) => [n.urn, n]));
        const layers = laid.map((n) => n.layer);
        const minL = Math.min(0, ...layers);
        const maxL = Math.max(0, ...layers);
        const colW = 170;
        const rowH = 44;
        const maxRows = Math.max(1, ...Array.from(new Set(layers)).map((l) => laid.filter((n) => n.layer === l).length));
        const width = (maxL - minL + 1) * colW + 20;
        const height = maxRows * rowH + 20;
        const pos = new Map(laid.map((n) => [n.urn, { x: 10 + (n.layer - minL) * colW, y: 10 + n.index * rowH }]));
        const svg = document.createElementNS(SVG_NS, 'svg');
        svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
        svg.setAttribute('width', String(Math.min(width, 900)));
        svg.setAttribute('role', 'img');
        svg.setAttribute('aria-label', `Lineage for ${byUrn.get(urn)?.name ?? urn}`);
        lineage.edges.forEach((e) => {
            const a = pos.get(e.source);
            const b = pos.get(e.target);
            if (!a || !b) return;
            const path = document.createElementNS(SVG_NS, 'path');
            const x1 = a.x + 140;
            const y1 = a.y + 15;
            const x2 = b.x;
            const y2 = b.y + 15;
            path.setAttribute('d', `M${x1},${y1} C${x1 + 20},${y1} ${x2 - 20},${y2} ${x2},${y2}`);
            path.setAttribute('fill', 'none');
            path.setAttribute('stroke', '#9ca3af');
            svg.append(path);
        });
        laid.forEach((n) => {
            const node = byUrn.get(n.urn);
            const p = pos.get(n.urn);
            if (!node || !p) return;
            const g = document.createElementNS(SVG_NS, 'g');
            g.setAttribute('transform', `translate(${p.x},${p.y})`);
            const rect = document.createElementNS(SVG_NS, 'rect');
            rect.setAttribute('width', '140');
            rect.setAttribute('height', '30');
            rect.setAttribute('rx', '6');
            rect.setAttribute('fill', n.urn === urn ? '#f1effd' : 'transparent');
            const t = tone(node.quality_status);
            rect.setAttribute('stroke', t === 'bad' ? '#c62828' : t === 'warn' ? '#b26a00' : '#d1d5db');
            const label = document.createElementNS(SVG_NS, 'text');
            label.setAttribute('x', '8');
            label.setAttribute('y', '19');
            label.textContent = node.hidden ? '🔒 hidden' : node.name.length > 20 ? `${node.name.slice(0, 19)}…` : node.name;
            const title = document.createElementNS(SVG_NS, 'title');
            title.textContent = node.hidden ? 'You do not have access to this asset' : `${node.name} (${node.platform ?? node.type}) — ${TONE_LABEL[t]}`;
            g.append(rect, label, title);
            svg.append(g);
        });
        const nodes: Node[] = [svg];
        if (lineage.hidden_count) nodes.push(el('div', 'meta', `${lineage.hidden_count} more asset(s) hidden by access policy`));
        this.body.style.maxWidth = 'none';
        this.body.replaceChildren(...nodes);
    }
}

export const ELEMENTS: Record<string, CustomElementConstructor> = {
    'cairn-asset-card': CairnAssetCardElement,
    'cairn-quality-badge': CairnQualityBadgeElement,
    'cairn-lineage': CairnLineageElement,
};

/** Idempotent registration (the SDK may be included more than once on a page). */
export function defineElements(registry: CustomElementRegistry = customElements): void {
    Object.entries(ELEMENTS).forEach(([name, ctor]) => {
        if (!registry.get(name)) registry.define(name, ctor);
    });
}
