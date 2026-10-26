import type {
    Agent,
    AgentPool,
    AgentPoolInput,
    AgentRun,
    AskResponse,
    DatasetHealth,
    DocGenerateResponse,
    EnrollmentToken,
    Evaluation,
    EvaluationFeedback,
    Monitor,
    MonitorInput,
    Policy,
    PolicyBundle,
    PolicyValidation,
    SemanticSearchResponse,
    SimulateRequest,
    SimulateResponse,
    Workflow,
    WorkflowAction,
    WorkflowCreate,
    WorkflowFilter,
} from './types';

export const DEFAULT_BASE_URL = '/cairn/api';

/** Error raised for any non-2xx response or network failure. */
export class CairnApiError extends Error {
    readonly status: number;

    readonly body: unknown;

    readonly path: string;

    constructor(message: string, status: number, path: string, body?: unknown) {
        super(message);
        this.name = 'CairnApiError';
        this.status = status;
        this.path = path;
        this.body = body;
    }

    get isUnauthorized(): boolean {
        return this.status === 401;
    }

    get isForbidden(): boolean {
        return this.status === 403;
    }

    get isNotFound(): boolean {
        return this.status === 404;
    }
}

export interface CairnClientOptions {
    baseUrl?: string;
    /** Optional bearer token (Cairn Core PAT). Without it the browser's same-origin cookies are used. */
    token?: string | (() => string | undefined);
    /** Injected for tests or SSR. Defaults to the global fetch. */
    fetchImpl?: typeof fetch;
}

export interface RequestOptions {
    signal?: AbortSignal;
    query?: Record<string, string | number | boolean | undefined | null>;
}

type Method = 'GET' | 'POST' | 'PUT' | 'DELETE';

/** Extracts a human-readable message from FastAPI-style error bodies ({detail: ...}). */
export function errorMessageFromBody(body: unknown, fallback: string): string {
    if (body && typeof body === 'object' && 'detail' in body) {
        const { detail } = body as { detail: unknown };
        if (typeof detail === 'string') return detail;
        if (Array.isArray(detail)) {
            return detail
                .map((d) => (d && typeof d === 'object' && 'msg' in d ? String((d as { msg: unknown }).msg) : String(d)))
                .join('; ');
        }
    }
    if (typeof body === 'string' && body.trim()) return body.trim().slice(0, 500);
    return fallback;
}

export function buildQuery(query?: RequestOptions['query']): string {
    if (!query) return '';
    const params = new URLSearchParams();
    Object.entries(query).forEach(([k, v]) => {
        if (v !== undefined && v !== null && v !== '') params.append(k, String(v));
    });
    const s = params.toString();
    return s ? `?${s}` : '';
}

export class CairnClient {
    private readonly baseUrl: string;

    private readonly token?: CairnClientOptions['token'];

    private readonly fetchImpl: typeof fetch;

    constructor(options: CairnClientOptions = {}) {
        this.baseUrl = (options.baseUrl ?? DEFAULT_BASE_URL).replace(/\/+$/, '');
        this.token = options.token;
        const f = options.fetchImpl ?? (typeof fetch !== 'undefined' ? fetch : undefined);
        if (!f) throw new Error('No fetch implementation available');
        // Bind so window.fetch is not invoked with a foreign `this`.
        this.fetchImpl = f.bind(globalThis) as typeof fetch;
    }

    async request<T>(method: Method, path: string, body?: unknown, opts: RequestOptions = {}): Promise<T> {
        const url = `${this.baseUrl}${path}${buildQuery(opts.query)}`;
        const headers: Record<string, string> = { Accept: 'application/json' };
        if (body !== undefined) headers['Content-Type'] = 'application/json';
        const token = typeof this.token === 'function' ? this.token() : this.token;
        if (token) headers.Authorization = `Bearer ${token}`;

        let response: Response;
        try {
            response = await this.fetchImpl(url, {
                method,
                headers,
                body: body === undefined ? undefined : JSON.stringify(body),
                credentials: 'include',
                signal: opts.signal,
            });
        } catch (e) {
            if (e instanceof DOMException && e.name === 'AbortError') throw e;
            throw new CairnApiError(`Network error calling ${path}: ${(e as Error).message}`, 0, path);
        }

        const text = await response.text();
        let parsed: unknown = undefined;
        if (text) {
            try {
                parsed = JSON.parse(text);
            } catch {
                parsed = text;
            }
        }
        if (!response.ok) {
            throw new CairnApiError(
                errorMessageFromBody(parsed, `${method} ${path} failed with HTTP ${response.status}`),
                response.status,
                path,
                parsed,
            );
        }
        return parsed as T;
    }

    // --- quality ---------------------------------------------------------------------------

    listMonitors(opts?: RequestOptions) {
        return this.request<Monitor[]>('GET', '/quality/monitors', undefined, opts);
    }

