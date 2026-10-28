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
    /** Compliance forms: COMPLIANT when every assigned form is done, INCOMPLETE while any is open. */
    compliance?: 'COMPLIANT' | 'INCOMPLETE' | null;
    /** Data contract: MET, BROKEN, or PENDING while its monitors have not reported. */
    contract?: ContractStatus | null;
}

export type ContractStatus = 'MET' | 'BROKEN' | 'PENDING';

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
    contract?: ContractStatus | null;
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
    is_admin?: boolean;
    /** The caller holds MANAGE_INGESTION and the remote-agent gateway is configured. */
    can_manage_agents?: boolean;
    /** URL agents use to reach the gateway (shown in the enrolment command). */
    gateway_url?: string;
}

// ------------------------------------------------------------- compliance forms

export type FormQuestionType =
    | 'description'
    | 'owners'
    | 'glossary_terms'
    | 'tags'
    | 'domain'
    | 'structured_property'
    | 'text'
    | 'yes_no';

export interface FormQuestion {
    id: string;
    type: FormQuestionType;
    title: string;
    description?: string;
    required: boolean;
    allowed: string[];
    multiple: boolean;
    property_urn?: string | null;
    ownership_type?: string;
}

export interface FormSpec {
    name: string;
    description?: string;
    kind: 'completion' | 'verification';
    questions: FormQuestion[];
    select: Record<string, unknown>;
    assignees: { owners: boolean; ownership_types: string[]; users: string[] };
    due_days?: number | null;
    max_assets?: number;
}

export interface FormTally {
    assets: number;
    PENDING: number;
    COMPLETED: number;
    VERIFIED: number;
    done_pct: number;
}

export interface FormSummary {
    id: string;
    name: string;
    state: 'ACTIVE' | 'ARCHIVED';
    created_by: string;
    created_ms: number;
    spec: FormSpec;
    progress: FormTally;
}

export interface FormProgress {
    form_id: string;
    total: FormTally;
    by_domain: Record<string, FormTally>;
    by_assignee: Record<string, FormTally>;
}

export interface FormAssignResult {
    matched: number;
    assigned: string[];
    reassigned: string[];
    withdrawn: string[];
    without_assignee: string[];
    dry_run: boolean;
}

export interface FormTask {
    form_id: string;
    form_name: string;
    urn: string;
    state: 'PENDING' | 'COMPLETED' | 'VERIFIED';
    kind: 'completion' | 'verification';
    assigned_ms: number;
    due_ms: number | null;
}

export interface FormTaskDetail {
    form_id: string;
    form_name: string;
    urn: string;
    state: FormTask['state'];
    kind: FormTask['kind'];
    assignees: string[];
    questions: FormQuestion[];
    answers: Record<string, unknown>;
    completed_by: string | null;
    verified_by: string | null;
}

// ------------------------------------------------------------- semantic search

export interface SemanticHit {
    urn: string;
    name: string;
    type: string;
    platform?: string | null;
    description?: string | null;
    /** Cosine similarity, 0..1. */
    score: number;
    /** Query words found in the asset (search only, not "similar"). */
    matched?: string[];
}

// ------------------------------------------------------------- Cairn Core GraphQL search

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

export interface BulkChange {
    urn: string;
    field: string;
    what: 'description' | 'tags' | 'glossary_terms' | 'owners' | 'domain';
    from: string | string[] | null;
    to: string | string[] | null;
    add?: string[];
    remove?: string[];
    privilege?: string;
}

export interface BulkPreview {
    batch_id: string;
    rows: number;
    changes: BulkChange[];
    denied: BulkChange[];
    errors: string[];
}

export interface BulkApplyResult {
    batch_id: string;
    applied: number;
    already_current: number;
    aspects: number;
}

export interface BulkBatch {
    batch_id: string;
    state: 'PREVIEWED' | 'APPLIED' | 'UNDONE';
    rows: number;
    changes: number;
    created_ms: number;
    updated_ms: number;
}

