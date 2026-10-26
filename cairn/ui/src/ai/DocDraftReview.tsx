import React, { useState } from 'react';
import { Alert, Button, Card, Descriptions, Input, Progress, Space, Table, Tag, Typography, message } from 'antd';

import { useDheClient, useMutation } from '../api/hooks';
import type { ColumnDraft, DocumentationDraft, Workflow } from '../api/types';
import { datasetNameFromUrn } from '../common/format';

/** Narrows an arbitrary workflow payload to a DocumentationDraft when it has the expected shape. */
export function draftFromPayload(payload: Record<string, unknown> | undefined): DocumentationDraft | null {
    const candidate = (payload && (payload.draft ?? payload)) as Partial<DocumentationDraft> | undefined;
    if (!candidate || typeof candidate !== 'object') return null;
    if (typeof candidate.description !== 'string' || !Array.isArray(candidate.columns)) return null;
    return {
        summary: candidate.summary ?? '',
        description: candidate.description,
        columns: candidate.columns as ColumnDraft[],
        suggested_terms: candidate.suggested_terms ?? [],
        confidence: typeof candidate.confidence === 'number' ? candidate.confidence : 0,
        unknowns: candidate.unknowns ?? [],
    };
}

export interface DraftViewProps {
    draft: DocumentationDraft;
}

/** Read-only rendering of an AI documentation draft. */
export function DraftView({ draft }: DraftViewProps) {
    const pct = Math.round(Math.max(0, Math.min(1, draft.confidence)) * 100);
    return (
        <Space direction="vertical" style={{ width: '100%' }} size="middle">
            <Descriptions size="small" column={1} bordered>
                <Descriptions.Item label="Summary">{draft.summary || '—'}</Descriptions.Item>
                <Descriptions.Item label="Description">
                    <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>
                        {draft.description}
                    </Typography.Paragraph>
                </Descriptions.Item>
                <Descriptions.Item label="Confidence">
                    <Progress
                        percent={pct}
                        size="small"
                        status={pct < 50 ? 'exception' : 'normal'}
                        style={{ maxWidth: 240 }}
                        aria-label={`Model confidence ${pct}%`}
                    />
                </Descriptions.Item>
                <Descriptions.Item label="Suggested terms">
                    {draft.suggested_terms.length ? draft.suggested_terms.map((t) => <Tag key={t}>{t}</Tag>) : '—'}
                </Descriptions.Item>
            </Descriptions>
            {draft.unknowns.length > 0 && (
                <Alert
                    type="warning"
                    showIcon
                    message="The model could not determine"
                    description={
                        <ul style={{ margin: 0, paddingLeft: 18 }}>
                            {draft.unknowns.map((u) => (
                                <li key={u}>{u}</li>
                            ))}
                        </ul>
                    }
                />
            )}
            <Table<ColumnDraft>
                size="small"
                rowKey="field_path"
                dataSource={draft.columns}
                pagination={{ pageSize: 25, hideOnSinglePage: true }}
                title={() => `Columns (${draft.columns.length})`}
                columns={[
                    { title: 'Field', dataIndex: 'field_path', width: 240, render: (v: string) => <code>{v}</code> },
                    { title: 'Description', dataIndex: 'description' },
                    {
                        title: 'Terms',
                        dataIndex: 'suggested_terms',
                        width: 220,
                        render: (terms: string[]) => (terms ?? []).map((t) => <Tag key={t}>{t}</Tag>),
                    },
                ]}
            />
        </Space>
    );
}

export interface DocDraftReviewProps {
    /** Review an existing DOCUMENTATION workflow. */
    workflow?: Workflow;
    /** Or start by generating a draft for this dataset. */
    datasetUrn?: string;
    onTransition?: (wf: Workflow) => void;
}

/**
 * Generate an AI documentation draft for a dataset and/or review it. Drafts are never written to
 * Cairn Core directly: approving the DOCUMENTATION workflow is what publishes them.
 */
export function DocDraftReview({ workflow, datasetUrn, onTransition }: DocDraftReviewProps) {
    const client = useDheClient();
    const [urn, setUrn] = useState(datasetUrn ?? workflow?.entity_urn ?? '');
    const [generated, setGenerated] = useState<{ workflowId: string; draft: DocumentationDraft } | null>(null);
    const [comment, setComment] = useState('');
    const generate = useMutation((u: string) => client.generateDocs(u));
    const transition = useMutation((id: string, action: 'approve' | 'reject' | 'request-changes') =>
        client.transitionWorkflow(id, action, comment || undefined),
    );

    const draft = generated?.draft ?? draftFromPayload(workflow?.payload);
    const workflowId = generated?.workflowId ?? workflow?.id;
    const reviewable = !workflow || workflow.state === 'SUBMITTED' || workflow.state === 'IN_REVIEW';

    const act = async (action: 'approve' | 'reject' | 'request-changes') => {
        if (!workflowId) return;
        try {
            const wf = await transition.mutate(workflowId, action);
            message.success(`Proposal ${wf.state.toLowerCase().replace('_', ' ')}`);
            setComment('');
            onTransition?.(wf);
        } catch (e) {
            message.error((e as Error).message);
        }
    };

    return (
        <Card
            size="small"
            title={urn ? `AI documentation — ${datasetNameFromUrn(urn)}` : 'AI documentation'}
            extra={workflow && <Tag>{workflow.state}</Tag>}
        >
            <Space direction="vertical" style={{ width: '100%' }} size="middle">
                {!workflow && (
                    <Space.Compact style={{ width: '100%' }}>
                        <Input
                            aria-label="Dataset URN"
                            value={urn}
                            onChange={(e) => setUrn(e.target.value)}
                            placeholder="urn:li:dataset:(urn:li:dataPlatform:snowflake,db.schema.table,PROD)"
                        />
                        <Button
                            type="primary"
                            loading={generate.pending}
                            disabled={!urn.trim()}
                            onClick={async () => {
                                try {
                                    const res = await generate.mutate(urn.trim());
                                    setGenerated({ workflowId: res.workflow_id, draft: res.draft });
                                } catch {
                                    // surfaced via generate.error
                                }
                            }}
                        >
                            Generate draft
                        </Button>
                    </Space.Compact>
                )}
                {generate.error && <Alert type="error" showIcon message={generate.error.message} />}
                {!draft && workflow && <Alert type="warning" showIcon message="This proposal has no readable draft payload." />}
                {draft && <DraftView draft={draft} />}
                {draft && workflowId && reviewable && (
                    <>
                        <Input.TextArea
                            aria-label="Review comment"
                            placeholder="Comment (optional)"
                            rows={2}
                            value={comment}
                            onChange={(e) => setComment(e.target.value)}
                        />
                        <Space>
                            <Button type="primary" loading={transition.pending} onClick={() => act('approve')}>
                                Approve &amp; publish
                            </Button>
                            <Button loading={transition.pending} onClick={() => act('request-changes')}>
                                Request changes
                            </Button>
                            <Button danger loading={transition.pending} onClick={() => act('reject')}>
                                Reject
                            </Button>
                        </Space>
                        {generated && (
                            <Typography.Text type="secondary">
                                Proposal <code>{generated.workflowId}</code> was created. Reviewers can also find it in the
                                certification inbox.
                            </Typography.Text>
                        )}
                    </>
                )}
            </Space>
        </Card>
    );
}

export default DocDraftReview;
