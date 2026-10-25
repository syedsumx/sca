/** Types for the Active Metadata (consumer) API — mirrors UX_ACTIVE_METADATA_SPEC.md §6.7. */

export type TrustStatus = string | null;

export interface TrendingItem {
    urn: string;
    name: string;
    type: string;
    platform: string | null;
    description: string | null;
    score: number;
    views_7d: number;
    unique_viewers_7d: number;
    bookmarks: number;
    trend: 'up' | 'new' | 'steady';
    certification: TrustStatus;
    quality_status: TrustStatus;
}

export interface Enrichment {
    views_7d: number;
    bookmarks: number;
    bookmarked: boolean;
    following: boolean;
    threads_open: number;
    certification: TrustStatus;
    quality_status: TrustStatus;
}

export type EnrichmentMap = Record<string, Enrichment>;

export interface Bookmark {
    urn: string;
    note: string | null;
    created_at: string;
}

export interface RecentItem {
    urn: string;
    last_viewed_at: string;
}

export interface Follow {
    urn: string;
    created_at: string;
}

export interface Comment {
    id: string;
    thread_id: string;
    author: string;
    body_md: string;
    created_at: string;
    edited_at: string | null;
    mentions: string[];
    reactions: Record<string, string[]>;
}

export interface Thread {
    id: string;
    entity_urn: string;
    field_path: string | null;
    status: 'OPEN' | 'RESOLVED';
    created_by: string;
    created_at: string;
    comments: Comment[];
}

export interface ActivityItem {
    id: string;
    type: string;
    actor: string;
    entity_urn: string;
    entity_name: string;
    summary: string;
    created_at: string;
}

export type AccessStatus = 'GRANTED' | 'PENDING' | 'NONE' | 'EXPIRED' | 'REJECTED';

export interface RequestPolicy {
    auto_approve: boolean;
    max_duration_days: number;
    purposes: string[];
    approvers: string[];
}

export interface ProductCard {
    urn: string;
    name: string;
    description: string | null;
    domain: { urn: string; name: string } | null;
    owners: string[];
    tags: string[];
    certification: TrustStatus;
    quality_status: TrustStatus;
    assets_count: number;
    consumers: number;
    sla: { freshness: string | null; availability: string | null };
    access: { status: AccessStatus; request_id: string | null; expires_at: string | null };
    request_policy: RequestPolicy;
}

export interface ProductAsset {
    urn: string;
    name: string;
    type: string;
    platform: string | null;
}

export interface ProductDetail extends ProductCard {
    assets: ProductAsset[];
    readme_md: string;
    subscribed: boolean;
}

export type AccessRequestState = 'PENDING' | 'APPROVED' | 'REJECTED' | 'CANCELLED' | 'EXPIRED' | 'REVOKED';

export interface AccessRequestHistory {
    at: string;
    actor: string;
    from_state: string | null;
    to_state: string;
    comment: string | null;
}

export interface AccessRequest {
    id: string;
    product_urn: string;
    product_name: string;
    requester: string;
    purpose: string;
    justification: string;
    duration_days: number;
    state: AccessRequestState;
    auto_approved: boolean;
    approvers: string[];
    decided_by: string | null;
    decided_at: string | null;
    expires_at: string | null;
    created_at: string;
    history: AccessRequestHistory[];
}

export interface AccessRequestCreate {
    product_urn: string;
    purpose: string;
    justification: string;
    duration_days: number;
}

export type AccessRequestAction = 'approve' | 'reject' | 'cancel' | 'revoke';

export interface AttributeRule {
    attr: string;
    op: string;
    value: unknown;
}

export interface PersonaScope {
    domains: string[];
    data_products: string[];
    platforms: string[];
    tags: string[];
    glossary_terms: string[];
    asset_urns: string[];
}

export interface Persona {
    id: string;
    name: string;
    description: string;
    color: string;
    members: { users: string[]; groups: string[]; attribute_rules: AttributeRule[] };
    scope: PersonaScope;
    permissions: string[];
    enabled: boolean;
    updated_at: string;
}

export type PersonaInput = Omit<Persona, 'id' | 'updated_at'> & { id?: string };

