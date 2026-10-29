import {
    Alert,
    Button,
    Checkbox,
    Input,
    InputNumber,
    List,
    Modal,
    Popconfirm,
    Select,
    Space,
    Switch,
    Table,
    Tabs,
    Tag,
    Typography,
} from 'antd';
import React, { useState } from 'react';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { AgentCall, AgentScope, AIDatasetRead, AiAgent } from '../api/types';
import { DatasetsRead } from './AiUsage';
import { Muted, SectionTitle } from './tokens';

const SCOPES: { value: AgentScope; label: string }[] = [
    { value: 'read', label: 'Read the catalog' },
    { value: 'query_data', label: 'Query data (masked as for the person)' },
    { value: 'propose', label: 'Propose changes for review' },
    { value: 'write', label: 'Change metadata directly' },
];

const when = (ms: number | null | undefined) => (ms ? new Date(ms).toLocaleString() : 'never');
const STATUS_COLOR = { ok: 'green', error: 'red', refused: 'orange', forbidden: 'orange' } as const;

/** An agent's recent tool calls: all of them for administrators, the caller's own otherwise. */
function ActivityButton({ agent, all }: { agent: AiAgent; all: boolean }) {
    const [open, setOpen] = useState(false);
    const { client } = useConsumerApi();
    const [calls, setCalls] = useState<AgentCall[] | null>(null);
    const [datasets, setDatasets] = useState<AIDatasetRead[] | null>(null);
    return (
        <>
            <Button
                size="small"
                onClick={async () => {
                    setOpen(true);
                    const [c, d] = await Promise.all([
                        client.aiAgentActivity(agent.id, all),
                        client.aiAgentDatasets(agent.id, all),
                    ]);
                    setCalls(c);
                    setDatasets(d);
                }}
            >
                Activity
            </Button>
            <Modal
                open={open}
                title={`What ${agent.name} did`}
                footer={null}
                width={860}
                onCancel={() => setOpen(false)}
            >
                <Tabs
                    items={[
                        {
                            key: 'calls',
                            label: 'Tool calls',
                            children: (
                                <Table
                                    size="small"
                                    rowKey="id"
                                    loading={calls === null}
                                    dataSource={calls ?? []}
                                    pagination={{ pageSize: 15, hideOnSinglePage: true }}
                                    locale={{ emptyText: 'No tool calls yet.' }}
                                    columns={[
                                        { title: 'When', render: (_, c) => new Date(c.created_ms).toLocaleString() },
                                        { title: 'Tool', dataIndex: 'tool' },
                                        ...(all
                                            ? [{ title: 'For', render: (_: unknown, c: AgentCall) => c.actor.split(':').pop() }]
                                            : []),
                                        {
                                            title: 'Outcome',
                                            render: (_, c) => (
                                                <>
                                                    <Tag color={STATUS_COLOR[c.status]}>{c.status}</Tag>
                                                    {c.flagged > 0 && <Tag color="volcano">flagged text</Tag>}
                                                </>
                                            ),
                                        },
                                        { title: 'Time', render: (_, c) => `${c.duration_ms} ms` },
                                        { title: 'Arguments', render: (_, c) => <Muted style={{ fontSize: 12 }}>{c.args}</Muted> },
                                    ]}
                                />
                            ),
                        },
                        {
                            key: 'datasets',
                            label: 'Datasets read',
                            children: <DatasetsRead rows={datasets} />,
                        },
                    ]}
                />
            </Modal>
        </>
    );
}

function ScopeTags({ scopes }: { scopes: AgentScope[] }) {
    return (
        <>
            {scopes.map((s) => (
                <Tag key={s} color={s === 'write' ? 'orange' : undefined}>
                    {s.replace('_', ' ')}
                </Tag>
            ))}
        </>
    );
}

