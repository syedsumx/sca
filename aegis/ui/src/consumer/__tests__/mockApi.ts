import { ConsumerClient } from '../api/client';
import { QueryCache } from '../api/cache';

export type Handler = (body: unknown, url: URL) => unknown | Promise<unknown>;

export interface MockCall {
    method: string;
    path: string;
    url: URL;
    body: unknown;
}

/**
 * Route table keyed by "METHOD /path" (path relative to the API base, without query). GraphQL
 * requests are matched by operation name: "GQL aegisConsumerSearch". Unknown routes → 404.
 */
export function mockFetch(routes: Record<string, Handler | unknown>, calls: MockCall[] = []) {
    const impl = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
        const url = new URL(String(input), 'http://localhost');
        const method = (init?.method ?? 'GET').toUpperCase();
        const body = init?.body ? JSON.parse(String(init.body)) : undefined;
        let path = url.pathname.replace(/^\/aegis\/api/, '');
        let key = `${method} ${decodeURIComponent(path)}`;
        if (url.pathname === '/api/v2/graphql') {
            const op = /query\s+(\w+)/.exec((body as { query: string }).query)?.[1] ?? 'anonymous';
            key = `GQL ${op}`;
            path = `/graphql#${op}`;
        }
        calls.push({ method, path: decodeURIComponent(path), url, body });
        const route = Object.prototype.hasOwnProperty.call(routes, key) ? routes[key] : undefined;
        if (route === undefined) {
            return new Response(JSON.stringify({ detail: `no mock for ${key}` }), { status: 404 });
        }
        if (route instanceof Error) {
            return new Response(JSON.stringify({ detail: route.message }), { status: 500 });
        }
        const value = typeof route === 'function' ? await (route as Handler)(body, url) : route;
        if (value instanceof Response) return value;
        return new Response(value === undefined ? '' : JSON.stringify(value), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
        });
    };
    return impl as typeof fetch;
}

export function makeClient(routes: Record<string, Handler | unknown>, calls: MockCall[] = []) {
    const client = new ConsumerClient({ fetchImpl: mockFetch(routes, calls) });
    return { client, cache: new QueryCache(), calls };
}

export const DS = 'urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.public.orders,PROD)';

export const PRODUCT = {
    urn: 'urn:li:dataProduct:customer360',
    name: 'Customer 360',
    description: 'Unified customer view',
    domain: { urn: 'urn:li:domain:finance', name: 'Finance' },
    owners: ['urn:li:corpuser:emeka'],
    tags: [],
    certification: 'CERTIFIED',
    quality_status: 'PASS',
    assets_count: 12,
    consumers: 38,
    sla: { freshness: 'hourly', availability: '99.5%' },
    access: { status: 'NONE', request_id: null, expires_at: null },
    request_policy: { auto_approve: true, max_duration_days: 90, purposes: ['Financial reporting', 'Fraud analytics'], approvers: ['urn:li:corpuser:emeka'] },
};

export const UI_CONFIG = {
    user: { urn: 'urn:li:corpuser:ana', display_name: 'Ana Analyst' },
    features: { context: true, governance: true },
    slack_enabled: true,
    teams_enabled: false,
    embed_allowed_origins: ['https://acme.looker.com'],
};

export const flush = () => new Promise((r) => setTimeout(r, 0));
