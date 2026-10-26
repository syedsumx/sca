import { DheApiError, DheClient, DheClientOptions, RequestOptions } from '../../api/client';
import type { CompiledPolicy } from './types';
import type {
    AccessRequest,
    AccessRequestAction,
    AccessRequestCreate,
    ActivityItem,
    Bookmark,
    ChatResponse,
    Comment,
    EmbedToken,
    EmbedTokenRequest,
    EnrichmentMap,
    ExplainResponse,
    Facet,
    Finding,
    Follow,
    InboxItem,
    MarkdownDocResponse,
    NotifySettings,
    Ontology,
    PermissionBundles,
    Persona,
    PersonaInput,
    PersonaPreview,
    ProductCard,
    ProductDetail,
    Purpose,
    PurposeInput,
    RecentItem,
    SearchEntity,
    SearchResponse,
    Thread,
    TrendingItem,
    UiConfig,
} from './types';

export const DEFAULT_GRAPHQL_URL = '/api/v2/graphql';

export interface ConsumerClientOptions extends DheClientOptions {
    /** DataHub GraphQL endpoint (same-origin through datahub-frontend). */
    graphqlUrl?: string;
}

export class GraphQLError extends Error {
    readonly errors: unknown[];

    constructor(message: string, errors: unknown[]) {
        super(message);
        this.name = 'GraphQLError';
        this.errors = errors;
    }
}

const enc = encodeURIComponent;

/** Orderings dhe-api accepts for GET /marketplace/products. */
export type ProductSort = 'relevance' | 'consumers' | 'name';
/** `access` filter values for GET /marketplace/products. */
export type ProductAccessFilter = 'mine' | 'requestable';

/** Typed client for the DHE consumer endpoints plus a tiny DataHub GraphQL helper. */
export class ConsumerClient extends DheClient {
    readonly graphqlUrl: string;

    private readonly gqlFetch: typeof fetch;

    private readonly gqlToken?: DheClientOptions['token'];

    constructor(options: ConsumerClientOptions = {}) {
        super(options);
        this.graphqlUrl = options.graphqlUrl ?? DEFAULT_GRAPHQL_URL;
        const f = options.fetchImpl ?? (typeof fetch !== 'undefined' ? fetch : undefined);
        if (!f) throw new Error('No fetch implementation available');
        this.gqlFetch = f.bind(globalThis) as typeof fetch;
        this.gqlToken = options.token;
    }

    /** POST a GraphQL query to DataHub; throws GraphQLError on `errors`. */
    async gql<T>(query: string, variables: Record<string, unknown> = {}, signal?: AbortSignal): Promise<T> {
        const headers: Record<string, string> = { 'Content-Type': 'application/json', Accept: 'application/json' };
        const token = typeof this.gqlToken === 'function' ? this.gqlToken() : this.gqlToken;
        if (token) headers.Authorization = `Bearer ${token}`;
        let response: Response;
        try {
            response = await this.gqlFetch(this.graphqlUrl, {
                method: 'POST',
                headers,
                credentials: 'include',
                body: JSON.stringify({ query, variables }),
                signal,
            });
        } catch (e) {
            if (e instanceof DOMException && e.name === 'AbortError') throw e;
            throw new DheApiError(`Network error calling GraphQL: ${(e as Error).message}`, 0, this.graphqlUrl);
        }
        const body = (await response.json().catch(() => ({}))) as { data?: T; errors?: { message?: string }[] };
        if (!response.ok) {
            throw new DheApiError(`GraphQL HTTP ${response.status}`, response.status, this.graphqlUrl, body);
        }
        if (body.errors && body.errors.length) {
            throw new GraphQLError(body.errors.map((e) => e.message ?? 'error').join('; '), body.errors);
        }
        return body.data as T;
    }

    // --- DataHub search ------------------------------------------------------------------

