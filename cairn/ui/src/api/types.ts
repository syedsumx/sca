/**
 * Wire types for the Cairn control-plane API. Field names mirror the Python (snake_case) payloads
 * exactly so responses can be used without mapping.
 */

export type IsoTimestamp = string;

// ---------------------------------------------------------------------------------------------
// Quality
// ---------------------------------------------------------------------------------------------

export type MonitorType = 'freshness' | 'volume' | 'field_metric' | 'distribution' | 'schema' | 'custom_sql';
export type Sensitivity = 'low' | 'medium' | 'high';
export type Severity = 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL';
export type MonitorStatus = 'TRAINING' | 'ACTIVE' | 'PAUSED';
export type EvaluationStatus = 'PASS' | 'FAIL' | 'WARN' | 'TRAINING' | 'ERROR';
export type EvaluationFeedback = 'EXPECTED' | 'REAL' | 'NEW_NORMAL';

export interface MonitorSchedule {
    cron?: string;
    interval_seconds?: number;
}

export interface Evaluation {
    id: string;
    monitor_id: string;
    ts: IsoTimestamp;
    metric_value: number | null;
    expected_low: number | null;
    expected_high: number | null;
    status: EvaluationStatus;
    score: number | null;
    message: string;
    feedback: EvaluationFeedback | null;
}

export interface Monitor {
    id: string;
    name: string;
    dataset_urn: string;
    type: MonitorType;
    schedule: MonitorSchedule;
    config: Record<string, unknown>;
    sensitivity: Sensitivity;
    severity: Severity;
    enabled: boolean;
    status: MonitorStatus;
    last_evaluation?: Evaluation | null;
    created_at: IsoTimestamp;
    updated_at: IsoTimestamp;
}

/** Payload accepted by POST /quality/monitors and PUT /quality/monitors/{id}. */
export type MonitorInput = Pick<
    Monitor,
    'name' | 'dataset_urn' | 'type' | 'schedule' | 'config' | 'sensitivity' | 'severity' | 'enabled'
>;

export interface DatasetHealth {
    dataset_urn: string;
    status: EvaluationStatus | 'UNKNOWN';
    monitors: Array<{ monitor: Monitor; last_evaluation: Evaluation | null }>;
    open_incidents: number;
    /** Checks from other tools (dbt tests, Great Expectations, Soda, generic), next to Cairn's own. */
    external?: ExternalCheck[];
}

// ---------------------------------------------------------------------------------------------
// Governance
// ---------------------------------------------------------------------------------------------

export type Decision = 'ALLOW' | 'DENY' | 'NOT_APPLICABLE';

export interface SimulateRequest {
    actor: string;
    privilege: string;
    resource_urn?: string;
    subject_attributes?: Record<string, unknown>;
    resource_attributes?: Record<string, unknown>;
    environment?: Record<string, unknown>;
}

export interface Obligation {
    type: string;
    params: Record<string, unknown>;
}

export interface TraceEntry {
    policy_id: string;
    effect: 'allow' | 'deny';
    matched: boolean;
    reason: string;
}

export interface SimulateResponse {
    decision: Decision;
    reason: string;
    policy_ids: string[];
    obligations: Obligation[];
    trace: TraceEntry[];
}

export interface Policy {
    id: string;
    description: string;
    effect: 'allow' | 'deny';
    priority: number;
    actions: string[];
    target?: Record<string, unknown>;
    condition?: Record<string, unknown>;
    obligations?: Array<Record<string, unknown>>;
}

export interface PolicyBundle {
    policies: Policy[];
}

export interface PolicyValidation {
    valid: boolean;
    errors: string[];
}

export type WorkflowKind = 'CERTIFICATION' | 'DOCUMENTATION';
export type WorkflowState =
    | 'DRAFT'
    | 'SUBMITTED'
    | 'IN_REVIEW'
    | 'CHANGES_REQUESTED'
    | 'APPROVED'
    | 'REJECTED'
    | 'CERTIFIED'
    | 'EXPIRED'
    | 'REVOKED';
export type WorkflowAction = 'submit' | 'approve' | 'reject' | 'request-changes' | 'revoke';

