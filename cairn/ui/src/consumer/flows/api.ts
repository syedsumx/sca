/** Cairn Flows over its API (`/v1/flows`), through the consumer client. */

import type { ConsumerClient } from '../api/client';
import type { FlowDef } from './model';

export interface FlowRunBrief {
    id: string;
    status: string;
    created_ms: number;
    finished_ms: number | null;
}

/** A flow as listed (GET /v1/flows). */
export interface FlowSummary {
    id: string;
    name: string;
    owner: string;
    description: string;
    editors: string[];
    viewers: string[];
    enabled: boolean;
    published_version: number | null;
    draft_errors: number;
    next_run_ms: number | null;
    updated_ms: number;
    updated_by: string;
    steps: number;
    role: 'owner' | 'admin' | 'editor' | 'viewer' | null;
    last_run: FlowRunBrief | null;
}

export interface FlowProblem {
    path: string;
    message: string;
    step?: string;
    line?: number;
}

/** One flow with its draft (as saved, valid or not) and its published version. */
export interface FlowDetail extends FlowSummary {
    draft: FlowDef;
    errors: FlowProblem[];
    draft_yaml: string | null;
    published: FlowDef | null;
}

export interface FlowVersion {
    version: number;
    published_by: string;
    published_ms: number;
    note: string | null;
}

export interface FlowStepRun {
    key: string;
    step: string;
    type: string;
    status: string;
    attempt: number;
    started_ms: number | null;
    finished_ms: number | null;
    next_ms: number | null;
    error: string | null;
    output: Record<string, unknown> | null;
    reused: boolean;
    decision: { decision: string; by: string; comment: string } | null;
    items?: FlowStepRun[];
    approvers?: string[];
    groups?: string[];
    log?: string | null;
}

export interface FlowRun {
    id: string;
    flow_id: string;
    flow: string;
    version: number;
    status: string;
    trigger: string;
    triggered_by: string | null;
    run_as: string;
    params: Record<string, unknown>;
    logical_date: string | null;
    detail: Record<string, unknown>;
    created_ms: number;
    started_ms: number | null;
    finished_ms: number | null;
    error: string | null;
    cancel_requested: boolean;
    steps: FlowStepRun[];
    definition?: FlowDef;
}

export interface FlowRunRow {
    id: string;
    version: number;
    status: string;
    trigger: string;
    triggered_by: string | null;
    created_ms: number;
    started_ms: number | null;
    finished_ms: number | null;
    logical_date: string | null;
    error: string | null;
}

export interface FlowApproval {
    run: string;
    flow: string;
    step: string;
    message: string;
    since_ms: number | null;
}

const enc = encodeURIComponent;

/** The calls the Flows page makes. */
export function flowsApi(client: ConsumerClient) {
    const r = <T>(method: 'GET' | 'POST' | 'PUT' | 'DELETE', path: string, body?: unknown) =>
        client.request<T>(method, path, body);
    return {
        list: () => r<FlowSummary[]>('GET', '/v1/flows'),
        get: (name: string) => r<FlowDetail>('GET', `/v1/flows/${enc(name)}`),
        create: (definition: FlowDef, publish = false) => r<FlowDetail>('POST', `/v1/flows${publish ? '?publish=true' : ''}`, definition),
        save: (name: string, definition: FlowDef) => r<FlowDetail>('PUT', `/v1/flows/${enc(name)}`, definition),
        publish: (name: string, note: string) => r<{ flow: string; version: number }>('POST', `/v1/flows/${enc(name)}/publish`, { note }),
        remove: (name: string) => r<void>('DELETE', `/v1/flows/${enc(name)}`),
        setEnabled: (name: string, enabled: boolean) => r<FlowDetail>('POST', `/v1/flows/${enc(name)}/enabled`, { enabled }),
        versions: (name: string) => r<FlowVersion[]>('GET', `/v1/flows/${enc(name)}/versions`),
        run: (name: string, params: Record<string, unknown>, draft = false) =>
            r<FlowRun>('POST', `/v1/flows/${enc(name)}/runs`, { params, ...(draft ? { version: 'draft' } : {}) }),
        runs: (name: string) => r<FlowRunRow[]>('GET', `/v1/flows/${enc(name)}/runs`),
        runDetail: (id: string) => r<FlowRun>('GET', `/v1/flows/runs/${enc(id)}`),
        stepLog: (id: string, key: string) =>
            r<{ key: string; status: string; log: string; error: string | null; output: unknown }>('GET', `/v1/flows/runs/${enc(id)}/steps/${enc(key)}/log`),
        cancel: (id: string) => r<FlowRun>('POST', `/v1/flows/runs/${enc(id)}/cancel`),
        rerun: (id: string, fromStep?: string) => r<FlowRun>('POST', `/v1/flows/runs/${enc(id)}/rerun`, fromStep ? { from_step: fromStep } : {}),
        decide: (id: string, step: string, decision: 'approve' | 'reject', comment: string) =>
            r<FlowRun>('POST', `/v1/flows/runs/${enc(id)}/steps/${enc(step)}/${decision}`, { comment }),
        approvals: () => r<FlowApproval[]>('GET', '/v1/flows/approvals'),
        backfill: (name: string, start: string, end: string, param?: string) =>
            r<{ runs: string[] }>('POST', `/v1/flows/${enc(name)}/backfill`, { start, end, ...(param ? { param } : {}) }),
    };
}

export type FlowsApi = ReturnType<typeof flowsApi>;
