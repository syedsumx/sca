/** The Ontology explorer over Cairn's `/v1/ontology` API, through the consumer client. */

import type { ConsumerClient } from '../api/client';

export type ElementStatus = 'accepted' | 'proposed' | 'rejected' | 'compiled';

export interface ExplorerTerm {
    id: string;
    name: string;
    description: string;
    synonyms: string[];
    table: string;
    table_name: string;
    catalog: string;
    schema: string;
    table_label: string;
    domain: string | null;
    domain_name: string | null;
    owners: string[];
    attributes: number;
    date_formats: number;
    bridge: boolean;
    provenance: string[];
    confidence: number;
    status: ElementStatus;
    proposal: string | null;
}

export interface ExplorerRelationship {
    id: string;
    source: string;
    target: string;
    source_column: string;
    target_column: string;
    source_term: string | null;
    target_term: string | null;
    cardinality: string;
    confidence: number;
    join_default: boolean;
    provenance: string[];
    queries: number;
    status: ElementStatus;
    proposal: string | null;
    bridge: boolean;
}

export interface ExplorerBridge {
    id: string;
    table: string;
    table_name: string;
    schema: string;
    domain: string | null;
    domain_name: string | null;
    term: string | null;
    targets: { table: string; column: string; source_column: string; term: string | null }[];
    metric: string | null;
}

export interface ExplorerTable {
    id: string;
    schema: string;
    catalog: string;
    table_label: string;
    domain: string | null;
    domain_name: string | null;
    bridge: boolean;
    term: string | null;
}

export interface ExplorerData {
    run_id: string | null;
    version: number | null;
    terms: ExplorerTerm[];
    relationships: ExplorerRelationship[];
    bridges: ExplorerBridge[];
    metrics: { id: string; name: string; term: string | null; tables: string[]; source: string; status: ElementStatus }[];
    tables: ExplorerTable[];
}

export interface RelationshipEvidence {
    sources: string[];
    foreign_key?: { detail: string };
    query_log?: { queries: number; executions: number; query_ids: string[] };
    model?: { confidence: number; detail: string; inferred_without_model: boolean };
}

export interface TermRelationship {
    id: string;
    direction: 'in' | 'out';
    source: string;
    source_column: string;
    target: string;
    target_column: string;
    join: string;
    other_table: string;
    other_term: { id: string; name: string } | null;
    cardinality: string;
    confidence: number;
    join_default: boolean;
    rationale: string;
    evidence: RelationshipEvidence;
    status: ElementStatus;
}

export interface Proposal {
    id: string;
    type: string;
    kind: 'term' | 'relationship' | 'metric';
    status: string;
    confidence: number;
    title: string;
    summary_md: string;
    entities: string[];
    element_id: string | null;
    version: number | null;
    proposed_changes?: unknown[] | null;
}

export interface TermDetail {
    id: string;
    name: string;
    description: string;
    synonyms: string[];
    proposed_name: string;
    table: string;
    table_name: string;
    catalog: string;
    schema: string;
    table_label: string;
    domain: string | null;
    domain_name: string | null;
    owners: string[];
    status: ElementStatus;
    glossary_term: string | null;
    attributes: { id: string; column: string; name: string; datatype: string | null; description: string | null;
        format: string | null; is_unique: boolean | null; deprecated: boolean; table: string; provenance: string[] }[];
    date_formats: { column: string; name: string; format: string }[];
    relationships: TermRelationship[];
    metrics: { id: string; name: string; description: string; expression: string | null; sql: string | null;
        source: string | null; usage: number | null; status: ElementStatus }[];
    bridges: { id: string; table: string; table_name: string; targets: string[] }[];
    provenance: {
        run: { id: string; version?: number; mode?: string; state?: string; started_ms?: number; finished_ms?: number | null;
            requested_by?: string };
        version: number | null;
        sources: { source: string; detail: string; confidence: number | null; run_id: string | null }[];
        inputs: { tables: string[]; columns: string[]; queries: string[] };
    };
    proposals: Proposal[];
}

export interface AssetTerms {
    urn: string;
    version: number | null;
    terms: { id: string; name: string; description: string; synonyms: string[]; status: ElementStatus }[];
    columns: { column: string; name: string; term: string; term_name: string; format: string | null; datatype: string | null }[];
    relationships: { id: string; column: string; other_table: string; other_column: string;
        other_term: { id: string; name: string } | null; cardinality: string; confidence: number }[];
    bridge: boolean;
}

export interface TermHit {
    id: string;
    name: string;
    description: string;
    synonyms: string[];
    table: string;
    table_name: string;
    matched: 'name' | 'synonym';
    synonym: string | null;
    score: number;
}

export interface RunProgress {
    run_id: string;
    phase: string;
    tables_total: number;
    tables_done: number;
    scope: number | null;
}

export interface OntologyRun {
    id: string;
    version: number;
    state: 'running' | 'completed' | 'failed' | 'cancelled' | 'interrupted';
    mode: string;
    started_ms: number;
    finished_ms: number | null;
    stats: Record<string, unknown>;
    error: string | null;
    requested_by: string;
    progress?: RunProgress;
}

export interface Estimate {
    mode: string;
    tables: number;
    to_process: number;
    reused: number;
    out_of_scope: number;
    without_columns: number;
    scope: number | null;
    published: string | null;
    model: { chat_configured: boolean; embeddings_configured: boolean; chat_calls_per_table: number; chat_calls_max: number;
        embedding_calls_max: number; note: string };
}

