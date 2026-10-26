import React, { useState } from 'react';
import { Alert, Button, Card, Col, Descriptions, Form, Input, Row, Select, Space, Table, Tag } from 'antd';

import { useDheClient, useMutation } from '../api/hooks';
import type { SimulateRequest, SimulateResponse, TraceEntry } from '../api/types';
import { decisionColor } from '../common/format';
import { Mono, PageContainer, PageHeader } from '../common/States';

/** Common Cairn Core privileges; free text is also accepted. */
export const COMMON_PRIVILEGES = [
    'VIEW_ENTITY_PAGE',
    'VIEW_DATASET_USAGE',
    'VIEW_DATASET_PROFILE',
    'EDIT_ENTITY',
    'EDIT_ENTITY_DOCS',
    'EDIT_ENTITY_TAGS',
    'EDIT_ENTITY_GLOSSARY_TERMS',
    'EDIT_ENTITY_OWNERS',
    'EDIT_DATASET_COL_DESCRIPTION',
    'EDIT_DATASET_COL_TAGS',
    'EDIT_LINEAGE',
    'DELETE_ENTITY',
    'MANAGE_POLICIES',
    'MANAGE_INGESTION',
];

/** Parses an optional JSON object textarea. Returns undefined for blank input; throws on invalid. */
export function parseJsonObject(label: string, text: string | undefined): Record<string, unknown> | undefined {
    if (!text || !text.trim()) return undefined;
    let parsed: unknown;
    try {
        parsed = JSON.parse(text);
    } catch (e) {
        throw new Error(`${label}: invalid JSON (${(e as Error).message})`);
    }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        throw new Error(`${label}: must be a JSON object`);
    }
    return parsed as Record<string, unknown>;
}

interface FormValues {
    actor: string;
    privilege: string[];
    resource_urn?: string;
    subject_attributes?: string;
    resource_attributes?: string;
    environment?: string;
}

export function AccessSimulator() {
    const client = useDheClient();
    const [form] = Form.useForm<FormValues>();
    const [result, setResult] = useState<SimulateResponse | null>(null);
    const [parseError, setParseError] = useState<string | null>(null);
    const { mutate, pending, error } = useMutation((req: SimulateRequest) => client.simulate(req));

    const onFinish = async (values: FormValues) => {
        setParseError(null);
        let req: SimulateRequest;
        try {
            req = {
                actor: values.actor.trim(),
                // `tags` mode returns an array; only the last entry is used.
                privilege: values.privilege[values.privilege.length - 1],
                resource_urn: values.resource_urn?.trim() || undefined,
                subject_attributes: parseJsonObject('Subject attributes', values.subject_attributes),
                resource_attributes: parseJsonObject('Resource attributes', values.resource_attributes),
                environment: parseJsonObject('Environment', values.environment),
            };
        } catch (e) {
            setParseError((e as Error).message);
            return;
        }
        try {
            setResult(await mutate(req));
        } catch {
            setResult(null);
        }
    };

    return (
        <PageContainer>
            <PageHeader>
                <h1>Access simulator</h1>
            </PageHeader>
            <Row gutter={16}>
                <Col xs={24} lg={10}>
                    <Card title="Request" size="small">
                        <Form<FormValues> form={form} layout="vertical" onFinish={onFinish} requiredMark="optional">
                            <Form.Item
                                name="actor"
                                label="Actor URN"
                                rules={[{ required: true }, { pattern: /^urn:li:corpuser:.+/, message: 'Must be a corpuser URN' }]}
                            >
                                <Input placeholder="urn:li:corpuser:jdoe" />
                            </Form.Item>
                            <Form.Item
                                name="privilege"
                                label="Privilege"
                                rules={[{ required: true, type: 'array', min: 1, message: 'Choose a privilege' }]}
                            >
                                <Select
                                    mode="tags"
                                    maxTagCount={1}
                                    placeholder="VIEW_ENTITY_PAGE"
                                    options={COMMON_PRIVILEGES.map((p) => ({ value: p, label: p }))}
                                />
                            </Form.Item>
                            <Form.Item name="resource_urn" label="Resource URN" tooltip="Leave empty for platform privileges">
                                <Input placeholder="urn:li:dataset:(urn:li:dataPlatform:snowflake,db.s.t,PROD)" />
                            </Form.Item>
                            <Form.Item
                                name="subject_attributes"
                                label="Override subject attributes (JSON)"
                                tooltip="Merged over the attributes resolved from Cairn Core, e.g. {&quot;clearance&quot;: &quot;confidential&quot;}"
                            >
                                <Input.TextArea rows={3} spellCheck={false} style={{ fontFamily: 'monospace' }} />
                            </Form.Item>
                            <Form.Item name="resource_attributes" label="Override resource attributes (JSON)">
                                <Input.TextArea rows={3} spellCheck={false} style={{ fontFamily: 'monospace' }} />
                            </Form.Item>
                            <Form.Item name="environment" label="Environment (JSON)" tooltip='e.g. {"ip": "10.1.2.3", "mfa": true}'>
                                <Input.TextArea rows={2} spellCheck={false} style={{ fontFamily: 'monospace' }} />
                            </Form.Item>
                            <Button type="primary" htmlType="submit" loading={pending}>
                                Evaluate
                            </Button>
                        </Form>
                    </Card>
                </Col>
                <Col xs={24} lg={14}>
                    <Card title="Decision" size="small" aria-live="polite">
                        {(parseError || error) && (
                            <Alert type="error" showIcon message={parseError ?? error?.message} style={{ marginBottom: 12 }} />
                        )}
                        {!result && !parseError && !error && <span>Submit a request to see the decision and trace.</span>}
                        {result && <DecisionView result={result} />}
                    </Card>
                </Col>
            </Row>
        </PageContainer>
    );
}

