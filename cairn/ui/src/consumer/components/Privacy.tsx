import { Alert, Button, Form, Input, InputNumber, Popconfirm, Select, Space, Table, Tabs, Tag, message } from 'antd';
import React, { useState } from 'react';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { PrivacyFinding, PrivacyPart, PrivacyRequest, RetentionPolicy } from '../api/types';
import { download } from './BulkEdit';
import { Muted, SectionTitle, Surface } from './tokens';

const datasetName = (urn: string) => urn.split(',')[1] ?? urn;
const day = (ms: number) => new Date(ms).toLocaleDateString();
const PART_COLOR: Record<string, string> = { PENDING: 'orange', DONE: 'green', NOT_APPLICABLE: 'default' };

function Findings({ findings, parts }: { findings: PrivacyFinding[]; parts: PrivacyPart[] }) {
    const byDataset = Object.fromEntries(parts.map((p) => [p.dataset, p]));
    return (
        <Table<PrivacyFinding>
            size="small"
            rowKey="dataset"
            dataSource={findings}
            pagination={false}
            locale={{ emptyText: 'No column holds this kind of identifier.' }}
            columns={[
                { title: 'Dataset', render: (_, f) => datasetName(f.dataset) },
                { title: 'Columns', render: (_, f) => f.columns.join(', ') },
                {
                    title: 'Rows',
                    render: (_, f) => (f.searched ? f.rows : <Muted title={f.reason ?? ''}>not searched</Muted>),
                },
                { title: 'Owners', render: (_, f) => f.owners.map((o) => o.split(':').pop()).join(', ') || '—' },
                {
                    title: 'Deletion',
                    render: (_, f) =>
                        byDataset[f.dataset] ? (
                            <Tag color={PART_COLOR[byDataset[f.dataset].state]}>
                                {byDataset[f.dataset].state.replace('_', ' ').toLowerCase()}
                            </Tag>
                        ) : (
                            <Muted>—</Muted>
                        ),
                },
            ]}
        />
    );
}

function Requests() {
    const { client } = useConsumerApi();
    const list = useDheQuery('/governance/privacy/requests', (c, signal) => c.privacyRequests({ signal }));
    const [form] = Form.useForm();
    const [opened, setOpened] = useState<PrivacyRequest | null>(null);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);
    return (
        <>
            <Surface style={{ padding: 18 }}>
                <SectionTitle as="h3" style={{ fontSize: 15 }}>
                    New subject request
                </SectionTitle>
                <Form
                    form={form}
                    layout="inline"
                    initialValues={{ kind: 'access', identifier_kind: 'email', due_days: 30 }}
                    onFinish={async (v) => {
                        setBusy(true);
                        setError(null);
                        try {
                            setOpened(await client.openPrivacyRequest(v));
                            form.resetFields(['identifier', 'reference']);
                            list.refresh();
                        } catch (e) {
                            setError(e instanceof Error ? e.message : String(e));
                        } finally {
                            setBusy(false);
                        }
                    }}
                >
                    <Form.Item name="kind">
                        <Select
                            aria-label="Request kind"
                            style={{ width: 130 }}
                            options={[
                                { value: 'access', label: 'Access' },
                                { value: 'deletion', label: 'Deletion' },
                            ]}
                        />
                    </Form.Item>
                    <Form.Item name="identifier_kind">
                        <Select
                            aria-label="Identifier kind"
                            style={{ width: 140 }}
                            options={[
                                { value: 'email', label: 'Email' },
                                { value: 'phone', label: 'Phone' },
                                { value: 'customer_id', label: 'Customer id' },
                            ]}
                        />
                    </Form.Item>
                    <Form.Item name="identifier" rules={[{ required: true, message: 'Who is it about?' }]}>
                        <Input aria-label="Identifier" placeholder="jane@example.com" style={{ width: 220 }} />
                    </Form.Item>
                    <Form.Item name="reference">
                        <Input aria-label="Reference" placeholder="ticket or case" style={{ width: 150 }} />
                    </Form.Item>
                    <Form.Item name="due_days" label="Due in (days)">
                        <InputNumber aria-label="Due in days" min={1} max={90} />
                    </Form.Item>
                    <Button type="primary" htmlType="submit" loading={busy}>
                        Find this person&apos;s data
                    </Button>
                </Form>
                {error && <Alert type="error" showIcon message={error} style={{ marginTop: 8 }} />}
                {opened && (
                    <div style={{ marginTop: 12 }}>
                        <Alert
                            type={opened.kind === 'deletion' ? 'warning' : 'info'}
                            showIcon
                            message={
                                opened.kind === 'deletion'
                                    ? `${opened.parts.length} dataset owner(s) were asked to delete this person's rows.`
                                    : `Found in ${opened.findings.filter((f) => f.rows > 0).length} dataset(s).`
                            }
                            description="Cairn keeps only a masked identifier and a hash; note the identifier in your case system."
                        />
                        <Findings findings={opened.findings} parts={opened.parts} />
                    </div>
                )}
            </Surface>
            <Table<PrivacyRequest>
                style={{ marginTop: 16 }}
                rowKey="id"
                loading={list.loading}
                dataSource={list.data ?? []}
                expandable={{ expandedRowRender: (r) => <Findings findings={r.findings} parts={r.parts} /> }}
                locale={{ emptyText: 'No subject requests yet.' }}
                columns={[
                    { title: 'Opened', render: (_, r) => day(r.created_ms) },
                    { title: 'Kind', render: (_, r) => <Tag>{r.kind}</Tag> },
                    { title: 'Person', render: (_, r) => `${r.identifier} (${r.identifier_kind})` },
                    { title: 'Reference', dataIndex: 'reference' },
                    {
                        title: 'State',
                        render: (_, r) => (
                            <>
                                <Tag color={r.state === 'COMPLETED' ? 'green' : 'blue'}>{r.state.toLowerCase()}</Tag>
                                {r.overdue && <Tag color="red">overdue</Tag>}
                            </>
                        ),
                    },
                    { title: 'Due', render: (_, r) => (r.state === 'OPEN' ? day(r.due_ms) : '—') },
                    {
                        title: '',
                        render: (_, r) => (
                            <Button
                                size="small"
                                onClick={async () => download(`${r.id}.csv`, await client.privacyRequestCsv(r.id))}
                            >
                                Report
                            </Button>
                        ),
                    },
                ]}
            />
        </>
    );
}

