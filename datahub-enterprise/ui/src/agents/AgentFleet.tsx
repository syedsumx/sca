import React, { useState } from 'react';
import {
    Alert,
    Button,
    Card,
    Form,
    Input,
    InputNumber,
    Modal,
    Popconfirm,
    Select,
    Space,
    Table,
    Tabs,
    Tag,
    Tooltip,
    Typography,
    message,
} from 'antd';

import { useDheClient, useMutation, usePolling } from '../api/hooks';
import type { Agent, AgentPool, AgentPoolInput, AgentRun, AgentStatus, EnrollmentToken } from '../api/types';
import { formatRelative, formatTimestamp } from '../common/format';
import { EmptyState, ErrorState, LoadingState, Mono, PageContainer, PageHeader } from '../common/States';

const AGENT_STATUS_COLOR: Record<AgentStatus, string> = { ONLINE: 'green', OFFLINE: 'default', REVOKED: 'red' };

export function runStatusColor(status: string): string {
    switch (status.toUpperCase()) {
        case 'SUCCESS':
        case 'SUCCEEDED':
        case 'COMPLETED':
            return 'green';
        case 'FAILURE':
        case 'FAILED':
        case 'TIMEOUT':
            return 'red';
        case 'RUNNING':
        case 'LEASED':
            return 'blue';
        case 'CANCELLED':
            return 'orange';
        default:
            return 'default';
    }
}

/** Shell snippet for starting an agent with a fresh enrollment token. */
export function enrollmentCommand(gatewayUrl: string, pool: string, token: string): string {
    return [
        'docker run -d --name dhe-remote-agent --restart unless-stopped \\',
        `  -e DHE_GATEWAY_URL=${gatewayUrl} \\`,
        `  -e DHE_AGENT_POOL=${pool} \\`,
        `  -e DHE_ENROLLMENT_TOKEN=${token} \\`,
        '  -v dhe-agent-state:/var/lib/dhe-agent \\',
        '  dhe/datahub-enterprise:latest dhe agent run',
    ].join('\n');
}

export interface AgentFleetProps {
    /** Public URL agents should use to reach the gateway; shown in the enrollment snippet. */
    gatewayUrl?: string;
    pollIntervalMs?: number;
}

