import { CairnApiError, CairnClient, CairnClientOptions, RequestOptions } from '../../api/client';
import type { CompiledPolicy } from './types';
import type {
    AIAssessment,
    AISystem,
    AISystemInput,
    AISystemsReport,
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
    ExtensionInstallResult,
    ExtensionSection,
    ExplainResponse,
    Facet,
    Finding,
    Follow,
    InboxItem,
    InstalledExtension,
    LicenceState,
    MarkdownDocResponse,
    NotifySettings,
    Ontology,
    PermissionBundles,
    Persona,
    PersonaInput,
    PersonaPreview,
    ProductCard,
    ProductDetail,
    PublishOptions,
    PublishPlan,
    PublishTarget,
    Purpose,
    PurposeInput,
    RecentItem,
    SearchEntity,
    SearchResponse,
    SemanticHit,
    FormAssignResult,
    FormProgress,
    FormSpec,
    FormSummary,
    FormTask,
    FormTaskDetail,
    BulkApplyResult,
    BulkBatch,
    BulkPreview,
    TagSyncResult,
    Contract,
    Automation,
    ContextDefinition,
    DefinitionEdits,
    PastAnswer,
    CoverageFacet,
    CostTotal,
    AiAgent,
    AgentScope,
    AskResult,
    DataResult,
    StewardProposal,
    StewardRun,
    ReportAsset,
    ReportSnapshot,
    AutomationPreview,
    AutomationRun,
    AutomationSpec,
    TagSyncTarget,
    Thread,
    TrendingItem,
    UiConfig,
} from './types';

export const DEFAULT_GRAPHQL_URL = '/api/v2/graphql';

export interface ConsumerClientOptions extends CairnClientOptions {
    /** Cairn Core GraphQL endpoint (same-origin through cairn-web). */
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

/** Orderings cairn-api accepts for GET /marketplace/products. */
export type ProductSort = 'relevance' | 'consumers' | 'name';
/** `access` filter values for GET /marketplace/products. */
export type ProductAccessFilter = 'mine' | 'requestable';

/** Typed client for the Cairn consumer endpoints plus a tiny Cairn Core GraphQL helper. */
export class ConsumerClient extends CairnClient {
    readonly graphqlUrl: string;

    private readonly gqlFetch: typeof fetch;

    private readonly gqlToken?: CairnClientOptions['token'];

    constructor(options: ConsumerClientOptions = {}) {
        super(options);
        this.graphqlUrl = options.graphqlUrl ?? DEFAULT_GRAPHQL_URL;
        const f = options.fetchImpl ?? (typeof fetch !== 'undefined' ? fetch : undefined);
        if (!f) throw new Error('No fetch implementation available');
        this.gqlFetch = f.bind(globalThis) as typeof fetch;
        this.gqlToken = options.token;
    }