function Retention() {
    const { client } = useConsumerApi();
    const data = useDheQuery('/governance/privacy/retention', (c, signal) => c.retention({ signal }));
    const [form] = Form.useForm();
    return (
        <>
            <Surface style={{ padding: 18 }}>
                <Form
                    form={form}
                    layout="inline"
                    onFinish={async (v: RetentionPolicy) => {
                        await client.putRetention(v);
                        form.resetFields();
                        data.refresh();
                    }}
                >
                    <Form.Item name="id" rules={[{ required: true, pattern: /^[a-z0-9][a-z0-9-]{1,38}$/ }]}>
                        <Input aria-label="Policy id" placeholder="pii-2y" style={{ width: 120 }} />
                    </Form.Item>
                    <Form.Item name="label" rules={[{ required: true }]}>
                        <Input aria-label="Classification" placeholder="Classification.PII*" style={{ width: 220 }} />
                    </Form.Item>
                    <Form.Item name="days" rules={[{ required: true }]}>
                        <InputNumber aria-label="Days" placeholder="days" min={1} />
                    </Form.Item>
                    <Form.Item name="time_column">
                        <Input aria-label="Time column" placeholder="time column (optional)" style={{ width: 170 }} />
                    </Form.Item>
                    <Button htmlType="submit">Save policy</Button>
                    <Button
                        style={{ marginLeft: 8 }}
                        onClick={async () => {
                            const s = await client.checkRetention();
                            message.info(`${s.checked} checked, ${s.breaches} breach(es), ${s.unchecked} not checked`);
                            data.refresh();
                        }}
                    >
                        Check now
                    </Button>
                </Form>
                <Space wrap style={{ marginTop: 12 }}>
                    {(data.data?.policies ?? []).map((p) => (
                        <Tag key={p.id} closable={false}>
                            {p.id}: {p.label} ≤ {p.days} days
                            <Popconfirm
                                title={`Remove ${p.id}?`}
                                onConfirm={async () => {
                                    await client.deleteRetention(p.id);
                                    data.refresh();
                                }}
                            >
                                <a style={{ marginLeft: 6 }}>remove</a>
                            </Popconfirm>
                        </Tag>
                    ))}
                </Space>
            </Surface>
            <Table
                style={{ marginTop: 16 }}
                rowKey={(r) => `${r.dataset}|${r.policy}`}
                loading={data.loading}
                dataSource={data.data?.datasets ?? []}
                locale={{ emptyText: 'Not checked yet.' }}
                columns={[
                    { title: 'Dataset', render: (_, r) => datasetName(r.dataset) },
                    { title: 'Policy', dataIndex: 'policy' },
                    {
                        title: 'State',
                        render: (_, r) => (
                            <Tag color={r.state === 'breach' ? 'red' : r.state === 'ok' ? 'green' : 'default'}>
                                {r.state}
                            </Tag>
                        ),
                    },
                    { title: 'Detail', dataIndex: 'detail' },
                    { title: 'Checked', render: (_, r) => day(r.checked_ms) },
                ]}
            />
        </>
    );
}

