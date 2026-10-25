import React, { useState } from 'react';
import { Alert, Button, Drawer, Input, List, Select, Space, Table, Tag, Timeline, Tooltip, message } from 'antd';

import { useAsync, useDheClient, useMutation } from '../api/hooks';
import type { Workflow, WorkflowAction, WorkflowKind, WorkflowState } from '../api/types';
import { datasetNameFromUrn, formatRelative, formatTimestamp, workflowStateColor } from '../common/format';
import { EmptyState, ErrorState, LoadingState, Mono, PageContainer, PageHeader } from '../common/States';
import { DocDraftReview } from '../ai/DocDraftReview';

export const WORKFLOW_STATES: WorkflowState[] = [
    'DRAFT',
    'SUBMITTED',
    'IN_REVIEW',
    'CHANGES_REQUESTED',
    'APPROVED',
    'REJECTED',
    'CERTIFIED',
    'EXPIRED',
    'REVOKED',
];

/** Transitions the UI offers per state; the server enforces the real state machine and SoD rules. */
export function actionsForState(state: WorkflowState): WorkflowAction[] {
    switch (state) {
        case 'DRAFT':
        case 'CHANGES_REQUESTED':
        case 'EXPIRED':
            return ['submit'];
        case 'SUBMITTED':
        case 'IN_REVIEW':
            return ['approve', 'request-changes', 'reject'];
        case 'APPROVED':
        case 'CERTIFIED':
            return ['revoke'];
        default:
            return [];
    }
}

const ACTION_LABELS: Record<WorkflowAction, string> = {
    submit: 'Submit for review',
    approve: 'Approve',
    reject: 'Reject',
    'request-changes': 'Request changes',
    revoke: 'Revoke',
};

export interface CertificationInboxProps {
    defaultState?: WorkflowState;
    defaultKind?: WorkflowKind;
}

export function CertificationInbox({ defaultState = 'IN_REVIEW', defaultKind }: CertificationInboxProps) {
    const client = useDheClient();
    const [state, setState] = useState<WorkflowState | undefined>(defaultState);
    const [kind, setKind] = useState<WorkflowKind | undefined>(defaultKind);
    const [selected, setSelected] = useState<Workflow | null>(null);
    const { data, error, loading, reload, setData } = useAsync(
        (signal) => client.listWorkflows({ state, kind }, { signal }),
        [client, state, kind],
    );

    const replace = (wf: Workflow) => {
        setData((prev) => {
            const list = prev ?? [];
            // Drop rows that no longer match the active state filter.
            if (state && wf.state !== state) return list.filter((w) => w.id !== wf.id);
            return list.map((w) => (w.id === wf.id ? wf : w));
        });
        setSelected(wf);
    };

    return (
        <PageContainer>
            <PageHeader>
                <h1>Certification &amp; review inbox</h1>
                <Space wrap>
                    <Select
                        aria-label="Filter by state"
                        allowClear
                        placeholder="Any state"
                        value={state}
                        onChange={(v) => setState(v)}
                        style={{ width: 200 }}
                        options={WORKFLOW_STATES.map((s) => ({ value: s, label: s.replace('_', ' ') }))}
                    />
                    <Select
                        aria-label="Filter by kind"
                        allowClear
                        placeholder="Any kind"
                        value={kind}
                        onChange={(v) => setKind(v)}
                        style={{ width: 180 }}
                        options={[
                            { value: 'CERTIFICATION', label: 'Certification' },
                            { value: 'DOCUMENTATION', label: 'AI documentation' },
                        ]}
                    />
                    <Button onClick={() => reload()}>Refresh</Button>
                </Space>
            </PageHeader>
            {loading && !data && <LoadingState label="Loading proposals…" />}
            {error && !data && <ErrorState error={error} onRetry={reload} />}
            {data && data.length === 0 && <EmptyState description="Nothing to review" />}
            {data && data.length > 0 && (
                <Table<Workflow>
                    rowKey="id"
                    dataSource={data}
                    onRow={(wf) => ({
                        onClick: () => setSelected(wf),
                        onKeyDown: (e) => {
                            if (e.key === 'Enter') setSelected(wf);
                        },
                        tabIndex: 0,
                        style: { cursor: 'pointer' },
                    })}
                    columns={[
                        {
                            title: 'Asset',
                            dataIndex: 'entity_urn',
                            render: (urn: string) => (
                                <Tooltip title={urn}>
                                    <strong>{datasetNameFromUrn(urn)}</strong>
                                </Tooltip>
                            ),
                        },
                        {
                            title: 'Kind',
                            dataIndex: 'kind',
                            width: 150,
                            render: (k: WorkflowKind) => (k === 'DOCUMENTATION' ? 'AI documentation' : 'Certification'),
                        },
                        {
                            title: 'State',
                            dataIndex: 'state',
                            width: 170,
                            render: (s: WorkflowState) => <Tag color={workflowStateColor(s)}>{s.replace('_', ' ')}</Tag>,
                        },
                        {
                            title: 'Approvals',
                            width: 110,
                            render: (_: unknown, wf) => `${wf.approvals.length} / ${wf.quorum}`,
                        },
                        {
                            title: 'Checks',
                            width: 110,
                            render: (_: unknown, wf) => {
                                const failed = wf.checks.filter((c) => !c.passed).length;
                                if (wf.checks.length === 0) return '—';
                                return failed ? <Tag color="red">{failed} failing</Tag> : <Tag color="green">all pass</Tag>;
                            },
                        },
                        { title: 'Requester', dataIndex: 'requester', render: (v: string) => <Mono>{v}</Mono> },
                        { title: 'Updated', dataIndex: 'updated_at', width: 110, render: (v: string) => formatRelative(v) },
                    ]}
                />
            )}
            <WorkflowDrawer workflow={selected} onClose={() => setSelected(null)} onChange={replace} />
        </PageContainer>
    );
}