export function AgentFleet({ gatewayUrl = 'https://dhe-gateway.example.com', pollIntervalMs = 15000 }: AgentFleetProps) {
    const client = useDheClient();
    const pools = usePolling((signal) => client.listPools({ signal }), pollIntervalMs, [client]);
    const agents = usePolling((signal) => client.listAgents({ signal }), pollIntervalMs, [client]);
    const runs = usePolling((signal) => client.listRuns({ signal }), pollIntervalMs, [client]);
    const [poolModal, setPoolModal] = useState(false);
    const [token, setToken] = useState<{ pool: string; token: EnrollmentToken } | null>(null);
    const tokenMutation = useMutation((pool: string) => client.createEnrollmentToken(pool));

    const issueToken = async (pool: string) => {
        try {
            const t = await tokenMutation.mutate(pool);
            setToken({ pool, token: t });
        } catch (e) {
            message.error((e as Error).message);
        }
    };

    const revoke = async (agent: Agent) => {
        try {
            const updated = await client.revokeAgent(agent.agent_id);
            agents.setData((prev) => (prev ?? []).map((a) => (a.agent_id === agent.agent_id ? { ...a, ...updated } : a)));
            message.success(`Revoked ${agent.hostname}`);
        } catch (e) {
            message.error((e as Error).message);
        }
    };

    const poolsTab = () => {
        if (pools.loading && !pools.data) return <LoadingState />;
        if (pools.error && !pools.data) return <ErrorState error={pools.error} onRetry={pools.reload} />;
        if (!pools.data?.length) {
            return (
                <EmptyState description="No agent pools yet">
                    <Button type="primary" onClick={() => setPoolModal(true)}>
                        Create a pool
                    </Button>
                </EmptyState>
            );
        }
        return (
            <Table<AgentPool>
                rowKey="name"
                dataSource={pools.data}
                pagination={false}
                columns={[
                    { title: 'Pool', dataIndex: 'name', render: (v: string) => <Mono>{v}</Mono> },
                    { title: 'Description', dataIndex: 'description' },
                    {
                        title: 'Allowed platforms',
                        dataIndex: 'allowed_platforms',
                        render: (p: string[]) => (p.length ? p.map((x) => <Tag key={x}>{x}</Tag>) : <Tag>any</Tag>),
                    },
                    { title: 'Max concurrency', dataIndex: 'max_concurrency', width: 140 },
                    {
                        title: 'Online',
                        dataIndex: 'agents_online',
                        width: 90,
                        render: (n: number) => <Tag color={n > 0 ? 'green' : 'red'}>{n}</Tag>,
                    },
                    {
                        title: '',
                        width: 180,
                        render: (_: unknown, p) => (
                            <Button size="small" loading={tokenMutation.pending} onClick={() => issueToken(p.name)}>
                                Enrollment token
                            </Button>
                        ),
                    },
                ]}
            />
        );
    };

    const agentsTab = () => {
        if (agents.loading && !agents.data) return <LoadingState />;
        if (agents.error && !agents.data) return <ErrorState error={agents.error} onRetry={agents.reload} />;
        if (!agents.data?.length) return <EmptyState description="No agents enrolled" />;
        return (
            <Table<Agent>
                rowKey="agent_id"
                dataSource={agents.data}
                pagination={{ pageSize: 25, hideOnSinglePage: true }}
                columns={[
                    {
                        title: 'Agent',
                        render: (_: unknown, a) => (
                            <Space direction="vertical" size={0}>
                                <strong>{a.hostname}</strong>
                                <Mono>{a.agent_id}</Mono>
                            </Space>
                        ),
                    },
                    { title: 'Pool', dataIndex: 'pool', render: (v: string) => <Mono>{v}</Mono> },
                    {
                        title: 'Status',
                        dataIndex: 'status',
                        width: 110,
                        render: (s: AgentStatus) => <Tag color={AGENT_STATUS_COLOR[s]}>{s}</Tag>,
                    },
                    { title: 'Version', dataIndex: 'version', width: 100 },
                    {
                        title: 'Last heartbeat',
                        dataIndex: 'last_heartbeat',
                        width: 140,
                        render: (v: string | null) => <Tooltip title={formatTimestamp(v)}>{formatRelative(v)}</Tooltip>,
                    },
                    { title: 'Running', dataIndex: 'running_tasks', width: 90 },
                    {
                        title: '',
                        width: 100,
                        render: (_: unknown, a) =>
                            a.status !== 'REVOKED' && (
                                <Popconfirm
                                    title="Revoke this agent? Its keys stop working immediately and running tasks are retried elsewhere."
                                    onConfirm={() => revoke(a)}
                                >
                                    <Button size="small" danger>
                                        Revoke
                                    </Button>
                                </Popconfirm>
                            ),
                    },
                ]}
            />
        );
    };

    const runsTab = () => {
        if (runs.loading && !runs.data) return <LoadingState />;
        if (runs.error && !runs.data) return <ErrorState error={runs.error} onRetry={runs.reload} />;
        if (!runs.data?.length) return <EmptyState description="No remote runs yet" />;
        return (
            <Table<AgentRun>
                rowKey="run_id"
                dataSource={runs.data}
                pagination={{ pageSize: 25, hideOnSinglePage: true }}
                columns={[
                    { title: 'Run', dataIndex: 'run_id', render: (v: string) => <Mono>{v}</Mono> },
                    { title: 'Pool', dataIndex: 'pool' },
                    { title: 'Agent', dataIndex: 'agent_id', render: (v: string | null) => (v ? <Mono>{v}</Mono> : '—') },
                    {
                        title: 'Status',
                        dataIndex: 'status',
                        width: 120,
                        render: (s: string) => <Tag color={runStatusColor(s)}>{s}</Tag>,
                    },
                    { title: 'Started', dataIndex: 'started_at', render: (v: string | null) => formatTimestamp(v) },
                    { title: 'Finished', dataIndex: 'finished_at', render: (v: string | null) => formatTimestamp(v) },
                    { title: 'MCPs', dataIndex: 'mcps_forwarded', width: 90 },
                    {
                        title: 'Execution request',
                        dataIndex: 'execution_request_urn',
                        render: (v: string | null) => (v ? <Mono>{v}</Mono> : '—'),
                    },
                ]}
            />
        );
    };

    return (
        <PageContainer>
            <PageHeader>
                <h1>Remote ingestion agents</h1>
                <Button type="primary" onClick={() => setPoolModal(true)}>
                    New pool
                </Button>
            </PageHeader>
            <Card size="small">
                <Tabs
                    items={[
                        { key: 'pools', label: `Pools${pools.data ? ` (${pools.data.length})` : ''}`, children: poolsTab() },
                        { key: 'agents', label: `Agents${agents.data ? ` (${agents.data.length})` : ''}`, children: agentsTab() },
                        { key: 'runs', label: 'Runs', children: runsTab() },
                    ]}
                />
            </Card>
            <CreatePoolModal
                open={poolModal}
                onClose={() => setPoolModal(false)}
                onCreated={(p) => {
                    setPoolModal(false);
                    pools.setData((prev) => [...(prev ?? []), p]);
                    message.success(`Created pool ${p.name}`);
                }}
            />
            <Modal
                open={!!token}
                title={token ? `Enrollment token for pool “${token.pool}”` : ''}
                onCancel={() => setToken(null)}
                footer={<Button onClick={() => setToken(null)}>I have copied it</Button>}
                width={680}
                destroyOnClose
            >
                {token && (
                    <Space direction="vertical" style={{ width: '100%' }}>
                        <Alert
                            type="warning"
                            showIcon
                            message="This token is shown only once"
                            description={`It can enroll one agent and expires ${formatTimestamp(
                                token.token.expires_at,
                            )}. Store it in the VPC's secret manager, not in source control.`}
                        />
                        <Typography.Paragraph copyable={{ text: token.token.token }} style={{ marginBottom: 0 }}>
                            <Mono>{token.token.token}</Mono>
                        </Typography.Paragraph>
                        <Typography.Text strong>Start an agent inside the private network:</Typography.Text>
                        <Typography.Paragraph copyable={{ text: enrollmentCommand(gatewayUrl, token.pool, token.token.token) }}>
                            <pre style={{ whiteSpace: 'pre-wrap', margin: 0 }}>
                                {enrollmentCommand(gatewayUrl, token.pool, token.token.token)}
                            </pre>
                        </Typography.Paragraph>
                    </Space>
                )}
            </Modal>
        </PageContainer>
    );
}