export type PermissionBundles = Record<string, string[]>;

export interface PersonaPreview {
    members_count: number;
    sample_members: string[];
    assets_in_scope: number;
    sample_assets: { urn: string; name: string }[];
}

export type MaskStrategy = 'redact' | 'partial' | 'hash' | 'null';

export interface Purpose {
    id: string;
    name: string;
    description: string;
    tags: string[];
    allowed: { users: string[]; groups: string[]; personas: string[] };
    deny: string[];
    mask: { enabled: boolean; strategy: MaskStrategy };
    enabled: boolean;
    updated_at: string;
}

export type PurposeInput = Omit<Purpose, 'id' | 'updated_at'> & { id?: string };

export interface CompiledPolicy {
    id: string;
    description?: string;
    effect: 'allow' | 'deny';
    priority?: number;
    actions: string[];
    condition?: unknown;
    obligations?: unknown[];
    source?: string;
}

export type FindingType =
    | 'TERM_SUGGESTION'
    | 'TERM_LINK'
    | 'METRIC_DEFINITION'
    | 'DEFINITION_DRIFT'
    | 'DOC_STALENESS'
    | 'JOIN_RELATIONSHIP'
    | 'RUN_ANOMALY';

export interface Finding {
    id: string;
    type: FindingType;
    title: string;
    summary_md: string;
    confidence: number;
    evidence: { source: string; ref: string; snippet: string }[];
    entities: string[];
    proposed_changes: { kind: string; entity_urn: string; field_path: string | null; value: unknown }[];
    status: 'OPEN' | 'ACCEPTED' | 'DISMISSED';
    created_at: string;
}

export interface OntologyNode {
    id: string;
    label: string;
    kind: 'term' | 'column' | 'metric' | 'dataset';
}

export interface OntologyEdge {
    source: string;
    target: string;
    kind: string;
    weight: number;
}

export interface Ontology {
    nodes: OntologyNode[];
    edges: OntologyEdge[];
}

export interface SuggestedAction {
    label: string;
    kind: 'open' | 'follow' | 'request_access' | 'explain';
    urn: string;
}

export interface ChatResponse {
    conversation_id: string;
    reply_md: string;
    citations: string[];
    tool_calls: { name: string; summary: string }[];
    suggested_actions: SuggestedAction[];
}

export interface ExplainResponse {
    summary_md: string;
    likely_causes: { urn: string; name: string; reason: string; evidence: string[]; score: number }[];
    timeline: { at: string; event: string; urn: string }[];
    used_llm: boolean;
}

export interface MarkdownDocResponse {
    markdown: string;
    workflow_id: string;
}

export interface NotifySettings {
    channels: {
        slack: { enabled: boolean; user_id: string | null };
        teams: { enabled: boolean; webhook_url: string | null };
        email: { enabled: boolean };
        in_app: { enabled: boolean };
    };
    events: Record<string, boolean>;
}

export interface InboxItem {
    id: string;
    type: string;
    title: string;
    body: string;
    entity_urn: string | null;
    created_at: string;
    read_at: string | null;
    action_url: string | null;
}

export type EmbedScope = 'card' | 'quality' | 'lineage';

export interface EmbedTokenRequest {
    urn: string;
    scopes: EmbedScope[];
    ttl_seconds: number;
    audience_origin?: string;
}

export interface EmbedToken {
    token: string;
    expires_at: string;
    snippet_html: string;
    iframe_url: string;
}

export interface UiConfig {
    user: { urn: string; display_name: string };
    features: Record<string, boolean>;
    slack_enabled: boolean;
    teams_enabled: boolean;
    embed_allowed_origins: string[];
}

// ------------------------------------------------------------- DataHub GraphQL search

export interface SearchEntity {
    urn: string;
    type: string;
    name: string;
    platform: string | null;
    description: string | null;
}

export interface FacetAggregation {
    value: string;
    count: number;
    label: string;
}

export interface Facet {
    field: string;
    displayName: string;
    aggregations: FacetAggregation[];
}

export interface SearchResponse {
    total: number;
    results: SearchEntity[];
    facets: Facet[];
}