export interface DiffPart {
    added: { id: string; label: string }[];
    removed: { id: string; label: string }[];
    changed: { id: string; label: string; changes: Record<string, { before: unknown; after: unknown }> }[];
    counts: { added: number; removed: number; changed: number; unchanged: number };
}

export interface RunDiff {
    run: string;
    against: string | null;
    version: number;
    terms: DiffPart;
    relationships: DiffPart;
    metrics: DiffPart;
}

export interface CatalogTables {
    tables: { id: string; name: string; catalog: string; schema: string; columns: number | null }[];
    schemas: string[];
}

export interface BatchResult {
    action: string;
    done: number;
    refused: number;
    results: { id: string; ok: boolean; status?: string; status_code?: number; error?: string }[];
}

export interface Grounded {
    terms: { id: string; name: string; table: string }[];
    relationships: { id: string; label: string }[];
}

export interface Scope {
    tables?: string[];
    schemas?: string[];
}

export type ExportFormat = 'jsonld' | 'turtle' | 'csv';

const enc = encodeURIComponent;

/** The calls the explorer makes. */
export function ontologyApi(client: ConsumerClient) {
    const r = <T>(method: 'GET' | 'POST', path: string, body?: unknown, query?: Record<string, string>) =>
        client.request<T>(method, path, body, query ? { query } : {});
    return {
        explorer: () => r<ExplorerData>('GET', '/v1/ontology/explorer'),
        term: (id: string) => r<TermDetail>('GET', `/v1/ontology/terms/${enc(id)}`),
        asset: (urn: string) => r<AssetTerms>('GET', '/v1/ontology/asset', undefined, { urn }),
        searchTerms: (q: string, limit = 8) => r<{ terms: TermHit[] }>('GET', '/v1/ontology/search-terms', undefined, { q, limit: String(limit) }),
        proposals: (kind?: string) => r<{ proposals: Proposal[] }>('GET', '/v1/ontology/proposals', undefined, kind ? { kind } : undefined),
        decide: (id: string, action: 'accept' | 'reject', comment = '', edits?: { name?: string; definition?: string; synonyms?: string[] }) =>
            r<Proposal>('POST', `/v1/ontology/proposals/${enc(id)}/${action}`, { comment, ...(edits ? { edits } : {}) }),
        decideMany: (ids: string[], action: 'accept' | 'reject', comment = '') =>
            r<BatchResult>('POST', '/v1/ontology/proposals/batch', { ids, action, comment }),
        catalog: () => r<CatalogTables>('GET', '/v1/ontology/catalog'),
        estimate: (mode: string, scope: Scope) => r<Estimate>('POST', '/v1/ontology/estimate', { mode, ...scope }),
        compile: (mode: string, scope: Scope) =>
            r<{ run: OntologyRun }>('POST', '/v1/ontology/runs', { mode, sync: true, wait: false, ...scope }),
        runs: () => r<{ runs: OntologyRun[]; published: string | null; active: string | null }>('GET', '/v1/ontology/runs'),
        run: (id: string) => r<OntologyRun>('GET', `/v1/ontology/runs/${enc(id)}`),
        cancel: (id: string) => r<{ cancelling: string }>('POST', `/v1/ontology/runs/${enc(id)}/cancel`),
        resume: (id: string) => r<OntologyRun>('POST', `/v1/ontology/runs/${enc(id)}/resume`),
        importRun: (id: string) => r<{ run_id: string; new: number; updated: number }>('POST', `/v1/ontology/runs/${enc(id)}/import`),
        diff: (id: string) => r<RunDiff>('GET', `/v1/ontology/runs/${enc(id)}/diff`),
        exportGraph: (format: ExportFormat) => r<unknown>('GET', '/v1/ontology/graph', undefined, { format }),
    };
}

export type OntologyApi = ReturnType<typeof ontologyApi>;

/** `many_to_one` → `N:1`. */
export function cardinalityLabel(c: string): string {
    return ({ many_to_one: 'N:1', one_to_one: '1:1', one_to_many: '1:N', many_to_many: 'N:M' } as Record<string, string>)[c] ?? '?';
}

/** How a relationship is known: a declared key, joins in the query log, or a model's (or heuristic's) inference. */
export function relationshipType(provenance: string[]): 'key' | 'queries' | 'inferred' {
    if (provenance.some((p) => p === 'catalog_fk' || p === 'catalog')) return 'key';
    if (provenance.length && provenance.every((p) => p === 'observed_in_queries')) return 'queries';
    return 'inferred';
}

export const TYPE_LABEL: Record<'key' | 'queries' | 'inferred', string> = {
    key: 'foreign key',
    queries: 'seen in queries',
    inferred: 'inferred',
};

/** The last part of a dataset urn's name (`retail.orders`). */
export function tableLabel(urn: string): string {
    const m = /^urn:li:dataset:\(urn:li:dataPlatform:[^,]+,(.+),[^,]+\)$/.exec(urn);
    if (!m) return urn;
    const parts = m[1].split('.');
    return parts.slice(-2).join('.');
}

export function assetHref(urn: string): string {
    return `/asset/${encodeURIComponent(urn)}`;
}

export function termHref(id: string): string {
    return `/ontology/terms/${encodeURIComponent(id)}`;
}
