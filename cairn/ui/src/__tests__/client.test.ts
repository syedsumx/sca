import { CairnApiError, CairnClient, buildQuery, errorMessageFromBody } from '../api/client';

function mockFetch(status: number, body: unknown) {
    const calls: Array<{ url: string; init: RequestInit }> = [];
    const impl = (async (url: string, init: RequestInit) => {
        calls.push({ url, init });
        return new Response(body === undefined ? null : typeof body === 'string' ? body : JSON.stringify(body), {
            status,
            headers: { 'Content-Type': 'application/json' },
        });
    }) as unknown as typeof fetch;
    return { impl, calls };
}

describe('CairnClient', () => {
    it('builds URLs, encodes URNs and sends credentials', async () => {
        const { impl, calls } = mockFetch(200, { dataset_urn: 'x', status: 'PASS', monitors: [], open_incidents: 0 });
        const client = new CairnClient({ baseUrl: '/cairn/api/', fetchImpl: impl });
        const urn = 'urn:li:dataset:(urn:li:dataPlatform:snowflake,db.s.t,PROD)';
        const res = await client.getDatasetHealth(urn);
        expect(res.status).toBe('PASS');
        expect(calls[0].url).toBe(`/cairn/api/quality/datasets/${encodeURIComponent(urn)}/health`);
        expect(calls[0].init.credentials).toBe('include');
        expect(calls[0].init.method).toBe('GET');
        expect((calls[0].init.headers as Record<string, string>).Authorization).toBeUndefined();
    });

    it('adds bearer token from a provider function and JSON body', async () => {
        const { impl, calls } = mockFetch(200, { id: 'wf1' });
        const client = new CairnClient({ fetchImpl: impl, token: () => 'pat-123' });
        await client.transitionWorkflow('wf1', 'approve', 'lgtm');
        const headers = calls[0].init.headers as Record<string, string>;
        expect(calls[0].url).toBe('/cairn/api/governance/workflows/wf1/approve');
        expect(headers.Authorization).toBe('Bearer pat-123');
        expect(headers['Content-Type']).toBe('application/json');
        expect(JSON.parse(calls[0].init.body as string)).toEqual({ comment: 'lgtm' });
    });

    it('serializes query params and skips empty ones', async () => {
        const { impl, calls } = mockFetch(200, []);
        const client = new CairnClient({ fetchImpl: impl });
        await client.listWorkflows({ state: 'IN_REVIEW' });
        expect(calls[0].url).toBe('/cairn/api/governance/workflows?state=IN_REVIEW');
        await client.listEvaluations('m 1', 50);
        expect(calls[1].url).toBe('/cairn/api/quality/monitors/m%201/evaluations?limit=50');
    });

    it('raises CairnApiError with FastAPI detail message', async () => {
        const { impl } = mockFetch(403, { detail: 'Denied by policy deny-pii' });
        const client = new CairnClient({ fetchImpl: impl });
        await expect(client.listMonitors()).rejects.toMatchObject({
            name: 'CairnApiError',
            status: 403,
            message: 'Denied by policy deny-pii',
        });
        try {
            await client.listMonitors();
        } catch (e) {
            expect(e).toBeInstanceOf(CairnApiError);
            expect((e as CairnApiError).isForbidden).toBe(true);
        }
    });

    it('handles empty 204 bodies', async () => {
        const { impl } = mockFetch(204, undefined);
        const client = new CairnClient({ fetchImpl: impl });
        await expect(client.deleteMonitor('m1')).resolves.toBeUndefined();
    });

    it('wraps network failures but propagates aborts', async () => {
        const failing = (async () => {
            throw new TypeError('Failed to fetch');
        }) as unknown as typeof fetch;
        await expect(new CairnClient({ fetchImpl: failing }).listAgents()).rejects.toMatchObject({ status: 0 });

        const aborting = (async () => {
            throw new DOMException('aborted', 'AbortError');
        }) as unknown as typeof fetch;
        await expect(new CairnClient({ fetchImpl: aborting }).listAgents()).rejects.toMatchObject({ name: 'AbortError' });
    });
});

describe('helpers', () => {
    it('errorMessageFromBody handles validation arrays and strings', () => {
        expect(errorMessageFromBody({ detail: [{ msg: 'a' }, { msg: 'b' }] }, 'x')).toBe('a; b');
        expect(errorMessageFromBody('  boom ', 'x')).toBe('boom');
        expect(errorMessageFromBody(null, 'fallback')).toBe('fallback');
    });

    it('buildQuery', () => {
        expect(buildQuery()).toBe('');
        expect(buildQuery({ a: 1, b: undefined, c: '', d: true })).toBe('?a=1&d=true');
    });
});