export function DecisionView({ result }: { result: SimulateResponse }) {
    return (
        <Space direction="vertical" style={{ width: '100%' }}>
            <Descriptions size="small" column={1} bordered>
                <Descriptions.Item label="Decision">
                    <Tag color={decisionColor(result.decision)} style={{ fontSize: 14, padding: '2px 10px' }}>
                        {result.decision}
                    </Tag>
                </Descriptions.Item>
                <Descriptions.Item label="Reason">{result.reason || '—'}</Descriptions.Item>
                <Descriptions.Item label="Deciding policies">
                    {result.policy_ids.length ? result.policy_ids.map((p) => <Tag key={p}>{p}</Tag>) : '—'}
                </Descriptions.Item>
                <Descriptions.Item label="Obligations">
                    {result.obligations.length
                        ? result.obligations.map((o, i) => (
                              // eslint-disable-next-line react/no-array-index-key
                              <div key={`${o.type}-${i}`}>
                                  <Tag color="purple">{o.type}</Tag>
                                  <Mono>{JSON.stringify(o.params)}</Mono>
                              </div>
                          ))
                        : '—'}
                </Descriptions.Item>
            </Descriptions>
            <Table<TraceEntry>
                size="small"
                rowKey={(t) => t.policy_id}
                dataSource={result.trace}
                pagination={false}
                title={() => 'Evaluation trace'}
                rowClassName={(t) => (t.matched ? 'cairn-trace-matched' : '')}
                columns={[
                    { title: 'Policy', dataIndex: 'policy_id', render: (v: string) => <Mono>{v}</Mono> },
                    {
                        title: 'Effect',
                        dataIndex: 'effect',
                        width: 90,
                        render: (e: TraceEntry['effect']) => <Tag color={e === 'deny' ? 'red' : 'green'}>{e}</Tag>,
                    },
                    {
                        title: 'Matched',
                        dataIndex: 'matched',
                        width: 90,
                        render: (m: boolean) => (m ? <Tag color="blue">yes</Tag> : <Tag>no</Tag>),
                    },
                    { title: 'Reason', dataIndex: 'reason' },
                ]}
            />
        </Space>
    );
}

export default AccessSimulator;