export interface TagSyncTarget {
    id: string;
    dialect: 'snowflake' | 'databricks';
    platform: string;
    connection: string | null;
    dry_run: boolean;
    mappings: { label: string; tag: string; value: string }[];
    managed_tags: number;
    last_run: null | {
        at_ms: number;
        actor: string;
        dry_run: boolean;
        datasets: number;
        tagged_objects: number;
        planned: number;
        applied: number;
        failed: number;
        conflicts: number;
    };
}

export interface TagSyncOperation {
    op: 'SET' | 'UNSET';
    urn: string;
    field: string | null;
    tag: string;
    value: string;
    sql: string;
    error?: string | null;
}

export interface TagSyncResult {
    target: string;
    datasets: number;
    tagged_objects: number;
    operations: TagSyncOperation[];
    conflicts: string[];
    dry_run: boolean;
    applied: number;
    failed: number;
}

export interface ContractPart {
    part: 'schema' | 'freshness' | 'volume' | 'quality';
    name?: string;
    monitor_id?: string;
    status: ContractStatus;
    detail: string;
}

export interface Contract {
    dataset: string;
    version: string;
    owner: string | null;
    status: ContractStatus;
    parts: ContractPart[];
    checked_ms: number;
    spec: Record<string, unknown>;
    created_by: string;
    updated_ms: number;
    versions?: { version: string; created_by: string; created_ms: number }[];
}

export type AutomationActionType = 'add_tag' | 'add_term' | 'add_owner' | 'set_domain' | 'notify';

export interface AutomationAction {
    type: AutomationActionType;
    value?: string | null;
    recipients?: string[];
}

export interface AutomationSpec {
    name: string;
    description?: string;
    select: Record<string, unknown>;
    every_minutes: number;
    actions: AutomationAction[];
}

export interface AutomationRun {
    id: string;
    automation_id: string;
    started_ms: number;
    trigger: 'schedule' | 'manual';
    matched: number;
    changed: number;
    batch_id: string | null;
    state: 'APPLIED' | 'NOOP' | 'UNDONE' | 'ERROR';
    detail: { assets?: string[]; notified?: string[]; error?: string };
}

export interface Automation {
    id: string;
    name: string;
    spec: AutomationSpec;
    enabled: boolean;
    created_by: string;
    identity: string;
    last_run: AutomationRun | null;
}

export interface AutomationPreview {
    matched: number;
    would_change: number;
    changes: BulkChange[];
    sample_matched: string[];
    truncated: boolean;
}

export type CoverageFacet = 'description' | 'owner' | 'classification' | 'certified' | 'monitored';

export interface CoverageRow extends Record<CoverageFacet, number> {
    key: string | null;
    assets: number;
}

export interface ReportSnapshot {
    day: string;
    computed_ms: number;
    assets: number;
    coverage: { overall: Record<CoverageFacet, number>; by_domain: CoverageRow[]; by_platform: CoverageRow[] };
    adoption: {
        weekly_active_users: number;
        monthly_active_users: number;
        asset_views_7d: number;
        requests_30d: number;
        approved_30d: number;
        pending_requests: number;
        median_hours_to_decision: number | null;
    };
    quality: { open_incidents: number; monitored_pct: number; resolved_30d: number; mean_hours_to_resolve: number | null };
    governance: { personas?: number; purposes?: number; active_grants?: number; grants_expiring_7d?: number };
}

export interface ReportAsset extends Record<CoverageFacet, boolean> {
    urn: string;
    name: string;
    domain: string | null;
    platform: string;
}

export interface CostTotal {
    urn: string;
    name: string;
    platform: string;
    storage_usd: number;
    compute_usd: number;
    cost_usd: number;
    reads: number;
    storage_bytes: number | null;
    last_day: string;
    monthly_storage_usd?: number;
    cost_per_read_usd?: number;
}

export interface DataResult {
    columns: string[];
    rows: unknown[][];
    masked_columns: string[];
    truncated: boolean;
    elapsed_ms: number;
    tables: string[];
}