/** Agents acting for me: AI tools I connected (OAuth) and automations I own. */
function MyAgents() {
    const { client } = useConsumerApi();
    const q = useDheQuery<AiAgent[]>('/me/agents', (c, signal) => c.myAiAgents({ signal }));
    return (
        <section aria-label="My agents">
            <SectionTitle as="h3">Connected to you</SectionTitle>
            <Muted>These act as you, never with more access than you have, and only within the scopes shown.</Muted>
            <List
                dataSource={q.data ?? []}
                locale={{ emptyText: 'No AI agents act for you.' }}
                renderItem={(a) => (
                    <List.Item
                        actions={[
                            <ActivityButton key="act" agent={a} all={false} />,
                            <Popconfirm
                                key="off"
                                title={`Disconnect ${a.name}? It must sign in again to act for you.`}
                                onConfirm={async () => {
                                    await client.disconnectAiAgent(a.id);
                                    q.refresh();
                                }}
                            >
                                <Button size="small">Disconnect</Button>
                            </Popconfirm>,
                        ]}
                    >
                        <List.Item.Meta
                            title={
                                <span>
                                    {a.name} <Tag>{a.kind === 'oauth' ? 'signed in' : 'automation key'}</Tag>
                                </span>
                            }
                            description={
                                <>
                                    <ScopeTags scopes={a.granted_scopes ?? a.scopes} />
                                    <Muted>
                                        {' '}
                                        · since {when(a.since_ms)} · last used {when(a.last_used_ms)}
                                    </Muted>
                                </>
                            }
                        />
                    </List.Item>
                )}
            />
        </section>
    );
}

