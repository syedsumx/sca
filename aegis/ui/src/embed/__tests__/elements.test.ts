import { computeBaseUrl, embedFetch } from '../api';
import { defineElements, layerLineage } from '../elements';
import { renderFrame } from '../frame';

const DS = 'urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.public.orders,PROD)';
const UP = 'urn:li:dataset:(urn:li:dataPlatform:s3,raw.orders,PROD)';
const DOWN = 'urn:li:dashboard:(looker,revenue)';

const RESPONSES: Record<string, unknown> = {
    [`/aegis/api/embed/v1/assets/${encodeURIComponent(DS)}`]: {
        urn: DS,
        name: 'orders',
        type: 'DATASET',
        platform: 'snowflake',
        description: 'All orders',
        owners: ['urn:li:corpuser:emeka'],
        certification: 'CERTIFIED',
        quality_status: 'FAIL',
        freshness: { last_updated: new Date(Date.now() - 120_000).toISOString() },
        url: 'https://datahub.example.com/dataset/x',
    },
    [`/aegis/api/embed/v1/quality/${encodeURIComponent(DS)}`]: {
        urn: DS,
        status: 'PASS',
        monitors: [{ name: 'row_count', type: 'VOLUME', status: 'PASS', at: '2026-10-25T10:00:00Z' }],
    },
    [`/aegis/api/embed/v1/lineage/${encodeURIComponent(DS)}`]: {
        nodes: [
            { urn: DS, name: 'orders', type: 'DATASET', platform: 'snowflake', quality_status: 'PASS' },
            { urn: UP, name: 'raw.orders', type: 'DATASET', platform: 's3', quality_status: null },
            { urn: DOWN, name: 'secret', type: 'DASHBOARD', platform: 'looker', quality_status: null, hidden: true },
        ],
        edges: [
            { source: UP, target: DS },
            { source: DS, target: DOWN },
        ],
        hidden_count: 1,
    },
};

const seen: string[] = [];

beforeAll(() => {
    vi.stubGlobal(
        'fetch',
        vi.fn(async (input: string, init?: RequestInit) => {
            const u = new URL(input, 'http://localhost');
            seen.push(`${u.pathname}?${u.searchParams.toString()}|${init?.credentials}`);
            if (u.searchParams.get('token') === 'expired') return new Response('{}', { status: 401 });
            const body = RESPONSES[u.pathname];
            return body ? new Response(JSON.stringify(body), { status: 200 }) : new Response('{}', { status: 404 });
        }),
    );
    defineElements();
    defineElements(); // idempotent
});

afterAll(() => {
    vi.unstubAllGlobals();
});

const text = (node: HTMLElement) => node.shadowRoot?.querySelector('.box')?.textContent ?? '';

async function settle(node: HTMLElement) {
    for (let i = 0; i < 20; i += 1) {
        await new Promise((r) => setTimeout(r, 0));
        if (!node.shadowRoot?.querySelector('[aria-busy]')) return;
    }
}

function make(tag: string, attrs: Record<string, string>) {
    const node = document.createElement(tag);
    Object.entries(attrs).forEach(([k, v]) => node.setAttribute(k, v));
    document.body.append(node);
    return node;
}