    getMonitor(id: string, opts?: RequestOptions) {
        return this.request<Monitor>('GET', `/quality/monitors/${encodeURIComponent(id)}`, undefined, opts);
    }

    createMonitor(input: MonitorInput, opts?: RequestOptions) {
        return this.request<Monitor>('POST', '/quality/monitors', input, opts);
    }

    updateMonitor(id: string, input: MonitorInput, opts?: RequestOptions) {
        return this.request<Monitor>('PUT', `/quality/monitors/${encodeURIComponent(id)}`, input, opts);
    }

    deleteMonitor(id: string, opts?: RequestOptions) {
        return this.request<void>('DELETE', `/quality/monitors/${encodeURIComponent(id)}`, undefined, opts);
    }

    runMonitor(id: string, opts?: RequestOptions) {
        return this.request<Evaluation>('POST', `/quality/monitors/${encodeURIComponent(id)}/run`, {}, opts);
    }

    listEvaluations(id: string, limit = 100, opts: RequestOptions = {}) {
        return this.request<Evaluation[]>('GET', `/quality/monitors/${encodeURIComponent(id)}/evaluations`, undefined, {
            ...opts,
            query: { ...opts.query, limit },
        });
    }

    sendFeedback(evaluationId: string, feedback: EvaluationFeedback, opts?: RequestOptions) {
        return this.request<Evaluation>(
            'POST',
            `/quality/evaluations/${encodeURIComponent(evaluationId)}/feedback`,
            { feedback },
            opts,
        );
    }

    getDatasetHealth(datasetUrn: string, opts?: RequestOptions) {
        return this.request<DatasetHealth>(
            'GET',
            `/quality/datasets/${encodeURIComponent(datasetUrn)}/health`,
            undefined,
            opts,
        );
    }

    // --- governance ------------------------------------------------------------------------

    simulate(req: SimulateRequest, opts?: RequestOptions) {
        return this.request<SimulateResponse>('POST', '/governance/simulate', req, opts);
    }

    getPolicies(opts?: RequestOptions) {
        return this.request<PolicyBundle>('GET', '/governance/policies', undefined, opts);
    }

    putPolicies(policies: Policy[], opts?: RequestOptions) {
        return this.request<PolicyBundle>('PUT', '/governance/policies', { policies }, opts);
    }

    validatePolicies(policies: unknown[], opts?: RequestOptions) {
        return this.request<PolicyValidation>('POST', '/governance/policies/validate', { policies }, opts);
    }

    listWorkflows(filter: WorkflowFilter = {}, opts: RequestOptions = {}) {
        return this.request<Workflow[]>('GET', '/governance/workflows', undefined, {
            ...opts,
            query: { ...opts.query, state: filter.state, kind: filter.kind },
        });
    }

    createWorkflow(input: WorkflowCreate, opts?: RequestOptions) {
        return this.request<Workflow>('POST', '/governance/workflows', input, opts);
    }

    transitionWorkflow(id: string, action: WorkflowAction, comment?: string, opts?: RequestOptions) {
        return this.request<Workflow>(
            'POST',
            `/governance/workflows/${encodeURIComponent(id)}/${action}`,
            comment ? { comment } : {},
            opts,
        );
    }

    // --- agents ----------------------------------------------------------------------------

    listAgents(opts?: RequestOptions) {
        return this.request<Agent[]>('GET', '/agents', undefined, opts);
    }

    listPools(opts?: RequestOptions) {
        return this.request<AgentPool[]>('GET', '/agents/pools', undefined, opts);
    }

    createPool(input: AgentPoolInput, opts?: RequestOptions) {
        return this.request<AgentPool>('POST', '/agents/pools', input, opts);
    }

    createEnrollmentToken(pool: string, opts?: RequestOptions) {
        return this.request<EnrollmentToken>(
            'POST',
            `/agents/pools/${encodeURIComponent(pool)}/enrollment-tokens`,
            {},
            opts,
        );
    }

    listRuns(opts?: RequestOptions) {
        return this.request<AgentRun[]>('GET', '/agents/runs', undefined, opts);
    }

    revokeAgent(agentId: string, opts?: RequestOptions) {
        return this.request<Agent>('POST', `/agents/${encodeURIComponent(agentId)}/revoke`, {}, opts);
    }

    // --- ai --------------------------------------------------------------------------------

    ask(question: string, opts?: RequestOptions) {
        return this.request<AskResponse>('POST', '/ai/ask', { question }, opts);
    }

    semanticSearch(query: string, limit = 10, opts?: RequestOptions) {
        return this.request<SemanticSearchResponse>('POST', '/ai/search/semantic', { query, limit }, opts);
    }

    generateDocs(datasetUrn: string, opts?: RequestOptions) {
        return this.request<DocGenerateResponse>('POST', '/ai/docs/generate', { dataset_urn: datasetUrn }, opts);
    }
}
