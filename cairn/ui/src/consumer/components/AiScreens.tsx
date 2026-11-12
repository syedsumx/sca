/**
 * The AI screens (L153-L157 in the UI): where each gateway model is served, the external MCP servers Cairn reaches
 * for its agents, Connect your IDE (exactly what `cairn ide init` writes) and the example agents, ready to install.
 */
import { Alert, Button, Checkbox, Descriptions, Empty, Form, Input, InputNumber, Modal, Popconfirm, Select, Space, Switch, Table, Tag, Typography, message } from 'antd';
import React, { useMemo, useState } from 'react';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import EXAMPLES from '../resources/agentExamples.generated.json';
import { describeError } from './ErrorNotice';

// ---- AI gateway › Models ---------------------------------------------------------------------------------------

interface ServedBy { upstream: string; kind: string; region: string | null; model_id: string | null }
interface GatewayModel { id: string; served_by?: ServedBy; cairn_route?: boolean; candidates?: string[] }

const CLOUD: Record<string, string> = { bedrock: 'Amazon Bedrock', foundry: 'Azure AI Foundry', vertex: 'Google Vertex AI',
    provider: 'Provider API' };

/** "Amazon Bedrock · us-east-1", "Azure AI Foundry", "Route over 3 models" … */
export function servedByLabel(m: GatewayModel): string {
    if (m.cairn_route) return `Route over ${m.candidates?.length ?? 0} model${m.candidates?.length === 1 ? '' : 's'}`;
    const s = m.served_by;
    if (!s) return '—';
    return [CLOUD[s.kind] ?? s.kind, s.region].filter(Boolean).join(' · ');
}

export function GatewayModels() {
    const q = useDheQuery<{ data: GatewayModel[] }>('/v1/ai/gateway/models',
        (c, signal) => c.request('GET', '/v1/ai/gateway/models', undefined, { signal }));
    return (
        <section aria-label="Gateway models">
            <Typography.Paragraph type="secondary">
                The models you may call through Cairn's AI gateway, and where each runs. Cairn hosts no model: the clouds'
                model services serve them in your account and region, and Cairn signs in with its workload identity.
            </Typography.Paragraph>
            {q.error && <Alert type="error" showIcon message={describeError(q.error)} />}
            <Table<GatewayModel> size="small" rowKey="id" loading={q.loading} pagination={false} dataSource={q.data?.data ?? []}
                columns={[
                    { title: 'Model', dataIndex: 'id', render: (id: string, m) => <>{id}{m.cairn_route && <Tag style={{ marginLeft: 8 }}>route</Tag>}</> },
                    { title: 'Served by', key: 'by', render: (_: unknown, m) => servedByLabel(m) },
                    { title: "The cloud's model id", key: 'mid', render: (_: unknown, m) => m.cairn_route
                        ? (m.candidates ?? []).join(', ') : <code>{m.served_by?.model_id ?? '—'}</code> },
                ]} />
        </section>
    );
}

// ---- AI gateway › MCP servers ----------------------------------------------------------------------------------

interface McpServer {
    name: string; url: string; description: string; auth: { type: string; header?: string; scope?: string; key?: string };
    allow_tools: string[] | null; deny_tools: string[] | null; read_only: boolean; rpm: number | null; enabled: boolean;
}
interface McpTool { name: string; description?: string }

function Tools({ name }: { name: string }) {
    const q = useDheQuery<{ tools: McpTool[] }>(`/v1/ai/mcp-servers/${name}/tools`,
        (c, signal) => c.request('GET', `/v1/ai/mcp-servers/${encodeURIComponent(name)}/tools`, undefined, { signal }));
    if (q.error) return <Alert type="warning" showIcon message={describeError(q.error)} />;
    if (q.loading) return <Typography.Text type="secondary">Asking the server for its tools…</Typography.Text>;
    const tools = q.data?.tools ?? [];
    if (!tools.length) return <Typography.Text type="secondary">No tool that its registration lets through.</Typography.Text>;
    return (
        <ul style={{ margin: 0, paddingLeft: 18 }}>
            {tools.map((t) => <li key={t.name}><code>{`${name}__${t.name}`}</code>{t.description ? ` · ${t.description}` : ''}</li>)}
        </ul>
    );
}