    /** POST a GraphQL query to Cairn Core; throws GraphQLError on `errors`. */
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
            throw new CairnApiError(`Network error calling GraphQL: ${(e as Error).message}`, 0, this.graphqlUrl);
        }
        const body = (await response.json().catch(() => ({}))) as { data?: T; errors?: { message?: string }[] };
        if (!response.ok) {
            throw new CairnApiError(`GraphQL HTTP ${response.status}`, response.status, this.graphqlUrl, body);
        }
        if (body.errors && body.errors.length) {
            throw new GraphQLError(body.errors.map((e) => e.message ?? 'error').join('; '), body.errors);
        }
        return body.data as T;
    }

    // --- Cairn Core search ------------------------------------------------------------------

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

    // --- semantic search -------------------------------------------------------------------

    /** Meaning-based matches for `query`, leaving out `exclude` (the keyword hits already shown). */
    semanticRelated(query: string, exclude: string[], limit = 5, minScore = 0, opts?: RequestOptions) {
        return this.request<{ results: SemanticHit[] }>(
            'POST',
            '/ai/search/semantic',
            { query, limit, exclude: exclude.slice(0, 200), min_score: minScore },
            opts,
        );
    }

    similarAssets(urn: string, limit = 5, opts: RequestOptions = {}) {
        return this.request<{ urn: string; results: SemanticHit[] }>('GET', '/ai/similar', undefined, {
            ...opts,
            query: { ...(opts.query ?? {}), urn, limit },
        });
    }

    // --- UI bootstrap ----------------------------------------------------------------------

    getUiConfig(opts?: RequestOptions) {
        return this.request<UiConfig>('GET', '/ui/config', undefined, opts);
    }

    // --- sharing data products outside the company -----------------------------------------

    planPublication(urn: string, target: PublishTarget, options: PublishOptions) {
        return this.request<PublishPlan>('POST', `/marketplace/products/${enc(urn)}/publish/${target}/plan`, options);
    }

    publishProduct(urn: string, target: PublishTarget, options: PublishOptions, connection?: string) {
        return this.request<PublishPlan>('POST', `/marketplace/products/${enc(urn)}/publish/${target}`, {
            options,
            connection: connection || null,
        });
    }

    // --- AI systems ------------------------------------------------------------------------

    aiAssessment(opts?: RequestOptions) {
        return this.request<AIAssessment>('GET', '/governance/ai-systems/assessment', undefined, opts);
    }

    listAISystems(opts?: RequestOptions) {
        return this.request<AISystem[]>('GET', '/governance/ai-systems', undefined, opts);
    }

    saveAISystem(body: AISystemInput, id?: string) {
        return id
            ? this.request<AISystem>('PUT', `/governance/ai-systems/${enc(id)}`, body)
            : this.request<AISystem>('POST', '/governance/ai-systems', body);
    }

    submitAISystem(id: string) {
        return this.request<AISystem>('POST', `/governance/ai-systems/${enc(id)}/submit`, {});
    }

    decideAISystem(id: string, approve: boolean, note: string) {
        return this.request<AISystem>('POST', `/governance/ai-systems/${enc(id)}/decision`, { approve, note });
    }

    aiSystemsReport(opts?: RequestOptions) {
        return this.request<AISystemsReport>('GET', '/governance/ai-systems/report', undefined, opts);
    }

    aiSystemsCsv() {
        return this.request<string>('GET', '/governance/ai-systems/report', undefined, { query: { format: 'csv' } });
    }

    // --- extensions ------------------------------------------------------------------------

    extensionSections(opts?: RequestOptions) {
        return this.request<ExtensionSection[]>('GET', '/extensions/ui', undefined, opts);
    }

    /** Agents acting for the caller (OAuth clients they connected, key agents they own). */
    myAiAgents(opts?: RequestOptions) {
        return this.request<AiAgent[]>('GET', '/me/agents', undefined, opts);
    }

    disconnectAiAgent(id: string) {
        return this.request<{ revoked: number }>('DELETE', `/me/agents/${enc(id)}`);
    }

    listAiAgents(opts?: RequestOptions) {
        return this.request<AiAgent[]>('GET', '/admin/agents', undefined, opts);
    }

    /** An agent for an automation; the answer's key is shown once. */
    createAiAgent(name: string, scopes: AgentScope[], owner?: string) {
        return this.request<AiAgent>('POST', '/admin/agents', { name, scopes, owner: owner || undefined });
    }

    agentSettings(opts?: RequestOptions) {
        return this.request<{ paused: boolean; changed_by?: string; changed_ms?: number }>(
            'GET',
            '/admin/agents/settings',
            undefined,
            opts,
        );
    }

    /** The kill switch: pause (or resume) every AI agent at once. */
    pauseAgents(paused: boolean) {
        return this.request<{ paused: boolean }>('POST', '/admin/agents/pause', { paused });
    }

    updateAiAgent(
        id: string,
        patch: {
            scopes?: AgentScope[];
            enabled?: boolean;
            ai_system?: string;
            autonomy?: 'suggest' | 'auto';
            auto_accept_at?: number;
            per_minute?: number;
            daily_requests?: number;
        },
    ) {
        return this.request<AiAgent>('PATCH', `/admin/agents/${enc(id)}`, patch);
    }

    rotateAiAgentKey(id: string) {
        return this.request<AiAgent>('POST', `/admin/agents/${enc(id)}/rotate`);
    }

    deleteAiAgent(id: string) {
        return this.request<void>('DELETE', `/admin/agents/${enc(id)}`);
    }

    listExtensions(opts?: RequestOptions) {
        return this.request<InstalledExtension[]>('GET', '/admin/extensions', undefined, opts);
    }

    installExtension(manifest: string) {
        return this.request<ExtensionInstallResult>('PUT', '/admin/extensions', manifest, {
            contentType: 'application/yaml',
        });
    }

    setExtensionEnabled(name: string, enabled: boolean) {
        return this.request<InstalledExtension>(
            'POST',
            `/admin/extensions/${enc(name)}/${enabled ? 'enable' : 'disable'}`,
        );
    }

    removeExtension(name: string) {
        return this.request<void>('DELETE', `/admin/extensions/${enc(name)}`);
    }

    // --- licence ---------------------------------------------------------------------------

    getLicence(opts?: RequestOptions) {
        return this.request<LicenceState>('GET', '/admin/licence', undefined, opts);
    }

    installLicence(key: string) {
        return this.request<LicenceState>('PUT', '/admin/licence', { key });
    }

    removeLicence() {
        return this.request<LicenceState>('DELETE', '/admin/licence');
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

    // --- compliance forms -------------------------------------------------------------------

    formTasks(state: FormTask['state'] = 'PENDING', opts: RequestOptions = {}) {
        return this.request<FormTask[]>('GET', '/governance/forms/tasks', undefined, { ...opts, query: { state } });
    }

    formTask(formId: string, urn: string, opts?: RequestOptions) {
        return this.request<FormTaskDetail>('GET', `/governance/forms/${enc(formId)}/tasks/${enc(urn)}`, undefined, opts);
    }

    submitFormTask(formId: string, urn: string, answers: Record<string, unknown>, verify = false) {
        return this.request<{ state: FormTask['state'] }>('POST', `/governance/forms/${enc(formId)}/tasks/${enc(urn)}`, {
            answers,
            verify,
        });
    }

    listForms(opts?: RequestOptions) {
        return this.request<FormSummary[]>('GET', '/governance/forms', undefined, opts);
    }

    createForm(spec: FormSpec) {
        return this.request<FormSummary>('POST', '/governance/forms', spec);
    }

    assignForm(formId: string, dryRun = false) {
        return this.request<FormAssignResult>('POST', `/governance/forms/${enc(formId)}/assign`, undefined, {
            query: { dry_run: dryRun },
        });
    }

    formProgress(formId: string, opts?: RequestOptions) {
        return this.request<FormProgress>('GET', `/governance/forms/${enc(formId)}/progress`, undefined, opts);
    }

    archiveForm(formId: string) {
        return this.request<void>('DELETE', `/governance/forms/${enc(formId)}`);
    }

    /** CSV of the matching datasets and their columns, ready to edit in a spreadsheet. */
    bulkExport(query: string, opts?: RequestOptions) {
        return this.request<string>('GET', '/bulk/export', undefined, { ...opts, query: { query: query || '*' } });
    }

    bulkImport(csv: string) {
        return this.request<BulkPreview>('POST', '/bulk/import', csv, { contentType: 'text/csv' });
    }

    bulkApply(batchId: string) {
        return this.request<BulkApplyResult>('POST', `/bulk/${enc(batchId)}/apply`);
    }

    bulkUndo(batchId: string) {
        return this.request<{ batch_id: string; restored: number }>('POST', `/bulk/${enc(batchId)}/undo`);
    }

    bulkHistory(opts?: RequestOptions) {
        return this.request<BulkBatch[]>('GET', '/bulk', undefined, opts);
    }

    tagSyncTargets(opts?: RequestOptions) {
        return this.request<TagSyncTarget[]>('GET', '/governance/tag-sync', undefined, opts);
    }

    tagSync(targetId: string, dryRun: boolean) {
        return this.request<TagSyncResult>('POST', `/governance/tag-sync/${enc(targetId)}/sync`, { dry_run: dryRun });
    }

    contract(dataset: string, opts?: RequestOptions) {
        return this.request<Contract>('GET', '/quality/contracts/one', undefined, { ...opts, query: { dataset } });
    }

    listAutomations(opts?: RequestOptions) {
        return this.request<Automation[]>('GET', '/governance/automations', undefined, opts);
    }

    previewAutomation(spec: AutomationSpec) {
        return this.request<AutomationPreview>('POST', '/governance/automations/preview', spec);
    }

    createAutomation(spec: AutomationSpec) {
        return this.request<Automation>('POST', '/governance/automations', spec);
    }

    setAutomationEnabled(id: string, enabled: boolean) {
        return this.request<Automation>('POST', `/governance/automations/${enc(id)}/enabled`, { enabled });
    }

    deleteAutomation(id: string) {
        return this.request<void>('DELETE', `/governance/automations/${enc(id)}`);
    }

    runAutomation(id: string) {
        return this.request<AutomationRun>('POST', `/governance/automations/${enc(id)}/run`);
    }

    automationRuns(id: string, opts?: RequestOptions) {
        return this.request<AutomationRun[]>('GET', `/governance/automations/${enc(id)}/runs`, undefined, opts);
    }

    undoAutomationRun(runId: string) {
        return this.request<AutomationRun>('POST', `/governance/automations/runs/${enc(runId)}/undo`);
    }

    report(opts?: RequestOptions) {
        return this.request<ReportSnapshot>('GET', '/reports', undefined, opts);
    }

    refreshReport() {
        return this.request<ReportSnapshot>('POST', '/reports/refresh');
    }

    reportCsv() {
        return this.request<string>('GET', '/reports/coverage.csv');
    }

    reportAssets(q: { missing?: CoverageFacet; domain?: string | null; platform?: string }, opts?: RequestOptions) {
        return this.request<ReportAsset[]>('GET', '/reports/assets', undefined, {
            ...opts,
            query: { missing: q.missing, platform: q.platform, domain: q.domain === null ? 'none' : q.domain },
        });
    }

    idleTables(days = 30, opts?: RequestOptions) {
        return this.request<CostTotal[]>('GET', '/reports/costs/idle', undefined, { ...opts, query: { days } });
    }

    expensiveTables(days = 30, opts?: RequestOptions) {
        return this.request<CostTotal[]>('GET', '/reports/costs/expensive', undefined, { ...opts, query: { days } });
    }

    proposeDeprecation(urn: string, reason: string) {
        return this.request<{ proposed: boolean }>('POST', '/reports/costs/propose-deprecation', { urn, reason });
    }

    previewData(urn: string, opts?: RequestOptions) {
        return this.request<DataResult>('GET', '/data/preview', undefined, { ...opts, query: { urn } });
    }

    runQuery(sql: string, urn: string, limit = 200) {
        return this.request<DataResult>('POST', '/data/query', { sql, urn, limit });
    }

    /** A question about the data answered with governed SQL over the given datasets (run as the caller). */
    askData(question: string, urns: string[] = [], run = true) {
        return this.request<AskResult>('POST', '/data/ask', { question, urns, run });
    }

    runSteward(limit = 100) {
        return this.request<StewardRun>('POST', '/ai/steward/run', { limit });
    }

    stewardProposals(kind?: string, opts?: RequestOptions) {
        return this.request<StewardProposal[]>('GET', '/ai/steward/proposals', undefined, { ...opts, query: { kind } });
    }

    decideProposals(ids: string[], action: 'accept' | 'reject', edits: Record<string, unknown> = {}) {
        return this.request<{ decided: number; batch_id: string | null }>('POST', '/ai/steward/decide', { ids, action, edits });
    }

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

    actOnFinding(id: string, action: 'accept' | 'dismiss', edits: DefinitionEdits = {}) {
        return this.request<Finding>('POST', `/context/findings/${enc(id)}/${action}`, edits);
    }

    contextDefinitions(opts?: RequestOptions) {
        return this.request<ContextDefinition[]>('GET', '/context/definitions', undefined, opts);
    }

    exportDefinitions(format: 'snowflake' | 'databricks', name: string, source?: string) {
        return this.request<string>('GET', '/context/definitions/export', undefined, { query: { format, name, source } });
    }

    pastAnswers(question: string, opts?: RequestOptions) {
        return this.request<PastAnswer[]>('GET', '/context/answers', undefined, { ...opts, query: { question } });
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
query cairnConsumerSearch($input: SearchAcrossEntitiesInput!) {
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