    async search(
        params: { query: string; filters: Record<string, string[]>; start?: number; count?: number },
        signal?: AbortSignal,
    ): Promise<SearchResponse> {
        const { types, orFilters } = filtersToInput(params.filters);
        const data = await this.gql<{ searchAcrossEntities: RawSearch | null }>(
            SEARCH_QUERY,
            {
                input: {
                    query: params.query || '*',
                    types: types.length ? types : undefined,
                    start: params.start ?? 0,
                    count: params.count ?? 20,
                    orFilters,
                },
            },
            signal,
        );
        return parseSearch(data.searchAcrossEntities);
    }

    // --- UI bootstrap ----------------------------------------------------------------------

    getUiConfig(opts?: RequestOptions) {
        return this.request<UiConfig>('GET', '/ui/config', undefined, opts);
    }

    // --- collaboration ---------------------------------------------------------------------

    trending(query: { entity_type?: string; domain?: string; limit?: number } = {}, opts: RequestOptions = {}) {
        return this.request<TrendingItem[]>('GET', '/collab/trending', undefined, { ...opts, query });
    }

    enrich(urns: string[], opts?: RequestOptions) {
        return this.request<EnrichmentMap>('POST', '/collab/enrich', { urns }, opts);
    }

    listBookmarks(opts?: RequestOptions) {
        return this.request<Bookmark[]>('GET', '/collab/bookmarks', undefined, opts);
    }

    addBookmark(urn: string, note?: string) {
        return this.request<Bookmark>('PUT', `/collab/bookmarks/${enc(urn)}`, { note: note ?? null });
    }

    removeBookmark(urn: string) {
        return this.request<void>('DELETE', `/collab/bookmarks/${enc(urn)}`);
    }

    listFollows(opts?: RequestOptions) {
        return this.request<Follow[]>('GET', '/collab/follows', undefined, opts);
    }

    follow(urn: string) {
        return this.request<Follow>('PUT', `/collab/follows/${enc(urn)}`, {});
    }

    unfollow(urn: string) {
        return this.request<void>('DELETE', `/collab/follows/${enc(urn)}`);
    }

    recordView(urn: string) {
        return this.request<void>('POST', '/collab/views', { urn });
    }

    recent(limit = 10, opts: RequestOptions = {}) {
        return this.request<RecentItem[]>('GET', '/collab/recent', undefined, { ...opts, query: { limit } });
    }

    activity(limit = 20, opts: RequestOptions = {}) {
        return this.request<ActivityItem[]>('GET', '/collab/activity', undefined, { ...opts, query: { limit } });
    }

    listThreads(entityUrn: string, opts: RequestOptions = {}) {
        return this.request<Thread[]>('GET', '/collab/threads', undefined, {
            ...opts,
            query: { entity_urn: entityUrn },
        });
    }

    createThread(entityUrn: string, bodyMd: string, fieldPath?: string | null) {
        return this.request<Thread>('POST', '/collab/threads', {
            entity_urn: entityUrn,
            field_path: fieldPath ?? null,
            body_md: bodyMd,
        });
    }

    addComment(threadId: string, bodyMd: string) {
        return this.request<Comment>('POST', `/collab/threads/${enc(threadId)}/comments`, { body_md: bodyMd });
    }

    setThreadStatus(threadId: string, action: 'resolve' | 'reopen') {
        return this.request<Thread>('POST', `/collab/threads/${enc(threadId)}/${action}`, {});
    }

    toggleReaction(commentId: string, emoji: string, add: boolean) {
        return this.request<Comment>(add ? 'POST' : 'DELETE', `/collab/comments/${enc(commentId)}/reactions`, {
            emoji,
        });
    }

    // --- marketplace -----------------------------------------------------------------------

    listProducts(
        query: { q?: string; domain?: string; certified?: boolean; access?: ProductAccessFilter; sort?: ProductSort } = {},
        opts: RequestOptions = {},
    ) {
        return this.request<ProductCard[]>('GET', '/marketplace/products', undefined, { ...opts, query });
    }