const splitList = (v?: string) => (v ?? '').split(',').map((x) => x.trim()).filter(Boolean);

export function McpServers() {
    const { client } = useConsumerApi();
    const q = useDheQuery<{ servers: McpServer[] }>('/v1/ai/mcp-servers',
        (c, signal) => c.request('GET', '/v1/ai/mcp-servers', undefined, { signal }));
    const [open, setOpen] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [form] = Form.useForm();
    const auth = Form.useWatch('auth', form);

    const save = async () => {
        const v = await form.validateFields();
        const body = {
            url: v.url, description: v.description ?? '', read_only: !!v.read_only, rpm: v.rpm ?? null,
            auth: v.auth === 'none' ? { type: 'none' } : { type: v.auth, scope: v.scope, key: v.key, ...(v.auth === 'header' ? { header: v.header } : {}) },
            allow_tools: splitList(v.allow_tools).length ? splitList(v.allow_tools) : null,
            deny_tools: splitList(v.deny_tools).length ? splitList(v.deny_tools) : null,
        };
        try {
            await client.request('PUT', `/v1/ai/mcp-servers/${encodeURIComponent(v.name)}`, body);
            message.success(`${v.name} registered`);
            setOpen(false);
            form.resetFields();
            q.refresh();
        } catch (e) {
            setError(describeError(e));
        }
    };
    return (
        <section aria-label="MCP servers">
            <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 12 }}>
                <Typography.Text type="secondary">
                    Other MCP servers (an issue tracker, a code host, a search engine) that Cairn's agents and your IDE reach
                    through Cairn, as you: only the tools a registration lets through, read-only if it says so, rate-limited
                    and audited. Credentials stay in secret scopes.
                </Typography.Text>
                <Button type="primary" onClick={() => { setError(null); setOpen(true); }}>Register a server</Button>
            </Space>
            {((error && !open) || q.error) && <Alert type="error" showIcon message={error ?? describeError(q.error)} style={{ marginBottom: 12 }} />}
            <Table<McpServer> size="small" rowKey="name" loading={q.loading} pagination={false} dataSource={q.data?.servers ?? []}
                locale={{ emptyText: <Empty description="No MCP server is registered yet." /> }}
                expandable={{ expandedRowRender: (s) => <Tools name={s.name} /> }}
                columns={[
                    { title: 'Name', dataIndex: 'name' },
                    { title: 'URL', dataIndex: 'url', render: (u: string) => <code>{u}</code> },
                    { title: 'Sign-in', key: 'auth', render: (_: unknown, s) => s.auth.type === 'none' ? 'none'
                        : `${s.auth.type} from ${s.auth.scope}/${s.auth.key}` },
                    { title: 'Rules', key: 'rules', render: (_: unknown, s) => (
                        <Space size={4} wrap>
                            {s.read_only && <Tag>read-only</Tag>}
                            {s.deny_tools?.length ? <Tag>deny {s.deny_tools.join(', ')}</Tag> : null}
                            {s.allow_tools?.length ? <Tag>allow {s.allow_tools.join(', ')}</Tag> : null}
                            {s.rpm ? <Tag>{s.rpm}/min</Tag> : null}
                        </Space>) },
                    { title: '', key: 'x', render: (_: unknown, s) => (
                        <Popconfirm title={`Remove ${s.name}? Agents and IDEs lose its tools at once.`}
                            onConfirm={async () => {
                                try { await client.request('DELETE', `/v1/ai/mcp-servers/${encodeURIComponent(s.name)}`); q.refresh(); }
                                catch (e) { setError(describeError(e)); }
                            }}>
                            <Button size="small" danger>Remove</Button>
                        </Popconfirm>) },
                ]} />
            <Modal open={open} title="Register an MCP server" okText="Register" onOk={() => void save()} onCancel={() => setOpen(false)}>
                {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
                <Form form={form} layout="vertical" initialValues={{ auth: 'none', read_only: false }}>
                    <Form.Item name="name" label="Name" rules={[{ required: true, pattern: /^[a-z][a-z0-9-]{1,39}$/, message: 'lowercase letters, digits and -' }]}>
                        <Input placeholder="tracker" />
                    </Form.Item>
                    <Form.Item name="url" label="URL" rules={[{ required: true }]}><Input placeholder="https://mcp.example.com/mcp" /></Form.Item>
                    <Form.Item name="description" label="Description"><Input /></Form.Item>
                    <Form.Item name="auth" label="Sign-in">
                        <Select options={[{ value: 'none', label: 'None' }, { value: 'bearer', label: 'Bearer token from a secret scope' },
                            { value: 'header', label: 'A header from a secret scope' }]} />
                    </Form.Item>
                    {auth && auth !== 'none' && (
                        <Space>
                            {auth === 'header' && <Form.Item name="header" label="Header" rules={[{ required: true }]}><Input placeholder="X-Api-Key" /></Form.Item>}
                            <Form.Item name="scope" label="Secret scope" rules={[{ required: true }]}><Input /></Form.Item>
                            <Form.Item name="key" label="Key" rules={[{ required: true }]}><Input /></Form.Item>
                        </Space>
                    )}
                    <Form.Item name="deny_tools" label="Deny tools (comma-separated, * allowed)"><Input placeholder="delete_*" /></Form.Item>
                    <Form.Item name="allow_tools" label="Allow only these tools"><Input /></Form.Item>
                    <Space>
                        <Form.Item name="read_only" label="Read-only" valuePropName="checked"><Switch /></Form.Item>
                        <Form.Item name="rpm" label="Calls per minute"><InputNumber min={1} /></Form.Item>
                    </Space>
                </Form>
            </Modal>
        </section>
    );
}

