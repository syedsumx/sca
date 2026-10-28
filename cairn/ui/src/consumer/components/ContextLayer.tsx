import { Alert, Button, Empty, Input, Modal, Select, Space, Table, Tag, message } from 'antd';
import React, { useState } from 'react';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { ContextDefinition, DefinitionEdits, Finding, PastAnswer } from '../api/types';
import { actorName, shortUrn } from '../utils/format';
import { download } from './BulkEdit';
import { Muted } from './tokens';

export const DEFINABLE = ['METRIC_DEFINITION', 'TERM_SUGGESTION', 'JOIN_RELATIONSHIP'];

/** Refine a metric or join before it becomes an approved, versioned definition. */
export function ApproveDefinition({
    finding,
    open,
    onClose,
    onApprove,
}: {
    finding: Finding;
    open: boolean;
    onClose: () => void;
    onApprove: (edits: DefinitionEdits) => void;
}) {
    const change = (finding.proposed_changes ?? []).find((c) => c.kind === 'create_term');
    const value = (change?.value ?? {}) as { name?: string; expression?: string };
    const [name, setName] = useState(value.name ?? '');
    const [expression, setExpression] = useState(value.expression ?? '');
    const [description, setDescription] = useState('');
    const join = finding.type === 'JOIN_RELATIONSHIP';
    return (
        <Modal
            open={open}
            title={join ? 'Approve join' : 'Approve metric definition'}
            okText="Approve"
            onCancel={onClose}
            onOk={() =>
                onApprove({
                    name: name || undefined,
                    expression: join ? undefined : expression || undefined,
                    description: description || undefined,
                })
            }
        >
            <Space direction="vertical" style={{ width: '100%' }}>
                <Input
                    aria-label="Definition name"
                    addonBefore="Name"
                    value={name}
                    placeholder={join ? 'orders_to_customers' : ''}
                    onChange={(e) => setName(e.target.value)}
                />
                {!join && (
                    <Input.TextArea
                        aria-label="Expression"
                        rows={2}
                        value={expression}
                        onChange={(e) => setExpression(e.target.value)}
                        style={{ fontFamily: 'monospace' }}
                    />
                )}
                <Input.TextArea
                    aria-label="Definition description"
                    rows={2}
                    placeholder="What it means, what it excludes"
                    value={description}
                    onChange={(e) => setDescription(e.target.value)}
                />
                <Muted>
                    Approved definitions are versioned, offered to AI agents and exported to warehouse semantic layers.
                </Muted>
            </Space>
        </Modal>
    );
}

export function DefinitionsView() {
    const { client } = useConsumerApi();
    const q = useDheQuery<ContextDefinition[]>('/context/definitions', (c, signal) => c.contextDefinitions({ signal }));
    const [source, setSource] = useState<string | undefined>();
    const sources = [...new Set((q.data ?? []).filter((d) => d.kind === 'join').map((d) => d.doc.source!))];
    const exportAs = async (format: 'snowflake' | 'databricks') => {
        try {
            const sql = await client.exportDefinitions(
                format,
                format === 'snowflake' ? 'cairn_semantic_view' : 'cairn_metrics',
                source,
            );
            download(`cairn-${format}.sql`, sql, 'application/sql;charset=utf-8');
        } catch (e) {
            message.error((e as Error).message);
        }
    };
    if (q.error) return <Alert type="error" message={q.error.message} />;
    if (q.data && !q.data.length)
        return <Empty description="No approved definitions yet. Approve metric and join findings to build them." />;
    return (
        <>
            <Space style={{ marginBottom: 12, flexWrap: 'wrap' }}>
                <Button onClick={() => void exportAs('snowflake')}>Export Snowflake semantic view</Button>
                <Select
                    aria-label="Fact table"
                    placeholder="Fact table for Databricks"
                    style={{ minWidth: 260 }}
                    value={source}
                    onChange={setSource}
                    options={sources.map((u) => ({ value: u, label: shortUrn(u) }))}
                />
                <Button disabled={!source} onClick={() => void exportAs('databricks')}>
                    Export Databricks metric view
                </Button>
            </Space>
            <Table<ContextDefinition>
                size="small"
                rowKey="id"
                dataSource={q.data ?? []}
                pagination={false}
                columns={[
                    { title: 'Name', dataIndex: 'name' },
                    { title: 'Kind', dataIndex: 'kind', render: (k: string) => <Tag>{k}</Tag> },
                    {
                        title: 'Definition',
                        render: (_, d) =>
                            d.kind === 'metric' ? (
                                <code>{d.doc.expression}</code>
                            ) : (
                                <code>
                                    {shortUrn(d.doc.source!)}.{d.doc.source_field} = {shortUrn(d.doc.destination!)}.
                                    {d.doc.destination_field}
                                </code>
                            ),
                    },
                    { title: 'Version', dataIndex: 'version', width: 80 },
                    { title: 'Approved by', dataIndex: 'approved_by', render: (a: string) => actorName(a) },
                ]}
            />
        </>
    );
}

export function PastAnswersView() {
    const { client } = useConsumerApi();
    const [question, setQuestion] = useState('');
    const [answers, setAnswers] = useState<PastAnswer[] | null>(null);
    const [busy, setBusy] = useState(false);
    const ask = async () => {
        setBusy(true);
        try {
            setAnswers(await client.pastAnswers(question));
        } catch (e) {
            message.error((e as Error).message);
        } finally {
            setBusy(false);
        }
    };
    return (
        <>
            <Input.Search
                aria-label="Question"
                placeholder="How was this answered before? e.g. revenue by region last month"
                enterButton="Find"
                loading={busy}
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                onSearch={() => question.trim().length >= 3 && void ask()}
            />
            <Muted style={{ display: 'block', margin: '6px 0 12px' }}>
                Searches the queries people ran, over tables you can see. AI agents get the same answers through MCP.
            </Muted>
            {answers && !answers.length && <Empty description="No similar question answered yet." />}
            {answers?.map((a) => (
                <section key={a.urn} aria-label={a.title} style={{ marginBottom: 16 }}>
                    <strong>{a.title}</strong> <Muted>· {a.tables.map(shortUrn).join(', ')}</Muted>
                    <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12 }}>{a.sql}</pre>
                    {a.definitions.map((d) => (
                        <Tag key={d.name} color="gold">
                            {d.name} v{d.version}
                        </Tag>
                    ))}
                </section>
            ))}
        </>
    );
}