    getProduct(urn: string, opts?: RequestOptions) {
        return this.request<ProductDetail>('GET', `/marketplace/products/${enc(urn)}`, undefined, opts);
    }

    subscribe(urn: string, on: boolean) {
        return this.request<void>(on ? 'POST' : 'DELETE', `/marketplace/products/${enc(urn)}/subscription`, {});
    }

    createAccessRequest(input: AccessRequestCreate) {
        return this.request<AccessRequest>('POST', '/marketplace/access-requests', input);
    }

    listAccessRequests(query: { scope: 'mine' | 'inbox'; state?: string }, opts: RequestOptions = {}) {
        return this.request<AccessRequest[]>('GET', '/marketplace/access-requests', undefined, { ...opts, query });
    }

    actOnAccessRequest(id: string, action: AccessRequestAction, comment?: string) {
        return this.request<AccessRequest>('POST', `/marketplace/access-requests/${enc(id)}/${action}`, {
            comment: comment ?? null,
        });
    }

    // --- personas & purposes ---------------------------------------------------------------

    listPersonas(opts?: RequestOptions) {
        return this.request<Persona[]>('GET', '/governance/personas', undefined, opts);
    }

    savePersona(input: PersonaInput) {
        return input.id
            ? this.request<Persona>('PUT', `/governance/personas/${enc(input.id)}`, input)
            : this.request<Persona>('POST', '/governance/personas', input);
    }

    deletePersona(id: string) {
        return this.request<void>('DELETE', `/governance/personas/${enc(id)}`);
    }

    previewPersona(id: string) {
        return this.request<PersonaPreview>('POST', `/governance/personas/${enc(id)}/preview`, {});
    }

    permissionBundles(opts?: RequestOptions) {
        return this.request<PermissionBundles>('GET', '/governance/personas/permission-bundles', undefined, opts);
    }

    listPurposes(opts?: RequestOptions) {
        return this.request<Purpose[]>('GET', '/governance/purposes', undefined, opts);
    }

    savePurpose(input: PurposeInput) {
        return input.id
            ? this.request<Purpose>('PUT', `/governance/purposes/${enc(input.id)}`, input)
            : this.request<Purpose>('POST', '/governance/purposes', input);
    }

    deletePurpose(id: string) {
        return this.request<void>('DELETE', `/governance/purposes/${enc(id)}`);
    }

    compiledPolicies(opts: RequestOptions = {}) {
        return this.request<{ policies: CompiledPolicy[]; compiled?: CompiledPolicy[] }>(
            'GET',
            '/governance/policies',
            undefined,
            { ...opts, query: { include_compiled: true } },
        );
    }

    // --- context ---------------------------------------------------------------------------

    listFindings(query: { type?: string; status?: string; entity_urn?: string } = {}, opts: RequestOptions = {}) {
        return this.request<Finding[]>('GET', '/context/findings', undefined, { ...opts, query });
    }

    actOnFinding(id: string, action: 'accept' | 'dismiss') {
        return this.request<Finding>('POST', `/context/findings/${enc(id)}/${action}`, {});
    }

    ontology(query: { domain?: string; limit?: number } = {}, opts: RequestOptions = {}) {
        return this.request<Ontology>('GET', '/context/ontology', undefined, { ...opts, query });
    }

    // --- AI --------------------------------------------------------------------------------

    chat(message: string, conversationId?: string | null) {
        return this.request<ChatResponse>('POST', '/ai/chat', {
            message,
            conversation_id: conversationId ?? undefined,
        });
    }

    explainLineage(urn: string) {
        return this.request<ExplainResponse>('POST', '/ai/explain/lineage', { urn });
    }

    markdownDoc(urn: string) {
        return this.request<MarkdownDocResponse>('POST', '/ai/docs/markdown', { urn });
    }

    // --- notifications & embeds ------------------------------------------------------------

