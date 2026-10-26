import type { Decision, EvaluationStatus, WorkflowState } from '../api/types';

/** antd Tag colour for evaluation / health statuses. */
export function statusColor(status: EvaluationStatus | 'UNKNOWN' | string | undefined | null): string {
    switch (status) {
        case 'PASS':
            return 'green';
        case 'WARN':
            return 'gold';
        case 'FAIL':
            return 'red';
        case 'ERROR':
            return 'volcano';
        case 'TRAINING':
            return 'blue';
        default:
            return 'default';
    }
}

export function decisionColor(decision: Decision): string {
    if (decision === 'ALLOW') return 'green';
    if (decision === 'DENY') return 'red';
    return 'default';
}

export function workflowStateColor(state: WorkflowState): string {
    switch (state) {
        case 'CERTIFIED':
        case 'APPROVED':
            return 'green';
        case 'REJECTED':
        case 'REVOKED':
            return 'red';
        case 'CHANGES_REQUESTED':
        case 'EXPIRED':
            return 'orange';
        case 'IN_REVIEW':
        case 'SUBMITTED':
            return 'blue';
        default:
            return 'default';
    }
}

export function formatTimestamp(iso: string | null | undefined): string {
    if (!iso) return '—';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString();
}

/** "3m ago" style relative time. `now` is injectable for tests. */
export function formatRelative(iso: string | null | undefined, now: number = Date.now()): string {
    if (!iso) return 'never';
    const t = new Date(iso).getTime();
    if (Number.isNaN(t)) return iso;
    const s = Math.round((now - t) / 1000);
    if (s < 0) return 'just now';
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
}

export function formatNumber(v: number | null | undefined): string {
    if (v === null || v === undefined || Number.isNaN(v)) return '—';
    if (Math.abs(v) >= 1e9) return `${(v / 1e9).toFixed(2)}B`;
    if (Math.abs(v) >= 1e6) return `${(v / 1e6).toFixed(2)}M`;
    if (Math.abs(v) >= 1e4) return `${(v / 1e3).toFixed(1)}k`;
    if (Number.isInteger(v)) return String(v);
    return v.toFixed(3);
}

/** Extracts the human name from a dataset URN: urn:li:dataset:(platform,db.schema.table,ENV) → table path. */
export function datasetNameFromUrn(urn: string): string {
    const m = /^urn:li:dataset:\(([^,]+),(.+),([^,)]+)\)$/.exec(urn);
    if (!m) return urn;
    return m[2];
}

export function platformFromUrn(urn: string): string | null {
    const m = /urn:li:dataPlatform:([^,)]+)/.exec(urn);
    return m ? m[1] : null;
}