export interface WorkflowVote {
    actor: string;
    at: IsoTimestamp;
    comment: string | null;
}

export interface WorkflowCheck {
    name: string;
    passed: boolean;
    detail: string;
}

export interface WorkflowHistoryEntry {
    at: IsoTimestamp;
    actor: string;
    from_state: WorkflowState | null;
    to_state: WorkflowState;
    comment: string | null;
}

export interface Workflow {
    id: string;
    kind: WorkflowKind;
    entity_urn: string;
    state: WorkflowState;
    requester: string;
    template: string;
    quorum: number;
    approvals: WorkflowVote[];
    rejections: WorkflowVote[];
    payload: Record<string, unknown>;
    checks: WorkflowCheck[];
    history: WorkflowHistoryEntry[];
    created_at: IsoTimestamp;
    updated_at: IsoTimestamp;
    valid_until: IsoTimestamp | null;
}

export interface WorkflowCreate {
    kind: WorkflowKind;
    entity_urn: string;
    template?: string;
    payload?: Record<string, unknown>;
}

export interface WorkflowFilter {
    state?: WorkflowState;
    kind?: WorkflowKind;
}

// ---------------------------------------------------------------------------------------------
// Remote agents
// ---------------------------------------------------------------------------------------------

export type AgentStatus = 'ONLINE' | 'OFFLINE' | 'REVOKED';

export interface Agent {
    agent_id: string;
    pool: string;
    hostname: string;
    version: string;
    status: AgentStatus;
    last_heartbeat: IsoTimestamp | null;
    enrolled_at: IsoTimestamp;
    running_tasks: number;
}

export interface AgentPool {
    name: string;
    description: string;
    allowed_platforms: string[];
    max_concurrency: number;
    agents_online: number;
}

export type AgentPoolInput = Omit<AgentPool, 'agents_online'>;

export interface EnrollmentToken {
    token: string;
    expires_at: IsoTimestamp;
}

export interface AgentRun {
    run_id: string;
    task_id: string;
    pool: string;
    agent_id: string | null;
    status: string;
    started_at: IsoTimestamp | null;
    finished_at: IsoTimestamp | null;
    mcps_forwarded: number;
    mcps_rejected?: number;
    attempts?: number;
    execution_request_urn: string | null;
}

export interface TaskLogs {
    task_id: string;
    run_id: string | null;
    status: string;
    lines: string[];
    /** Pass as `after` on the next call to get only new lines. */
    next: number;
    done: boolean;
}

export interface CancelResult {
    task_id: string;
    status: 'CANCELLED';
    was: string;
}

// ---------------------------------------------------------------------------------------------
// AI
// ---------------------------------------------------------------------------------------------

export interface SearchFilter {
    field: string;
    condition: string;
    values: string[];
}

export interface LineageSpec {
    direction: 'UPSTREAM' | 'DOWNSTREAM' | string;
    urn: string;
    max_hops: number;
}

export interface SearchPlan {
    query: string;
    entity_types: string[];
    filters: SearchFilter[];
    lineage?: LineageSpec | null;
    sort?: string | Record<string, unknown> | null;
}

export interface AssetResult {
    urn: string;
    name: string;
    type: string;
    platform: string | null;
    description: string | null;
    score: number | null;
}

export interface AskResponse {
    answer: string;
    plan: SearchPlan;
    results: AssetResult[];
    citations: string[];
}

export interface SemanticSearchResponse {
    results: AssetResult[];
}

export interface ColumnDraft {
    field_path: string;
    description: string;
    suggested_terms: string[];
}

export interface DocumentationDraft {
    summary: string;
    description: string;
    columns: ColumnDraft[];
    suggested_terms: string[];
    confidence: number;
    unknowns: string[];
}

export interface DocGenerateResponse {
    workflow_id: string;
    draft: DocumentationDraft;
}

export interface ExternalCheck {
    id: string;
    tool: string;
    name: string;
    column: string | null;
    status: 'PASS' | 'WARN' | 'FAIL' | 'ERROR';
    message: string;
    url: string | null;
    last_ms: number;
    history: { ts: number; status: string }[];
}