    getNotifySettings(opts?: RequestOptions) {
        return this.request<NotifySettings>('GET', '/notify/settings', undefined, opts);
    }

    saveNotifySettings(settings: NotifySettings) {
        return this.request<NotifySettings>('PUT', '/notify/settings', settings);
    }

    inbox(unreadOnly = false, opts: RequestOptions = {}) {
        return this.request<InboxItem[]>('GET', '/notify/inbox', undefined, {
            ...opts,
            query: { unread: unreadOnly || undefined },
        });
    }

    markRead(ids: string[]) {
        return this.request<void>('POST', '/notify/inbox/read', { ids });
    }

    createEmbedToken(input: EmbedTokenRequest) {
        return this.request<EmbedToken>('POST', '/embed/v1/tokens', input);
    }
}

// ------------------------------------------------------------- GraphQL search plumbing

const SUMMARY = `
  urn
  type
  ... on Dataset { name platform { name } properties { name description } }
  ... on Dashboard { platform { name } properties { name description } }
  ... on Chart { platform { name } properties { name description } }
  ... on DataJob { properties { name description } }
  ... on DataFlow { properties { name description } }
  ... on DataProduct { properties { name description } }
  ... on GlossaryTerm { properties { name description } }
  ... on Domain { properties { name description } }
`;

export const SEARCH_QUERY = `
query dheConsumerSearch($input: SearchAcrossEntitiesInput!) {
  searchAcrossEntities(input: $input) {
    total
    searchResults { entity { ${SUMMARY} } }
    facets {
      field
      displayName
      aggregations {
        value
        count
        entity { urn ... on Domain { properties { name } } ... on DataPlatform { name } }
      }
    }
  }
}`;

interface RawEntity {
    urn: string;
    type: string;
    name?: string;
    platform?: { name?: string } | null;
    properties?: { name?: string; description?: string } | null;
}

interface RawSearch {
    total?: number;
    searchResults?: { entity: RawEntity | null }[];
    facets?: {
        field: string;
        displayName?: string | null;
        aggregations?: {
            value: string;
            count: number;
            entity?: { urn?: string; name?: string; properties?: { name?: string } | null } | null;
        }[];
    }[];
}

/** The `_entityType` facet maps to `types`; everything else becomes an AND-ed orFilter clause. */
export function filtersToInput(filters: Record<string, string[]>): {
    types: string[];
    orFilters: { and: { field: string; values: string[] }[] }[] | undefined;
} {
    const types: string[] = [];
    const clauses: { field: string; values: string[] }[] = [];
    Object.entries(filters).forEach(([field, values]) => {
        if (!values.length) return;
        if (field === '_entityType' || field === 'type') types.push(...values);
        else clauses.push({ field, values });
    });
    return { types, orFilters: clauses.length ? [{ and: clauses }] : undefined };
}

export function entityName(e: RawEntity): string {
    return e.properties?.name || e.name || e.urn.split(',').slice(-2, -1)[0] || e.urn;
}

export function parseSearch(raw: RawSearch | null | undefined): SearchResponse {
    const results: SearchEntity[] = (raw?.searchResults ?? [])
        .map((r) => r.entity)
        .filter((e): e is RawEntity => !!e)
        .map((e) => ({
            urn: e.urn,
            type: e.type,
            name: entityName(e),
            platform: e.platform?.name ?? null,
            description: e.properties?.description ?? null,
        }));
    const facets: Facet[] = (raw?.facets ?? []).map((f) => ({
        field: f.field,
        displayName: f.displayName || f.field,
        aggregations: (f.aggregations ?? [])
            .filter((a) => a.count > 0)
            .map((a) => ({
                value: a.value,
                count: a.count,
                label: a.entity?.properties?.name || a.entity?.name || a.value.split(':').pop() || a.value,
            })),
    }));
    return { total: raw?.total ?? results.length, results, facets };
}