/** Administrators: every agent, automation keys, scopes, enabling and rotation. */
function AllAgents() {
    const { client } = useConsumerApi();
    const q = useDheQuery<AiAgent[]>('/admin/agents', (c, signal) => c.listAiAgents({ signal }));
    const hub = useDheQuery<{ paused: boolean }>('/admin/agents/settings', (c, signal) => c.agentSettings({ signal }));
    const [name, setName] = useState('');
    const [owner, setOwner] = useState('');
    const [scopes, setScopes] = useState<AgentScope[]>(['read']);
    const [key, setKey] = useState<{ name: string; key: string } | null>(null);
    const [error, setError] = useState<string | null>(null);

    const run = async (fn: () => Promise<unknown>) => {
        setError(null);
        try {
            await fn();
            q.refresh();
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        }
    };

    return (
        <section aria-label="All agents" style={{ marginTop: 32 }}>
            <SectionTitle as="h3">All agents</SectionTitle>
            {hub.data?.paused && (
                <Alert
                    type="warning"
                    showIcon
                    style={{ marginBottom: 12 }}
                    message="Every AI agent is paused: their credentials are refused until you resume them."
                />
            )}
            <Space style={{ marginBottom: 12 }}>
                <Switch
                    aria-label="Pause all agents"
                    checked={!!hub.data?.paused}
                    onChange={(v) =>
                        void run(async () => {
                            await client.pauseAgents(v);
                            hub.refresh();
                        })
                    }
                />
                <span>Pause all agents</span>
            </Space>
            <List
                dataSource={q.data ?? []}
                locale={{ emptyText: 'No agents yet. AI tools appear here when someone connects one.' }}
                renderItem={(a) => (
                    <List.Item
                        actions={[
                            <Switch
                                key="on"
                                aria-label={`Enable ${a.name}`}
                                checked={a.enabled}
                                onChange={(v) => void run(() => client.updateAiAgent(a.id, { enabled: v }))}
                            />,
                            ...(a.kind === 'key'
                                ? [
                                      <Button
                                          key="rot"
                                          size="small"
                                          onClick={() =>
                                              void run(async () => {
                                                  const r = await client.rotateAiAgentKey(a.id);
                                                  setKey({ name: r.name, key: r.key ?? '' });
                                              })
                                          }
                                      >
                                          New key
                                      </Button>,
                                  ]
                                : []),
                            <ActivityButton key="act" agent={a} all />,
                            <Popconfirm
                                key="rm"
                                title={`Delete ${a.name}? Its credentials stop working.`}
                                onConfirm={() => void run(() => client.deleteAiAgent(a.id))}
                            >
                                <Button danger size="small">
                                    Delete
                                </Button>
                            </Popconfirm>,
                        ]}
                    >
                        <List.Item.Meta
                            title={
                                <span>
                                    {a.name} <Tag>{a.kind === 'oauth' ? 'OAuth client' : 'automation key'}</Tag>
                                    {!a.enabled && <Tag color="red">disabled</Tag>}
                                </span>
                            }
                            description={
                                <Space direction="vertical" size={4}>
                                    <Checkbox.Group
                                        aria-label={`Scopes of ${a.name}`}
                                        options={SCOPES}
                                        value={a.scopes}
                                        onChange={(v) =>
                                            void run(() => client.updateAiAgent(a.id, { scopes: v as AgentScope[] }))
                                        }
                                    />
                                    <Space wrap size={8}>
                                        <Select
                                            aria-label={`Autonomy of ${a.name}`}
                                            size="small"
                                            style={{ width: 230 }}
                                            value={a.autonomy ?? 'suggest'}
                                            options={[
                                                { value: 'suggest', label: 'Suggestions wait for review' },
                                                { value: 'auto', label: 'Apply confident suggestions' },
                                            ]}
                                            onChange={(v) =>
                                                void run(() => client.updateAiAgent(a.id, { autonomy: v }))
                                            }
                                        />
                                        {a.autonomy === 'auto' && (
                                            <InputNumber
                                                aria-label={`Auto-apply confidence of ${a.name}`}
                                                size="small"
                                                min={0.5}
                                                max={1}
                                                step={0.05}
                                                value={a.auto_accept_at}
                                                addonBefore="at"
                                                onChange={(v) =>
                                                    v != null &&
                                                    void run(() =>
                                                        client.updateAiAgent(a.id, { auto_accept_at: Number(v) }),
                                                    )
                                                }
                                            />
                                        )}
                                        <InputNumber
                                            aria-label={`Requests a minute for ${a.name}`}
                                            size="small"
                                            min={1}
                                            value={a.per_minute}
                                            addonAfter="/ min"
                                            onChange={(v) =>
                                                v != null &&
                                                void run(() => client.updateAiAgent(a.id, { per_minute: Number(v) }))
                                            }
                                        />
                                        <InputNumber
                                            aria-label={`Requests a day for ${a.name}`}
                                            size="small"
                                            min={1}
                                            value={a.daily_requests}
                                            addonAfter="/ day"
                                            onChange={(v) =>
                                                v != null &&
                                                void run(() =>
                                                    client.updateAiAgent(a.id, { daily_requests: Number(v) }),
                                                )
                                            }
                                        />
                                    </Space>
                                    <Muted>
                                        {a.owner ? `acts as ${a.owner} · ` : 'acts as whoever signs in · '}
                                        {a.requests_today ?? 0} request(s) today · last used {when(a.last_used_ms)}
                                        {a.ai_system ? ` · AI system ${a.ai_system}` : ''}
                                    </Muted>
                                </Space>
                            }
                        />
                    </List.Item>
                )}
            />
            {key && (
                <Alert
                    type="success"
                    showIcon
                    style={{ margin: '16px 0' }}
                    message={`Key for ${key.name}. Copy it now: it is not shown again.`}
                    description={
                        <Typography.Text copyable code>
                            {key.key}
                        </Typography.Text>
                    }
                />
            )}
            {error && <Alert type="error" message={error} style={{ margin: '16px 0' }} />}
            <SectionTitle as="h4" style={{ marginTop: 16 }}>
                New automation key
            </SectionTitle>
            <Space direction="vertical" style={{ width: '100%' }}>
                <Input
                    aria-label="Agent name"
                    placeholder="Name, e.g. Nightly doc bot"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                />
                <Input
                    aria-label="Acts as"
                    placeholder="Acts as (urn:li:corpuser:…); empty: you"
                    value={owner}
                    onChange={(e) => setOwner(e.target.value)}
                />
                <Checkbox.Group
                    aria-label="New agent scopes"
                    options={SCOPES}
                    value={scopes}
                    onChange={(v) => setScopes(v as AgentScope[])}
                />
                <Button
                    type="primary"
                    disabled={name.trim().length < 2 || scopes.length === 0}
                    onClick={() =>
                        void run(async () => {
                            const r = await client.createAiAgent(name.trim(), scopes, owner.trim());
                            setKey({ name: r.name, key: r.key ?? '' });
                            setName('');
                            setOwner('');
                        })
                    }
                >
                    Create key
                </Button>
            </Space>
        </section>
    );
}

/** Settings → AI agents. */
export function AiAgentsSettings({ isAdmin }: { isAdmin: boolean }) {
    return (
        <div style={{ maxWidth: 860 }}>
            <MyAgents />
            {isAdmin && <AllAgents />}
        </div>
    );
}
