import React, { useEffect, useRef, useState } from 'react';
import { Alert, Button, Card, Input, List, Space, Tag, Typography } from 'antd';
import styled from 'styled-components';

import { useDheClient } from '../api/hooks';
import type { AskResponse, SearchPlan } from '../api/types';
import { PageContainer, PageHeader } from '../common/States';

interface Turn {
    id: number;
    question: string;
    response?: AskResponse;
    error?: string;
    pending: boolean;
}

const Transcript = styled.div`
    display: flex;
    flex-direction: column;
    gap: 16px;
    max-height: 64vh;
    overflow-y: auto;
    padding-right: 4px;
`;

const Question = styled.div`
    align-self: flex-end;
    background: #e6f7ff;
    border-radius: 12px 12px 2px 12px;
    padding: 8px 14px;
    max-width: 80%;
    white-space: pre-wrap;
`;

/** Human-readable chips summarizing a search plan. */
export function planChips(plan: SearchPlan): string[] {
    const chips: string[] = [];
    if (plan.query && plan.query !== '*') chips.push(`query: ${plan.query}`);
    plan.entity_types.forEach((t) => chips.push(`type: ${t}`));
    plan.filters.forEach((f) => chips.push(`${f.field} ${f.condition.toLowerCase()} ${f.values.join(' | ')}`));
    if (plan.lineage) chips.push(`${plan.lineage.direction.toLowerCase()} of ${plan.lineage.urn} (≤${plan.lineage.max_hops} hops)`);
    if (plan.sort) chips.push(`sort: ${typeof plan.sort === 'string' ? plan.sort : JSON.stringify(plan.sort)}`);
    return chips;
}

const EXAMPLES = [
    'Which certified tables in the Finance domain contain PII?',
    'What feeds the revenue dashboard and is any upstream failing quality checks?',
    'Snowflake tables owned by the growth team updated in the last week',
];

export interface AskDataHubPanelProps {
    /** Called when the user clicks a result, e.g. to navigate to the entity profile. */
    onOpenEntity?: (urn: string) => void;
    /** Render without the page chrome (e.g. inside a drawer). */
    embedded?: boolean;
}

export function AskDataHubPanel({ onOpenEntity, embedded = false }: AskDataHubPanelProps) {
    const client = useDheClient();
    const [turns, setTurns] = useState<Turn[]>([]);
    const [draft, setDraft] = useState('');
    const nextId = useRef(1);
    const bottomRef = useRef<HTMLDivElement>(null);
    const controllers = useRef<AbortController[]>([]);

    useEffect(() => () => controllers.current.forEach((c) => c.abort()), []);
    useEffect(() => {
        bottomRef.current?.scrollIntoView?.({ behavior: 'smooth', block: 'end' });
    }, [turns]);

    const ask = async (question: string) => {
        const q = question.trim();
        if (!q) return;
        const id = nextId.current++;
        setTurns((t) => [...t, { id, question: q, pending: true }]);
        setDraft('');
        const controller = new AbortController();
        controllers.current.push(controller);
        try {
            const response = await client.ask(q, { signal: controller.signal });
            setTurns((t) => t.map((x) => (x.id === id ? { ...x, response, pending: false } : x)));
        } catch (e) {
            if (controller.signal.aborted) return;
            setTurns((t) => t.map((x) => (x.id === id ? { ...x, error: (e as Error).message, pending: false } : x)));
        } finally {
            controllers.current = controllers.current.filter((c) => c !== controller);
        }
    };

    const busy = turns.some((t) => t.pending);

    const content = (
        <Space direction="vertical" style={{ width: '100%' }} size="middle">
            {turns.length === 0 && (
                <Card size="small" title="Ask about your data in plain language">
                    <Space wrap>
                        {EXAMPLES.map((ex) => (
                            <Button key={ex} size="small" onClick={() => ask(ex)}>
                                {ex}
                            </Button>
                        ))}
                    </Space>
                </Card>
            )}
            <Transcript aria-live="polite" aria-busy={busy}>
                {turns.map((t) => (
                    <React.Fragment key={t.id}>
                        <Question>{t.question}</Question>
                        <Card size="small" loading={t.pending}>
                            {t.error && <Alert type="error" showIcon message={t.error} />}
                            {t.response && <AnswerView response={t.response} onOpenEntity={onOpenEntity} />}
                        </Card>
                    </React.Fragment>
                ))}
                <div ref={bottomRef} />
            </Transcript>
            <Input.Search
                aria-label="Ask a question about your metadata"
                placeholder="Ask about datasets, lineage, owners, quality…"
                enterButton="Ask"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onSearch={ask}
                loading={busy}
                maxLength={2000}
            />
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                Answers are grounded in DataHub metadata you are allowed to see. Verify before acting on them.
            </Typography.Text>
        </Space>
    );

    if (embedded) return content;
    return (
        <PageContainer style={{ maxWidth: 1000 }}>
            <PageHeader>
                <h1>Ask DataHub</h1>
                {turns.length > 0 && <Button onClick={() => setTurns([])}>Clear</Button>}
            </PageHeader>
            {content}
        </PageContainer>
    );
}

function AnswerView({ response, onOpenEntity }: { response: AskResponse; onOpenEntity?: (urn: string) => void }) {
    const chips = planChips(response.plan);
    return (
        <Space direction="vertical" style={{ width: '100%' }}>
            <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>{response.answer}</Typography.Paragraph>
            {chips.length > 0 && (
                <div aria-label="Search plan">
                    <Typography.Text type="secondary" style={{ fontSize: 12, marginRight: 6 }}>
                        Interpreted as:
                    </Typography.Text>
                    {chips.map((c) => (
                        <Tag key={c} color="geekblue">
                            {c}
                        </Tag>
                    ))}
                </div>
            )}
            {response.results.length > 0 && (
                <List
                    size="small"
                    bordered
                    dataSource={response.results}
                    renderItem={(r) => (
                        <List.Item
                            actions={
                                onOpenEntity
                                    ? [
                                          <Button key="open" type="link" size="small" onClick={() => onOpenEntity(r.urn)}>
                                              Open
                                          </Button>,
                                      ]
                                    : undefined
                            }
                        >
                            <List.Item.Meta
                                title={
                                    <Space size={6}>
                                        <span>{r.name}</span>
                                        <Tag>{r.type}</Tag>
                                        {r.platform && <Tag color="blue">{r.platform}</Tag>}
                                        {r.score !== null && (
                                            <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                                                {r.score.toFixed(2)}
                                            </Typography.Text>
                                        )}
                                    </Space>
                                }
                                description={r.description ? r.description.slice(0, 240) : undefined}
                            />
                        </List.Item>
                    )}
                />
            )}
            {response.citations.length > 0 && (
                <div>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        Sources:
                    </Typography.Text>
                    <ol style={{ margin: '4px 0 0', paddingLeft: 20, fontSize: 12 }}>
                        {response.citations.map((c) => (
                            <li key={c}>
                                {onOpenEntity && c.startsWith('urn:') ? (
                                    <Button type="link" size="small" style={{ padding: 0 }} onClick={() => onOpenEntity(c)}>
                                        {c}
                                    </Button>
                                ) : (
                                    <code>{c}</code>
                                )}
                            </li>
                        ))}
                    </ol>
                </div>
            )}
        </Space>
    );
}

export default AskDataHubPanel;