interface WorkflowDrawerProps {
    workflow: Workflow | null;
    onClose: () => void;
    onChange: (wf: Workflow) => void;
}

function WorkflowDrawer({ workflow, onClose, onChange }: WorkflowDrawerProps) {
    const client = useDheClient();
    const [comment, setComment] = useState('');
    const { mutate, pending, error } = useMutation((id: string, action: WorkflowAction, c?: string) =>
        client.transitionWorkflow(id, action, c),
    );

    const act = async (action: WorkflowAction) => {
        if (!workflow) return;
        if ((action === 'reject' || action === 'request-changes' || action === 'revoke') && !comment.trim()) {
            message.warning('Please add a comment explaining this decision');
            return;
        }
        try {
            const wf = await mutate(workflow.id, action, comment.trim() || undefined);
            setComment('');
            onChange(wf);
            message.success(`${ACTION_LABELS[action]}: now ${wf.state}`);
        } catch {
            // surfaced via error
        }
    };

    return (
        <Drawer
            open={!!workflow}
            onClose={onClose}
            width={720}
            title={workflow ? `${workflow.kind === 'DOCUMENTATION' ? 'Documentation proposal' : 'Certification'} — ${datasetNameFromUrn(workflow.entity_urn)}` : ''}
            destroyOnClose
        >
            {workflow && (
                <Space direction="vertical" style={{ width: '100%' }} size="large">
                    <Space wrap>
                        <Tag color={workflowStateColor(workflow.state)}>{workflow.state}</Tag>
                        <span>
                            Template <Mono>{workflow.template}</Mono>
                        </span>
                        <span>
                            Quorum {workflow.approvals.length}/{workflow.quorum}
                        </span>
                        {workflow.valid_until && <span>Valid until {formatTimestamp(workflow.valid_until)}</span>}
                    </Space>
                    <Mono>{workflow.entity_urn}</Mono>

                    {workflow.checks.length > 0 && (
                        <List
                            size="small"
                            header={<strong>Entry checks</strong>}
                            bordered
                            dataSource={workflow.checks}
                            renderItem={(c) => (
                                <List.Item>
                                    <Space>
                                        <Tag color={c.passed ? 'green' : 'red'}>{c.passed ? 'PASS' : 'FAIL'}</Tag>
                                        <strong>{c.name}</strong>
                                        <span>{c.detail}</span>
                                    </Space>
                                </List.Item>
                            )}
                        />
                    )}

                    {workflow.kind === 'DOCUMENTATION' && (
                        <DocDraftReview workflow={workflow} onTransition={onChange} />
                    )}

                    {(workflow.approvals.length > 0 || workflow.rejections.length > 0) && (
                        <List
                            size="small"
                            header={<strong>Votes</strong>}
                            bordered
                            dataSource={[
                                ...workflow.approvals.map((v) => ({ ...v, vote: 'approved' })),
                                ...workflow.rejections.map((v) => ({ ...v, vote: 'rejected' })),
                            ]}
                            renderItem={(v) => (
                                <List.Item>
                                    <Space>
                                        <Tag color={v.vote === 'approved' ? 'green' : 'red'}>{v.vote}</Tag>
                                        <Mono>{v.actor}</Mono>
                                        <small>{formatTimestamp(v.at)}</small>
                                        {v.comment && <span>“{v.comment}”</span>}
                                    </Space>
                                </List.Item>
                            )}
                        />
                    )}

                    <div>
                        <strong>History</strong>
                        <Timeline style={{ marginTop: 12 }}>
                            {workflow.history.map((h) => (
                                <Timeline.Item key={`${h.at}-${h.to_state}`} color={workflowStateColor(h.to_state)}>
                                    {h.from_state ? `${h.from_state} → ` : ''}
                                    <strong>{h.to_state}</strong> by <Mono>{h.actor}</Mono> · {formatTimestamp(h.at)}
                                    {h.comment && <div>“{h.comment}”</div>}
                                </Timeline.Item>
                            ))}
                        </Timeline>
                    </div>

                    {workflow.kind === 'CERTIFICATION' && actionsForState(workflow.state).length > 0 && (
                        <Space direction="vertical" style={{ width: '100%' }}>
                            {error && <Alert type="error" showIcon message={error.message} />}
                            <Input.TextArea
                                aria-label="Comment"
                                rows={3}
                                placeholder="Comment (required for reject, request changes and revoke)"
                                value={comment}
                                onChange={(e) => setComment(e.target.value)}
                            />
                            <Space wrap>
                                {actionsForState(workflow.state).map((a) => (
                                    <Button
                                        key={a}
                                        type={a === 'approve' || a === 'submit' ? 'primary' : 'default'}
                                        danger={a === 'reject' || a === 'revoke'}
                                        loading={pending}
                                        onClick={() => act(a)}
                                    >
                                        {ACTION_LABELS[a]}
                                    </Button>
                                ))}
                            </Space>
                        </Space>
                    )}
                </Space>
            )}
        </Drawer>
    );
}

export default CertificationInbox;