export interface StewardProposal {
    id: string;
    urn: string;
    name: string;
    kind: 'owner' | 'term' | 'description';
    field: string;
    value: string | { description: string; columns: unknown[]; model?: string };
    evidence: string[];
    confidence: number;
    state: 'PROPOSED' | 'ACCEPTED' | 'AUTO_ACCEPTED' | 'REJECTED';
}

export interface StewardRun {
    run_id: string;
    datasets_with_gaps: number;
    examined: number;
    proposed: number;
    auto_accepted: number;
    by_kind: Record<'owner' | 'term' | 'description', number>;
}

export interface ContextDefinition {
    id: string;
    kind: 'metric' | 'join';
    name: string;
    version: number;
    doc: {
        expression?: string;
        description?: string;
        datasets?: string[];
        source?: string;
        destination?: string;
        source_field?: string;
        destination_field?: string;
    };
    approved_by: string;
    updated_ms: number;
}

export interface PastAnswer {
    urn: string;
    title: string;
    sql: string;
    tables: string[];
    score: number;
    definitions: { name: string; kind: string; version: number; expression?: string }[];
}

export interface DefinitionEdits {
    name?: string;
    expression?: string;
    description?: string;
}

export type LicenceStatus = 'evaluation' | 'valid' | 'expiring' | 'expired' | 'invalid' | 'missing';

/** GET /admin/licence. Everyone gets status, read_only, days_left, over_seats; admins the rest. */
export interface LicenceState {
    status: LicenceStatus;
    read_only: boolean;
    days_left: number | null;
    over_seats?: boolean;
    required?: boolean;
    tenant?: string | null;
    seats_used?: number;
    seats_limit?: number | null;
    problem?: string | null;
    licence?: {
        id: string;
        customer: string;
        tenant: string | null;
        edition: string;
        features: string[];
        max_users: number | null;
        issued_at: string;
        expires_at: string;
        grace_days: number;
        development: boolean;
    } | null;
}

/** GET /extensions/ui: a section an installed extension adds to the workspace. */
export interface ExtensionSection {
    extension: string;
    publisher: string;
    label: string;
    url: string;
    placement: 'workspace' | 'asset';
}

export interface InstalledExtension {
    name: string;
    version: string;
    enabled: boolean;
    actor: string;
    installed_by: string;
    installed_ms: number;
    manifest: {
        publisher: string;
        description: string;
        permissions: string[];
        scope: unknown;
        ui: unknown[];
        events: unknown[];
    };
}

/** PUT /admin/extensions: token and signing_secret only on a first install (shown once). */
export interface ExtensionInstallResult {
    name: string;
    version: string;
    actor: string;
    upgraded_from?: string;
    token?: string;
    signing_secret?: string;
}

export type AITier = 'unacceptable' | 'high' | 'limited' | 'minimal';
export type AIState = 'DRAFT' | 'IN_REVIEW' | 'APPROVED' | 'REJECTED' | 'RETIRED';

export interface AIAssessment {
    name: string;
    restricted_labels: string[];
    questions: {
        id: string;
        title: string;
        type: 'multi' | 'boolean' | 'text';
        options: { value: string; label: string; tier: AITier }[];
        tier_if_yes: AITier | null;
        required_for: string[];
    }[];
}

export interface AISystemInput {
    name: string;
    purpose: string;
    owner?: string | null;
    models: string[];
    datasets: string[];
    vendor?: string | null;
    data_purpose_id?: string | null;
    answers: Record<string, unknown>;
}

export interface AISystem extends AISystemInput {
    id: string;
    owner: string;
    state: AIState;
    tier: AITier;
    missing: string[];
    violations: { dataset: string; labels: string[]; reason: string }[];
    decided_by: string | null;
    decided_ms: number | null;
    decision_note: string | null;
    updated_ms: number;
}

export interface AISystemsReport {
    assessment: string;
    total: number;
    by_tier: Record<AITier, number>;
    by_state: Record<string, number>;
    high_risk_not_approved: string[];
    violations: { system: string; name: string; state: AIState; dataset: string; labels: string[]; reason: string }[];
}