describe('custom elements', () => {
    it('<aegis-asset-card> renders inside shadow DOM with trust pills', async () => {
        const node = make('aegis-asset-card', { urn: DS, token: 't1', 'base-url': '/aegis/api' });
        await settle(node);
        const root = node.shadowRoot as ShadowRoot;
        expect(document.body.querySelector('a')).toBeNull(); // nothing leaks into light DOM
        expect(root.querySelector('a')?.textContent).toBe('orders');
        expect(root.querySelector('a')?.getAttribute('rel')).toBe('noopener noreferrer');
        expect(text(node)).toContain('Certified');
        expect(text(node)).toContain('Failing');
        expect(text(node)).toContain('2 min ago');
        expect(text(node)).toContain('Owners: emeka');
        expect(seen.some((s) => s.includes('token=t1') && s.endsWith('|omit'))).toBe(true);
        node.remove();
    });

    it('<aegis-quality-badge> and <aegis-lineage> render', async () => {
        const badge = make('aegis-quality-badge', { urn: DS, token: 't1', 'base-url': '/aegis/api' });
        const lin = make('aegis-lineage', { urn: DS, token: 't1', 'base-url': '/aegis/api', depth: '3' });
        await settle(badge);
        await settle(lin);
        expect(text(badge)).toContain('Healthy');
        const svg = lin.shadowRoot?.querySelector('svg');
        expect(svg?.getAttribute('aria-label')).toBe('Lineage for orders');
        expect(svg?.querySelectorAll('g')).toHaveLength(3);
        expect(svg?.querySelectorAll('path')).toHaveLength(2);
        expect(text(lin)).toContain('🔒 hidden');
        expect(text(lin)).toContain('1 more asset(s) hidden');
        expect(seen.some((s) => s.includes('depth=3'))).toBe(true);
        badge.remove();
        lin.remove();
    });

    it('shows clear errors for missing attributes and revoked tokens', async () => {
        const bare = make('aegis-asset-card', {});
        expect(text(bare)).toBe('Missing urn or token attribute.');
        const revoked = make('aegis-asset-card', { urn: DS, token: 'expired', 'base-url': '/aegis/api' });
        await settle(revoked);
        expect(text(revoked)).toBe('This embed link has expired or was revoked.');
        bare.remove();
        revoked.remove();
    });

    it('layerLineage places upstream left, downstream right', () => {
        const laid = layerLineage(DS, RESPONSES[`/aegis/api/embed/v1/lineage/${encodeURIComponent(DS)}`] as never);
        const by = Object.fromEntries(laid.map((n) => [n.urn, n.layer]));
        expect(by).toEqual({ [UP]: -1, [DS]: 0, [DOWN]: 1 });
    });
});

describe('embed helpers', () => {
    it('computeBaseUrl prefers data-aegis-base, then the script path', () => {
        const s = document.createElement('script');
        s.src = 'https://dh.example.com/aegis/api/embed/v1/sdk.js';
        expect(computeBaseUrl(s)).toBe('https://dh.example.com/aegis/api');
        s.dataset.aegisBase = 'https://other/api';
        expect(computeBaseUrl(s)).toBe('https://other/api');
        expect(computeBaseUrl(null)).toBe('/aegis/api');
    });

    it('embedFetch sends the token in the query and no credentials', async () => {
        const f = vi.fn(async () => new Response('{"ok":1}', { status: 200 }));
        await expect(embedFetch('/x/', '/assets/a', 'tok', { depth: '1' }, f as unknown as typeof fetch)).resolves.toEqual({ ok: 1 });
        expect(f).toHaveBeenCalledWith('/x/embed/v1/assets/a?depth=1&token=tok', expect.objectContaining({ credentials: 'omit' }));
        // the backend's snippets pass base-url="…/aegis/api/embed/v1"
        await embedFetch('https://h/aegis/api/embed/v1', '/quality/a', 'tok', {}, f as unknown as typeof fetch);
        expect(f).toHaveBeenLastCalledWith('https://h/aegis/api/embed/v1/quality/a?token=tok', expect.anything());
    });

    it('renderFrame maps the frame path to an element and derives the base', () => {
        document.body.innerHTML = '<div id="aegis-embed"></div>';
        const node = renderFrame(document, { pathname: '/aegis/api/embed/v1/frame/lineage', search: `?urn=${encodeURIComponent(DS)}&token=t&theme=dark` });
        expect(node?.tagName.toLowerCase()).toBe('aegis-lineage');
        expect(node?.getAttribute('base-url')).toBe('/aegis/api');
        expect(node?.getAttribute('theme')).toBe('dark');
        expect(renderFrame(document, { pathname: '/frame/nope', search: '' })).toBeNull();
        expect(document.getElementById('aegis-embed')?.textContent).toContain('Unknown embed kind');
        document.body.innerHTML = '';
    });
});
