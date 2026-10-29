import { Alert, Button, Descriptions, Empty, Form, Input, Popconfirm, Select, Space, Table, Tag, message } from 'antd';
import React, { useState } from 'react';
import { Link } from 'react-router-dom';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { WorkspaceAsset, WorkspaceRef, WorkspaceSummary } from '../api/types';
import { serializeSearchState } from '../utils/urlState';
import { assetPath } from './Assets';
import { Muted, SectionTitle, Surface } from './tokens';

const ROLE_LABEL: Record<string, string> = { admin: 'admin', member: 'member', global_admin: 'global admin' };
const short = (urn: string) => urn.replace(/^urn:li:[A-Za-z]+:/, '');

const list = (v?: string) =>
    (v ?? '')
        .split(/[\s,]+/)
        .map((x) => x.trim())
        .filter(Boolean);

function workspaceSearch(domains: string[]): string {
    return `/search${serializeSearchState({ query: '', filters: { domains }, page: 1 })}`;
}

function NewWorkspace({ onCreated }: { onCreated: (id: string) => void }) {
    const { client } = useConsumerApi();
    const [form] = Form.useForm();
    return (
        <Form
            form={form}
            layout="inline"
            style={{ marginTop: 12, rowGap: 8 }}
            onFinish={async (v) => {
                const id = String(v.name)
                    .toLowerCase()
                    .replace(/[^a-z0-9]+/g, '-')
                    .replace(/^-+|-+$/g, '')
                    .slice(0, 60);
                try {
                    await client.putWorkspace({ id, name: v.name, domains: list(v.domains), admins: list(v.admins) });
                    form.resetFields();
                    onCreated(id);
                } catch (e) {
                    message.error((e as Error).message);
                }
            }}
        >
            <Form.Item name="name" rules={[{ required: true, message: 'Name it' }]}>
                <Input aria-label="Workspace name" placeholder="Finance" style={{ width: 180 }} />
            </Form.Item>
            <Form.Item name="domains" rules={[{ required: true, message: 'At least one domain' }]}>
                <Input aria-label="Workspace domains" placeholder="urn:li:domain:finance, …" style={{ width: 260 }} />
            </Form.Item>
            <Form.Item name="admins">
                <Input
                    aria-label="Workspace admins"
                    placeholder="admins: urn:li:corpuser:… or urn:li:corpGroup:…"
                    style={{ width: 320 }}
                />
            </Form.Item>
            <Button htmlType="submit" type="primary">
                Create workspace
            </Button>
        </Form>
    );
}