// ---- Connect your IDE ------------------------------------------------------------------------------------------

interface IdeConfig {
    client: string; url: string; file: string; content: string; command: string; secret: string | null;
    clients: string[]; toolsets: string[]; default_toolsets: string[];
}

const CLIENT_LABEL: Record<string, string> = { vscode: 'VS Code (GitHub Copilot)', 'copilot-agent': 'Copilot coding agent',
    'claude-code': 'Claude Code', cursor: 'Cursor', jetbrains: 'JetBrains (Junie)' };

function Copyable({ label, text }: { label: string; text: string }) {
    return (
        <div style={{ position: 'relative' }}>
            <Typography.Text strong>{label}</Typography.Text>
            <Typography.Paragraph copyable={{ text }} style={{ marginBottom: 4 }} />
            <pre aria-label={label} style={{ background: '#0f172a', color: '#e2e8f0', padding: 12, borderRadius: 8, overflowX: 'auto', fontSize: 12 }}>{text}</pre>
        </div>
    );
}

export function ConnectIde() {
    const [client, setClient] = useState('vscode');
    const [toolsets, setToolsets] = useState<string[] | null>(null);
    const base = typeof window !== 'undefined' ? window.location.origin : '';
    const query = useMemo(() => {
        const p = new URLSearchParams({ client, base });
        if (toolsets) p.set('toolsets', toolsets.join(','));
        return p.toString();
    }, [client, toolsets, base]);
    const q = useDheQuery<IdeConfig>(`/v1/ai/ide-config?${query}`,
        (c, signal) => c.request('GET', `/v1/ai/ide-config?${query}`, undefined, { signal }));
    const cfg = q.data;
    const chosen = toolsets ?? cfg?.default_toolsets ?? [];
    return (
        <section aria-label="Connect your IDE" style={{ maxWidth: 900 }}>
            <Typography.Paragraph type="secondary">
                Use Cairn from your editor's AI assistant: every Cairn operation is a tool, run as you with your permissions.
                Choose your editor and the toolsets it should see, then add this file to your repository (or run the command).
                The editor signs you in to Cairn the first time.
            </Typography.Paragraph>
            <Space direction="vertical" style={{ width: '100%' }} size={12}>
                <Space wrap>
                    <Typography.Text strong>Editor</Typography.Text>
                    <Select aria-label="Editor" value={client} onChange={setClient} style={{ minWidth: 240 }}
                        options={(cfg?.clients ?? Object.keys(CLIENT_LABEL)).map((c) => ({ value: c, label: CLIENT_LABEL[c] ?? c }))} />
                </Space>
                <div>
                    <Typography.Text strong>Toolsets</Typography.Text>
                    <div style={{ marginTop: 6 }}>
                        <Checkbox.Group value={chosen} onChange={(v) => setToolsets(v as string[])}
                            options={(cfg?.toolsets ?? []).map((t) => ({ value: t, label: t }))} />
                    </div>
                </div>
                {q.error && <Alert type="error" showIcon message={describeError(q.error)} />}
                {cfg && (
                    <>
                        <Copyable label={cfg.file} text={cfg.content} />
                        <Copyable label="Or run" text={cfg.command} />
                        {cfg.secret && (
                            <Alert type="info" showIcon message={`The coding agent signs in with a Cairn token: add it to the repository's Copilot environment as the secret ${cfg.secret}.`} />
                        )}
                        <Descriptions size="small" column={1}>
                            <Descriptions.Item label="Endpoint"><code>{cfg.url}</code></Descriptions.Item>
                        </Descriptions>
                    </>
                )}
            </Space>
        </section>
    );
}