function Ropa() {
    const { client } = useConsumerApi();
    const data = useDheQuery('/governance/privacy/ropa', (c, signal) => c.ropa({ signal }));
    return (
        <>
            <Space style={{ marginBottom: 12 }}>
                <Muted>
                    Personal data per domain: kinds, owners, allowed purposes, retention and AI systems using it.
                </Muted>
                <Button onClick={async () => download('record-of-processing.csv', await client.ropaCsv())}>
                    Download (CSV)
                </Button>
            </Space>
            {(data.data?.domains ?? []).map((d) => (
                <Surface key={d.domain} style={{ padding: 14, marginBottom: 12 }}>
                    <SectionTitle as="h3" style={{ fontSize: 14 }}>
                        {d.domain.replace('urn:li:domain:', '')}
                    </SectionTitle>
                    <Table
                        size="small"
                        rowKey="dataset"
                        pagination={false}
                        dataSource={d.datasets}
                        columns={[
                            { title: 'Dataset', render: (_, e) => datasetName(e.dataset) },
                            { title: 'Personal data', render: (_, e) => e.personal_data.join(', ') },
                            {
                                title: 'Purposes',
                                render: (_, e) => e.purposes.join(', ') || <Muted>none allowed</Muted>,
                            },
                            { title: 'Retention', render: (_, e) => e.retention.join(', ') || <Muted>none</Muted> },
                            { title: 'AI systems', render: (_, e) => e.ai_systems.join(', ') || '—' },
                        ]}
                    />
                </Surface>
            ))}
        </>
    );
}

/** Governance → Privacy: subject requests, retention and the record of processing. */
export function PrivacyAdmin() {
    return (
        <Tabs
            items={[
                { key: 'requests', label: 'Subject requests', children: <Requests /> },
                { key: 'retention', label: 'Retention', children: <Retention /> },
                { key: 'ropa', label: 'Record of processing', children: <Ropa /> },
            ]}
        />
    );
}

/** For dataset owners: deletions waiting on them. */
export function MyPrivacyParts() {
    const { client } = useConsumerApi();
    const parts = useDheQuery('/governance/privacy/my-parts', (c, signal) => c.myPrivacyParts({ signal }));
    if (!parts.data?.length) return null;
    return (
        <Surface style={{ padding: 18, marginBottom: 16 }}>
            <SectionTitle as="h2" style={{ fontSize: 16 }}>
                Deletions waiting on you
            </SectionTitle>
            <Table
                size="small"
                rowKey={(p) => `${p.request}|${p.dataset}`}
                pagination={false}
                dataSource={parts.data}
                columns={[
                    { title: 'Dataset', render: (_, p) => datasetName(p.dataset) },
                    { title: 'Person', dataIndex: 'identifier' },
                    {
                        title: 'Due',
                        render: (_, p) => (
                            <>
                                {day(p.due_ms)} {p.overdue && <Tag color="red">overdue</Tag>}
                            </>
                        ),
                    },
                    {
                        title: 'Statement to review',
                        render: (_, p) => <code style={{ fontSize: 11 }}>{p.statement}</code>,
                    },
                    {
                        title: '',
                        render: (_, p) => (
                            <Space>
                                <Button
                                    size="small"
                                    type="primary"
                                    onClick={async () => {
                                        await client.markPrivacyPart(p.request, p.dataset, 'DONE');
                                        parts.refresh();
                                    }}
                                >
                                    Done
                                </Button>
                                <Button
                                    size="small"
                                    onClick={async () => {
                                        await client.markPrivacyPart(p.request, p.dataset, 'NOT_APPLICABLE');
                                        parts.refresh();
                                    }}
                                >
                                    Not applicable
                                </Button>
                            </Space>
                        ),
                    },
                ]}
            />
        </Surface>
    );
}