function WorkspaceDetail({ id, isAdmin, onRemoved }: { id: string; isAdmin: boolean; onRemoved: () => void }) {
    const { client } = useConsumerApi();
    const ws = useDheQuery(`/workspaces/${id}`, (c, signal) => c.workspace(id, { signal }));
    const assets = useDheQuery(`/workspaces/${id}/assets`, (c, signal) =>
        c.workspaceAssets(id, { count: 50 }, { signal }),
    );
    const [members, setMembers] = useState<string[] | null>(null);
    const [admins, setAdmins] = useState<string[] | null>(null);
    const w: WorkspaceSummary | undefined = ws.data;
    if (ws.error) return <Alert type="error" showIcon message={ws.error.message} />;
    if (!w) return <Muted>Loading…</Muted>;
    const canManage = isAdmin || w.role === 'admin';
    const save = async () => {
        try {
            await client.putWorkspace({ ...w, members: members ?? w.members, admins: admins ?? w.admins });
            message.success('Saved');
            setMembers(null);
            setAdmins(null);
            ws.refresh();
        } catch (e) {
            message.error((e as Error).message);
        }
    };
    const counts = Object.entries(w.assets);
    return (
        <Surface style={{ marginTop: 12 }}>
            <Space align="baseline" wrap>
                <SectionTitle style={{ margin: 0 }}>{w.name}</SectionTitle>
                {w.role && <Tag color="blue">{ROLE_LABEL[w.role]}</Tag>}
                <Link to={workspaceSearch(w.domains)}>Search this workspace</Link>
            </Space>
            {w.description && <p style={{ marginTop: 8 }}>{w.description}</p>}
            <Descriptions size="small" column={1} style={{ marginTop: 8 }}>
                <Descriptions.Item label="Domains">
                    {w.domains.map((d) => (
                        <Tag key={d}>{short(d)}</Tag>
                    ))}
                </Descriptions.Item>
                <Descriptions.Item label="Assets">
                    {counts.length ? counts.map(([k, n]) => `${n} ${k}${n === 1 ? '' : 's'}`).join(', ') : 'none yet'}
                    {w.unowned > 0 && <Muted>{` · ${w.unowned} without an owner`}</Muted>}
                </Descriptions.Item>
                <Descriptions.Item label="Admins can">{w.admin_permissions.join(', ')}</Descriptions.Item>
                <Descriptions.Item label="Members can">{w.member_permissions.join(', ')}</Descriptions.Item>
            </Descriptions>
            {canManage && (
                <Space direction="vertical" style={{ width: '100%', marginTop: 8 }}>
                    <label>
                        Admins
                        <Select
                            aria-label="Admins"
                            mode="tags"
                            disabled={!isAdmin}
                            value={admins ?? w.admins}
                            onChange={setAdmins}
                            style={{ width: '100%' }}
                        />
                    </label>
                    <label>
                        Members (users or groups)
                        <Select
                            aria-label="Members"
                            mode="tags"
                            value={members ?? w.members}
                            onChange={setMembers}
                            style={{ width: '100%' }}
                        />
                    </label>
                    {!isAdmin && <Muted>Only a global admin changes domains, admins and permissions.</Muted>}
                    <Space>
                        <Button
                            type="primary"
                            disabled={members === null && admins === null}
                            onClick={() => void save()}
                        >
                            Save workspace
                        </Button>
                        {isAdmin && (
                            <Popconfirm
                                title={`Remove ${w.name}? Its admins and members lose what it granted.`}
                                onConfirm={async () => {
                                    await client.deleteWorkspace(w.id);
                                    onRemoved();
                                }}
                            >
                                <Button danger>Remove workspace</Button>
                            </Popconfirm>
                        )}
                    </Space>
                </Space>
            )}
            <Table<WorkspaceAsset>
                style={{ marginTop: 12 }}
                size="small"
                rowKey="urn"
                loading={assets.loading}
                dataSource={assets.data?.items ?? []}
                pagination={{ pageSize: 10, hideOnSinglePage: true }}
                locale={{ emptyText: 'Nothing in these domains yet.' }}
                columns={[
                    {
                        title: 'Asset',
                        render: (_: unknown, r: WorkspaceAsset) => <Link to={assetPath(r.urn)}>{short(r.urn)}</Link>,
                    },
                    { title: 'Type', render: (_: unknown, r: WorkspaceAsset) => r.type },
                    { title: 'Domain', render: (_: unknown, r: WorkspaceAsset) => short(r.domain) },
                    {
                        title: 'Owners',
                        render: (_: unknown, r: WorkspaceAsset) =>
                            r.owners.length ? r.owners.map(short).join(', ') : <Tag color="orange">no owner</Tag>,
                    },
                ]}
            />
        </Surface>
    );
}

/** Settings → Workspaces: the business units you belong to; global admins create them and assign domains. */
export function WorkspacesSettings({ isAdmin }: { isAdmin: boolean }) {
    const list = useDheQuery('/workspaces', (c, signal) => c.workspaces({ signal }));
    const [selected, setSelected] = useState<string | null>(null);
    const rows: WorkspaceRef[] = list.data ?? [];
    const current = selected ?? rows[0]?.id ?? null;
    return (
        <>
            <Muted>
                A workspace is a business unit: its domains, the admins who govern them, and the people who work there.
            </Muted>
            {rows.length === 0 && !list.loading ? (
                <Empty style={{ marginTop: 16 }} description="You are not in a workspace yet." />
            ) : (
                <Space wrap style={{ marginTop: 12 }}>
                    {rows.map((w) => (
                        <Button
                            key={w.id}
                            type={w.id === current ? 'primary' : 'default'}
                            onClick={() => setSelected(w.id)}
                        >
                            {w.name} <Muted style={{ marginLeft: 6 }}>{ROLE_LABEL[w.role]}</Muted>
                        </Button>
                    ))}
                </Space>
            )}
            {current && (
                <WorkspaceDetail
                    key={current}
                    id={current}
                    isAdmin={isAdmin}
                    onRemoved={() => {
                        setSelected(null);
                        list.refresh();
                    }}
                />
            )}
            {isAdmin && (
                <NewWorkspace
                    onCreated={(id) => {
                        list.refresh();
                        setSelected(id);
                    }}
                />
            )}
        </>
    );
}