// ---- Agents › Examples -----------------------------------------------------------------------------------------

interface Example {
    agent: { name: string; description: string; instructions: string; tools: string[]; scopes: string[]; max_steps?: number };
    cases: number;
    source: string;
}

export function AgentExamples() {
    const { client } = useConsumerApi();
    const q = useDheQuery<{ name: string }[] | { agents: { name: string }[] }>('/v1/custom-agents',
        (c, signal) => c.request('GET', '/v1/custom-agents', undefined, { signal }));
    const installed = new Set((Array.isArray(q.data) ? q.data : q.data?.agents ?? []).map((a) => a.name));
    const [busy, setBusy] = useState<string | null>(null);
    const [error, setError] = useState<string | null>(null);
    const install = async (e: Example) => {
        setBusy(e.agent.name);
        setError(null);
        try {
            await client.request('POST', '/v1/custom-agents', e.agent);
            message.success(`${e.agent.name} installed: deploy it under Custom agents when it suits you`);
            q.refresh();
        } catch (err) {
            setError(describeError(err));
        } finally {
            setBusy(null);
        }
    };
    return (
        <section aria-label="Example agents">
            <Typography.Paragraph type="secondary">
                Ready-made agents, each tested on its own cases. Installing one creates it as a custom agent you own: read
                its instructions and tools, change what you like, and deploy it when ready. They work as the person asking,
                with that person's permissions.
            </Typography.Paragraph>
            {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
            <ul style={{ listStyle: 'none', padding: 0, margin: 0, display: 'grid', gap: 12,
                gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))' }}>
                {(EXAMPLES as Example[]).map((e) => (
                    <li key={e.agent.name} style={{ background: '#fff', border: '1px solid #e5e7eb', borderRadius: 10, padding: 16,
                        display: 'flex', flexDirection: 'column', gap: 8 }}>
                        <Typography.Text strong>{e.agent.name}</Typography.Text>
                        <Typography.Text type="secondary">{e.agent.description}</Typography.Text>
                        <div>{e.agent.tools.map((t) => <Tag key={t} style={{ marginBottom: 4 }}>{t}</Tag>)}</div>
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                            Scopes: {e.agent.scopes.join(', ')} · tested on {e.cases} case{e.cases === 1 ? '' : 's'}
                        </Typography.Text>
                        <details>
                            <summary>Instructions</summary>
                            <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12 }}>{e.agent.instructions}</pre>
                        </details>
                        <div style={{ marginTop: 'auto' }}>
                            {installed.has(e.agent.name)
                                ? <Tag color="green">installed</Tag>
                                : <Button type="primary" loading={busy === e.agent.name} onClick={() => void install(e)}>Install {e.agent.name}</Button>}
                        </div>
                    </li>
                ))}
            </ul>
        </section>
    );
}