interface CreatePoolModalProps {
    open: boolean;
    onClose: () => void;
    onCreated: (pool: AgentPool) => void;
}

function CreatePoolModal({ open, onClose, onCreated }: CreatePoolModalProps) {
    const client = useDheClient();
    const [form] = Form.useForm<AgentPoolInput>();
    const { mutate, pending, error } = useMutation((input: AgentPoolInput) => client.createPool(input));

    return (
        <Modal
            open={open}
            title="New agent pool"
            okText="Create"
            confirmLoading={pending}
            onCancel={onClose}
            destroyOnClose
            onOk={async () => {
                try {
                    const values = await form.validateFields();
                    onCreated(await mutate({ ...values, allowed_platforms: values.allowed_platforms ?? [] }));
                    form.resetFields();
                } catch {
                    // validation or API error is displayed
                }
            }}
        >
            {error && <Alert type="error" showIcon message={error.message} style={{ marginBottom: 12 }} />}
            <Form form={form} layout="vertical" initialValues={{ max_concurrency: 2, allowed_platforms: [] }}>
                <Form.Item
                    name="name"
                    label="Pool name (used as executorId)"
                    rules={[
                        { required: true },
                        { pattern: /^[a-z0-9][a-z0-9-]{1,62}$/, message: 'Lowercase letters, digits and dashes' },
                    ]}
                >
                    <Input placeholder="finance-vpc" />
                </Form.Item>
                <Form.Item name="description" label="Description">
                    <Input />
                </Form.Item>
                <Form.Item
                    name="allowed_platforms"
                    label="Allowed platforms"
                    tooltip="Metadata for other platforms is rejected by the gateway. Leave empty to allow any."
                >
                    <Select mode="tags" placeholder="snowflake, postgres" />
                </Form.Item>
                <Form.Item name="max_concurrency" label="Max concurrent runs" rules={[{ required: true }]}>
                    <InputNumber min={1} max={64} style={{ width: '100%' }} />
                </Form.Item>
            </Form>
        </Modal>
    );
}

export default AgentFleet;
