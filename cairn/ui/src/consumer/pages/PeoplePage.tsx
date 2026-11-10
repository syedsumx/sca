import { Alert, Button, Descriptions, Drawer, Empty, Input, List, Popconfirm, Select, Space, Table, Tabs, Tag, message } from 'antd';
import React, { useMemo, useState } from 'react';
import { Link, useHistory, useLocation } from 'react-router-dom';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import { ErrorNotice, notifyError } from '../components/ErrorNotice';
import { Muted, Page, SectionTitle, Surface, tokens } from '../components/tokens';
import { pageHelp } from '../guide/help';
import { PageHeader } from '../guide/PageHeader';
import { MessageKey, useT } from '../i18n';
import { RESOURCE_SPECS } from '../resources/catalog';
import { ActionDialog } from '../resources/ResourceView';
import type { ResourceAction, ResourceSpec } from '../resources/types';
import { errorStatus } from '../utils/errors';
import { actorName } from '../utils/format';

// ---- what the API answers (cairn.identity.people) ----------------------------------------------------------------
export interface PersonRow {
    id: string;
    urn: string;
    type: 'person' | 'service';
    display_name: string;
    email?: string | null;
    title?: string | null;
    department?: string | null;
    country?: string | null;
    employment_type?: string | null;
    manager?: string | null;
    attributes: Record<string, string>;
    status: 'active' | 'deactivated';
    source: 'scim' | 'manual' | 'sso';
    groups: string[];
}
interface Paged<T> { total: number; offset: number; limit: number; items: T[] }
interface GroupRow { id: string; urn: string; name: string; description: string; source: string; members: number }
interface Reason {
    kind: 'direct' | 'group' | 'attribute_rule' | 'grant';
    group?: string;
    rules?: { attr: string; op: string; value: unknown }[];
    source?: string;
    expires_at_ms?: number | null;
}
interface HeldPersona { id: string; name: string; permissions: string[]; reasons: Reason[] }
interface PersonDetail extends Omit<PersonRow, 'groups'> {
    groups: GroupRow[];
    personas: HeldPersona[];
    persona_grants: { id: string; persona: string; source: string; expires_at_ms: number | null }[];
    workspaces: { id: string; name: string; role: string; domains: string[] }[];
    owns: { kind: string; id: string; name: string; scopes?: string[]; enabled?: boolean; user?: string }[];
    last_sign_in: { ts: string; action: string } | null;
    open_access_reviews: { review: string; review_name: string; due_ms: number; persona: string }[];
    recent_audit: { seq: number; ts: string; category: string; action: string; actor: string; subject: string | null }[];
    managed: { editable: boolean; why: string | null };
}
interface Reach {
    personas: (HeldPersona & { privileges: string[]; scope_text: string })[];
    purposes: { id: string; name: string; labels: string[]; allowed: boolean; effect: string }[];
    workspaces: { id: string; name: string; role: string; domains: string[]; permissions: string[] }[];
}
interface GroupDetail extends GroupRow {
    member_list: { id: string; display_name: string; email?: string | null; status: string; source: string }[];
    personas: { id: string; name: string }[];
    purposes: { id: string; name: string }[];
    workspaces: { id: string; name: string; role: string }[];
    managed: { editable: boolean; why: string | null };
}
interface Principal {
    type: 'agent' | 'workload' | 'service';
    id: string;
    name: string;
    owner?: string | null;
    scopes: string[];
    enabled: boolean;
    agent_kind?: string;
    purpose?: string;
    acts_as?: string;
    last_used_ms?: number | null;
}
interface Labelled { kind: string; name: string; policy_id: string; description?: string; why_not?: string }
export interface CheckAnswer {
    allowed: boolean;
    decision: string;
    summary: string;
    because: Labelled[];
    considered: Labelled[];
    obligations: { type: string; params?: Record<string, unknown> }[];
    subject: { groups: string[]; personas: string[]; workspaces: string[]; attributes: Record<string, unknown> };
}

/** What an access check can ask, in words (any other privilege can be typed). */
export const ACTIONS: { value: string; label: string }[] = [
    { value: 'VIEW_ENTITY_PAGE', label: 'see it in the catalog' },
    { value: 'CAIRN_DATA_ACCESS', label: 'query its data' },
    { value: 'VIEW_DATASET_PROFILE', label: 'see sample values and profiles' },
    { value: 'EDIT_ENTITY_DOCS', label: 'edit its documentation' },
    { value: 'EDIT_ENTITY_TAGS', label: 'edit its tags' },
    { value: 'EDIT_ENTITY_OWNERS', label: 'change its owners' },
    { value: 'CAIRN_APPROVE_ACCESS', label: 'approve access requests to it' },
    { value: 'MANAGE_POLICIES', label: 'administer access (the platform)' },
];

const SPEC = (k: string) => RESOURCE_SPECS[k] as ResourceSpec;
const action = (k: string, label: string) => SPEC(k).actions!.find((a) => a.label === label) as ResourceAction;
const when = (ms?: number | null) => (ms ? new Date(ms).toLocaleString() : '');
const SOURCE_COLOR: Record<string, string> = { scim: 'purple', manual: 'blue', sso: 'default', core: 'default' };

/** Why someone holds a persona, in words. */
export function reasonText(r: Reason): string {
    switch (r.kind) {
        case 'direct':
            return 'named in the persona';
        case 'group':
            return `through the group ${actorName(r.group ?? '')}`;
        case 'attribute_rule':
            return `by attribute: ${(r.rules ?? []).map((x) => `${x.attr} ${x.op} ${JSON.stringify(x.value)}`).join(' and ')}`;
        case 'grant':
            return `granted${r.source && r.source !== 'manual' ? ` (${r.source})` : ' by hand'}${r.expires_at_ms ? `, until ${when(r.expires_at_ms)}` : ''}`;
        default:
            return r.kind;
    }
}

function useSearchParam(name: string): [string, (v: string | null, extra?: Record<string, string | null>) => void] {
    const location = useLocation();
    const history = useHistory();
    const params = new URLSearchParams(location.search);
    const set = (v: string | null, extra: Record<string, string | null> = {}) => {
        const next = new URLSearchParams(location.search);
        for (const [k, val] of Object.entries({ [name]: v, ...extra })) {
            if (val === null || val === '') next.delete(k);
            else next.set(k, val);
        }
        history.replace({ pathname: location.pathname, search: `?${next.toString()}` });
    };
    return [params.get(name) ?? '', set];
}

function SourceTag({ source }: { source: string }) {
    const t = useT();
    const key = `people.source.${source}` as MessageKey;
    return <Tag color={SOURCE_COLOR[source]}>{t(key)}</Tag>;
}

function StatusTag({ status }: { status: string }) {
    const t = useT();
    return status === 'active' ? <Tag color="green">{t('people.status.active')}</Tag> : <Tag color="red">{t('people.status.deactivated')}</Tag>;
}

// ---- People --------------------------------------------------------------------------------------------------------
function PeopleTab({ open, startNew, onNewClosed }: { open: (id: string) => void; startNew: boolean; onNewClosed: () => void }) {
    const t = useT();
    const { cache } = useConsumerApi();
    const [q, setQ] = useState('');
    const [status, setStatus] = useState('all');
    const [source, setSource] = useState('');
    const [type, setType] = useState('all');
    const [page, setPage] = useState(1);
    const [creating, setCreating] = useState(startNew);
    const size = 25;
    const query = new URLSearchParams({ q, status, source, type, offset: String((page - 1) * size), limit: String(size) });
    const list = useDheQuery<Paged<PersonRow>>(`/v1/people/users?${query}`, (c, signal) =>
        c.request('GET', `/v1/people/users?${query}`, undefined, { signal }));
    return (
        <>
            <Space wrap style={{ marginBottom: 12 }}>
                <Input.Search allowClear aria-label={t('people.search')} placeholder={t('people.search')} style={{ width: 260 }}
                    onSearch={(v) => { setQ(v); setPage(1); }} />
                <Select aria-label={t('people.filter.status')} value={status} style={{ width: 150 }}
                    onChange={(v) => { setStatus(v); setPage(1); }}
                    options={[{ value: 'all', label: `${t('people.filter.status')}: ${t('people.filter.any')}` },
                        { value: 'active', label: t('people.status.active') },
                        { value: 'deactivated', label: t('people.status.deactivated') }]} />
                <Select aria-label={t('people.filter.source')} value={source} style={{ width: 170 }}
                    onChange={(v) => { setSource(v); setPage(1); }}
                    options={[{ value: '', label: `${t('people.filter.source')}: ${t('people.filter.any')}` },
                        ...['scim', 'manual', 'sso'].map((s) => ({ value: s, label: t(`people.source.${s}` as MessageKey) }))]} />
                <Select aria-label={t('people.filter.type')} value={type} style={{ width: 170 }}
                    onChange={(v) => { setType(v); setPage(1); }}
                    options={[{ value: 'all', label: `${t('people.filter.type')}: ${t('people.filter.any')}` },
                        { value: 'person', label: t('people.type.person') }, { value: 'service', label: t('people.type.service') }]} />
                <Button type="primary" onClick={() => setCreating(true)}>{t('people.new')}</Button>
            </Space>
            {list.error && <ErrorNotice error={list.error} area={t('people.tab.people')} docs={SPEC('people').docs} />}
            <Table<PersonRow>
                size="small"
                rowKey="id"
                loading={list.loading}
                dataSource={list.data?.items ?? []}
                pagination={{ current: page, pageSize: size, total: list.data?.total ?? 0, onChange: setPage, showSizeChanger: false,
                    hideOnSinglePage: true }}
                locale={{ emptyText: <Empty description="Nobody matches" /> }}
                columns={[
                    { title: 'Name', key: 'name', render: (_: unknown, r: PersonRow) => (
                        <Button type="link" style={{ padding: 0 }} aria-label={t('people.open', { name: r.display_name })}
                            onClick={() => open(r.id)}>{r.display_name}</Button>) },
                    { title: 'User name', dataIndex: 'id' },
                    { title: 'Email', dataIndex: 'email' },
                    { title: 'Department', dataIndex: 'department' },
                    { title: 'Groups', key: 'groups', render: (_: unknown, r: PersonRow) => r.groups.length || '' },
                    { title: 'Source', key: 'source', render: (_: unknown, r: PersonRow) => (
                        <>{r.type === 'service' && <Tag>{t('people.type.service')}</Tag>}<SourceTag source={r.source} /></>) },
                    { title: 'Status', key: 'status', render: (_: unknown, r: PersonRow) => <StatusTag status={r.status} /> },
                ]}
            />
            {creating && (
                <ActionDialog spec={SPEC('people')} action={action('people', 'New person')} row={null}
                    onDone={() => cache.invalidate('/v1/people')} onClose={() => { setCreating(false); onNewClosed(); }} />
            )}
        </>
    );
}

function PersonDrawer({ id, onClose, onCheck }: { id: string; onClose: () => void; onCheck: (id: string) => void }) {
    const t = useT();
    const { client, cache } = useConsumerApi();
    const q = useDheQuery<PersonDetail>(`/v1/people/users/${id}`, (c, signal) =>
        c.request('GET', `/v1/people/users/${encodeURIComponent(id)}`, undefined, { signal }));
    const reach = useDheQuery<Reach>(`/v1/people/users/${id}/access`, (c, signal) =>
        c.request('GET', `/v1/people/users/${encodeURIComponent(id)}/access`, undefined, { signal }));
    const [editing, setEditing] = useState(false);
    const p = q.data;
    const refresh = () => cache.invalidate('/v1/people');
    const setActive = async (active: boolean) => {
        try {
            if (active) await client.request('POST', `/v1/people/users/${encodeURIComponent(id)}/activate`);
            else await client.request('DELETE', `/v1/people/users/${encodeURIComponent(id)}`);
            message.success(active ? 'Activated' : 'Deactivated: their sessions ended');
            refresh();
        } catch (e) {
            notifyError(e, { action: active ? t('people.activate') : t('people.deactivate'), area: t('people.tab.people') });
        }
    };
    return (
        <Drawer open onClose={onClose} width={720} destroyOnClose title={p ? p.display_name : id}>
            {q.error && <ErrorNotice error={q.error} area={t('people.tab.people')} docs={SPEC('people').docs} />}
            {!p ? <Muted>Loading…</Muted> : (
                <div aria-label={`Person ${p.display_name}`} role="region">
                    <Space wrap style={{ marginBottom: 8 }}>
                        <SourceTag source={p.source} />
                        <StatusTag status={p.status} />
                        {p.type === 'service' && <Tag>{t('people.type.service')}</Tag>}
                    </Space>
                    {!p.managed.editable && <Alert type="info" showIcon style={{ marginBottom: 12 }} message={t('people.managedByIdp')} />}
                    <Space wrap style={{ marginBottom: 12 }}>
                        <Button onClick={() => onCheck(p.id)}>{t('people.checkAccess')}</Button>
                        {p.source === 'manual' && <Button onClick={() => setEditing(true)}>{t('people.edit')}</Button>}
                        {p.managed.editable && (p.status === 'active' ? (
                            <Popconfirm title={`Deactivate ${p.display_name}? They are suspended and every session Cairn issued them ends.`}
                                okText={t('people.deactivate')} onConfirm={() => void setActive(false)}>
                                <Button danger>{t('people.deactivate')}</Button>
                            </Popconfirm>
                        ) : <Button onClick={() => void setActive(true)}>{t('people.activate')}</Button>)}
                    </Space>
                    <Descriptions size="small" column={2} bordered>
                        <Descriptions.Item label="User name">{p.id}</Descriptions.Item>
                        <Descriptions.Item label="Email">{p.email}</Descriptions.Item>
                        <Descriptions.Item label="Title">{p.title}</Descriptions.Item>
                        <Descriptions.Item label="Department">{p.department}</Descriptions.Item>
                        <Descriptions.Item label="Country">{p.country}</Descriptions.Item>
                        <Descriptions.Item label="Employment type">{p.employment_type}</Descriptions.Item>
                        <Descriptions.Item label="Manager">{p.manager ? actorName(p.manager) : ''}</Descriptions.Item>
                        <Descriptions.Item label="Last sign-in">
                            {p.last_sign_in ? `${new Date(p.last_sign_in.ts).toLocaleString()} (${p.last_sign_in.action})` : 'none seen by Cairn'}
                        </Descriptions.Item>
                        {Object.entries(p.attributes).map(([k, v]) => (
                            <Descriptions.Item key={k} label={k}>{v}</Descriptions.Item>
                        ))}
                    </Descriptions>

                    <SectionTitle style={{ marginTop: 16 }}>Roles and why</SectionTitle>
                    {p.personas.length === 0 ? <Muted>No persona.</Muted> : (
                        <List size="small" dataSource={p.personas} renderItem={(h) => (
                            <List.Item>
                                <List.Item.Meta title={<>{h.name} <Muted>({h.permissions.join(', ')})</Muted></>}
                                    description={<span>{t('people.why')}: {h.reasons.map(reasonText).join('; ')}</span>} />
                            </List.Item>
                        )} />
                    )}

                    <SectionTitle style={{ marginTop: 16 }}>What they can see</SectionTitle>
                    {reach.error && <ErrorNotice error={reach.error} />}
                    {reach.data && (
                        <>
                            {reach.data.personas.map((r) => (
                                <p key={r.id} style={{ margin: '4px 0' }}><b>{r.name}</b>: {r.scope_text}</p>
                            ))}
                            {reach.data.purposes.filter((x) => !x.allowed).map((x) => (
                                <p key={x.id} style={{ margin: '4px 0' }}>
                                    <Tag color="orange">restricted</Tag>{x.name} ({x.labels.join(', ')}): {x.effect}
                                </p>
                            ))}
                            {reach.data.personas.length === 0 && <Muted>Nothing through a persona.</Muted>}
                        </>
                    )}

                    <SectionTitle style={{ marginTop: 16 }}>Groups</SectionTitle>
                    {p.groups.length === 0 ? <Muted>No group.</Muted> : (
                        <Space wrap>{p.groups.map((g) => <Tag key={g.urn}>{g.name} · {t(`people.source.${g.source}` as MessageKey)}</Tag>)}</Space>
                    )}

                    <SectionTitle style={{ marginTop: 16 }}>Workspaces</SectionTitle>
                    {p.workspaces.length === 0 ? <Muted>No workspace.</Muted> : (
                        <Space wrap>{p.workspaces.map((w) => <Tag key={w.id}>{w.name} · {w.role}</Tag>)}</Space>
                    )}

                    <SectionTitle style={{ marginTop: 16 }}>Agents and service accounts they own</SectionTitle>
                    {p.owns.length === 0 ? <Muted>None.</Muted> : (
                        <List size="small" dataSource={p.owns} renderItem={(o) => (
                            <List.Item>{o.name} <Muted>({o.kind}{o.scopes ? `: ${o.scopes.join(', ')}` : ''}{o.user ? ` → ${actorName(o.user)}` : ''})</Muted></List.Item>
                        )} />
                    )}

                    <SectionTitle style={{ marginTop: 16 }}>Open access reviews</SectionTitle>
                    {p.open_access_reviews.length === 0 ? <Muted>None.</Muted> : (
                        <List size="small" dataSource={p.open_access_reviews} renderItem={(r) => (
                            <List.Item>{r.review_name}: {r.persona} <Muted>(due {when(r.due_ms)})</Muted></List.Item>
                        )} />
                    )}

                    <SectionTitle style={{ marginTop: 16 }}>Recent activity</SectionTitle>
                    <Table size="small" rowKey="seq" pagination={false} dataSource={p.recent_audit}
                        locale={{ emptyText: 'Nothing recorded yet.' }}
                        columns={[
                            { title: 'When', key: 'ts', render: (_: unknown, e: PersonDetail['recent_audit'][number]) => new Date(e.ts).toLocaleString() },
                            { title: 'What', dataIndex: 'action' },
                            { title: 'By', key: 'actor', render: (_: unknown, e: PersonDetail['recent_audit'][number]) => actorName(e.actor) },
                        ]} />
                </div>
            )}
            {editing && p && (
                <ActionDialog spec={SPEC('people')} action={action('people', 'Edit person')} row={p as unknown as Record<string, unknown>}
                    onDone={refresh} onClose={() => setEditing(false)} />
            )}
        </Drawer>
    );
}

// ---- Groups --------------------------------------------------------------------------------------------------------
function GroupsTab({ openPerson }: { openPerson: (id: string) => void }) {
    const t = useT();
    const { cache } = useConsumerApi();
    const [q, setQ] = useState('');
    const [source, setSource] = useState('');
    const [selected, setSelected] = useState<string | null>(null);
    const [creating, setCreating] = useState(false);
    const query = new URLSearchParams({ q, source, limit: '500' });
    const list = useDheQuery<Paged<GroupRow>>(`/v1/people/groups?${query}`, (c, signal) =>
        c.request('GET', `/v1/people/groups?${query}`, undefined, { signal }));
    return (
        <>
            <Space wrap style={{ marginBottom: 12 }}>
                <Input.Search allowClear aria-label={t('people.searchGroups')} placeholder={t('people.searchGroups')} style={{ width: 260 }}
                    onSearch={setQ} />
                <Select aria-label={t('people.filter.source')} value={source} style={{ width: 170 }} onChange={setSource}
                    options={[{ value: '', label: `${t('people.filter.source')}: ${t('people.filter.any')}` },
                        ...['scim', 'manual', 'sso', 'core'].map((s) => ({ value: s, label: t(`people.source.${s}` as MessageKey) }))]} />
                <Button type="primary" onClick={() => setCreating(true)}>{t('people.newGroup')}</Button>
            </Space>
            {list.error && <ErrorNotice error={list.error} area={t('people.tab.groups')} docs={SPEC('people-groups').docs} />}
            <Table<GroupRow> size="small" rowKey="id" loading={list.loading} dataSource={list.data?.items ?? []}
                pagination={{ pageSize: 25, hideOnSinglePage: true }}
                columns={[
                    { title: 'Name', key: 'name', render: (_: unknown, g: GroupRow) => (
                        <Button type="link" style={{ padding: 0 }} aria-label={t('people.open', { name: g.name })}
                            onClick={() => setSelected(g.id)}>{g.name}</Button>) },
                    { title: 'Id', dataIndex: 'id' },
                    { title: 'Members', dataIndex: 'members' },
                    { title: 'Source', key: 'source', render: (_: unknown, g: GroupRow) => <SourceTag source={g.source} /> },
                ]} />
            {creating && (
                <ActionDialog spec={SPEC('people-groups')} action={action('people-groups', 'New group')} row={null}
                    onDone={() => cache.invalidate('/v1/people')} onClose={() => setCreating(false)} />
            )}
            {selected && <GroupDrawer id={selected} onClose={() => setSelected(null)} openPerson={openPerson} />}
        </>
    );
}

function GroupDrawer({ id, onClose, openPerson }: { id: string; onClose: () => void; openPerson: (id: string) => void }) {
    const t = useT();
    const { client, cache } = useConsumerApi();
    const q = useDheQuery<GroupDetail>(`/v1/people/groups/${id}`, (c, signal) =>
        c.request('GET', `/v1/people/groups/${encodeURIComponent(id)}`, undefined, { signal }));
    const [dialog, setDialog] = useState<'edit' | 'member' | null>(null);
    const g = q.data;
    const refresh = () => cache.invalidate('/v1/people');
    const remove = async (person: string) => {
        try {
            await client.request('DELETE', `/v1/people/groups/${encodeURIComponent(id)}/members/${encodeURIComponent(person)}`);
            refresh();
        } catch (e) {
            notifyError(e, { action: 'Remove member', area: t('people.tab.groups') });
        }
    };
    const drop = async () => {
        try {
            await client.request('DELETE', `/v1/people/groups/${encodeURIComponent(id)}`);
            refresh();
            onClose();
        } catch (e) {
            notifyError(e, { action: 'Delete group', area: t('people.tab.groups') });
        }
    };
    return (
        <Drawer open onClose={onClose} width={600} destroyOnClose title={g ? g.name : id}>
            {q.error && <ErrorNotice error={q.error} area={t('people.tab.groups')} />}
            {g && (
                <>
                    <Space wrap style={{ marginBottom: 8 }}><SourceTag source={g.source} /><Muted>{g.id}</Muted></Space>
                    {g.description && <p>{g.description}</p>}
                    {g.managed.why && <Alert type="info" showIcon style={{ marginBottom: 12 }} message={g.managed.why} />}
                    {g.managed.editable && (
                        <Space wrap style={{ marginBottom: 12 }}>
                            <Button onClick={() => setDialog('member')}>Add member</Button>
                            <Button onClick={() => setDialog('edit')}>Edit group</Button>
                            <Popconfirm title={`Delete ${g.name}? Its members leave it.`} onConfirm={() => void drop()}>
                                <Button danger>Delete group</Button>
                            </Popconfirm>
                        </Space>
                    )}
                    <SectionTitle>Members</SectionTitle>
                    <List size="small" dataSource={g.member_list} locale={{ emptyText: 'No members.' }} renderItem={(m) => (
                        <List.Item actions={g.managed.editable ? [
                            <Popconfirm key="rm" title={`Remove ${m.display_name} from ${g.name}?`} onConfirm={() => void remove(m.id)}>
                                <Button size="small" type="link" aria-label={`Remove ${m.display_name}`}>Remove</Button>
                            </Popconfirm>] : []}>
                            <Button type="link" style={{ padding: 0 }} onClick={() => openPerson(m.id)}>{m.display_name}</Button>
                            <Muted style={{ marginLeft: 8 }}>{m.id}</Muted>
                        </List.Item>
                    )} />
                    <SectionTitle style={{ marginTop: 16 }}>Roles it gives</SectionTitle>
                    {g.personas.length === 0 ? <Muted>No persona names this group.</Muted>
                        : <Space wrap>{g.personas.map((x) => <Tag key={x.id}>{x.name}</Tag>)}</Space>}
                    {g.purposes.length > 0 && (
                        <p style={{ marginTop: 8 }}>Allowed by purposes: {g.purposes.map((x) => x.name).join(', ')}</p>
                    )}
                    {g.workspaces.length > 0 && (
                        <p style={{ marginTop: 8 }}>Workspaces: {g.workspaces.map((w) => `${w.name} (${w.role})`).join(', ')}</p>
                    )}
                </>
            )}
            {dialog === 'edit' && g && (
                <ActionDialog spec={SPEC('people-groups')} action={action('people-groups', 'Edit group')}
                    row={g as unknown as Record<string, unknown>} onDone={refresh} onClose={() => setDialog(null)} />
            )}
            {dialog === 'member' && g && (
                <ActionDialog spec={SPEC('people-groups')} action={action('people-groups', 'Add member')}
                    row={{ ...g, group: g.id } as unknown as Record<string, unknown>} onDone={refresh} onClose={() => setDialog(null)} />
            )}
        </Drawer>
    );
}

// ---- Roles ---------------------------------------------------------------------------------------------------------
interface PersonaSummary {
    id: string; name: string; description: string; enabled: boolean; permissions: string[];
    members: { users: string[]; groups: string[]; attribute_rules: { attr: string; op: string; value: unknown }[] };
}

function RolesTab() {
    const t = useT();
    const q = useDheQuery<PersonaSummary[]>('/governance/personas', (c, signal) => c.request('GET', '/governance/personas', undefined, { signal }));
    return (
        <>
            <p><Muted>{t('people.rolesIntro')}</Muted></p>
            <p><Link to="/governance">{t('people.openStudio')}</Link></p>
            {q.error && <ErrorNotice error={q.error} />}
            <Table<PersonaSummary> size="small" rowKey="id" loading={q.loading} dataSource={q.data ?? []} pagination={false}
                columns={[
                    { title: 'Role', dataIndex: 'name' },
                    { title: 'May', key: 'perm', render: (_: unknown, p: PersonaSummary) => p.permissions.join(', ') },
                    { title: 'Who', key: 'who', render: (_: unknown, p: PersonaSummary) => [
                        p.members.users.length ? `${p.members.users.length} people` : '',
                        ...p.members.groups.map((g) => `group ${actorName(g)}`),
                        p.members.attribute_rules.length ? `${p.members.attribute_rules.length} attribute rule(s)` : '',
                    ].filter(Boolean).join(', ') || '—' },
                    { title: 'On', key: 'on', render: (_: unknown, p: PersonaSummary) => (p.enabled ? '' : <Tag>disabled</Tag>) },
                ]} />
        </>
    );
}

// ---- Service accounts & agents ---------------------------------------------------------------------------------------
function PrincipalsTab() {
    const [type, setType] = useState('all');
    const q = useDheQuery<Principal[]>(`/v1/people/principals?type=${type}`, (c, signal) =>
        c.request('GET', `/v1/people/principals?type=${type}`, undefined, { signal }));
    return (
        <>
            <Select aria-label="Type" value={type} onChange={setType} style={{ width: 220, marginBottom: 12 }}
                options={[{ value: 'all', label: 'Everything' }, { value: 'agent', label: 'Agent identities' },
                    { value: 'workload', label: 'Workload identities' }, { value: 'service', label: 'Service accounts' }]} />
            {q.error && <ErrorNotice error={q.error} area="Service accounts & agents" docs={SPEC('people-principals').docs} />}
            <Table<Principal> size="small" rowKey={(r) => `${r.type}:${r.id}`} loading={q.loading} dataSource={q.data ?? []}
                pagination={{ pageSize: 25, hideOnSinglePage: true }}
                columns={[
                    { title: 'Name', dataIndex: 'name' },
                    { title: 'Type', key: 'type', render: (_: unknown, r: Principal) => (
                        <Tag>{r.type === 'agent' ? `agent (${r.agent_kind})` : r.type === 'workload' ? 'workload identity' : 'service account'}</Tag>) },
                    { title: 'Owner / acts as', key: 'owner', render: (_: unknown, r: Principal) => (r.owner ? actorName(r.owner) : r.purpose ?? '') },
                    { title: 'Scopes', key: 'scopes', render: (_: unknown, r: Principal) => r.scopes.join(', ') },
                    { title: 'Enabled', key: 'enabled', render: (_: unknown, r: Principal) => (r.enabled ? 'yes' : 'no') },
                    { title: 'Last used', key: 'used', render: (_: unknown, r: Principal) => when(r.last_used_ms) },
                ]} />
        </>
    );
}

// ---- Access check --------------------------------------------------------------------------------------------------
function PersonSelect({ value, onChange }: { value: string; onChange: (v: string) => void }) {
    const t = useT();
    const [q, setQ] = useState('');
    const list = useDheQuery<Paged<PersonRow>>(`/v1/people/users?q=${q}&limit=20&type=all`, (c, signal) =>
        c.request('GET', `/v1/people/users?${new URLSearchParams({ q, limit: '20' })}`, undefined, { signal }));
    const options = (list.data?.items ?? []).map((p) => ({ value: p.id, label: `${p.display_name} (${p.id})` }));
    if (value && !options.some((o) => o.value === value)) options.unshift({ value, label: value });
    return (
        <Select showSearch aria-label={t('people.access.person')} value={value || undefined} placeholder="Search people"
            filterOption={false} onSearch={setQ} onChange={onChange} options={options} style={{ width: 280 }}
            notFoundContent={list.loading ? 'Searching…' : 'Nobody matches'} />
    );
}

function AccessCheckTab({ person: initialPerson }: { person: string }) {
    const t = useT();
    const { client } = useConsumerApi();
    const [person, setPerson] = useState(initialPerson);
    const [privilege, setPrivilege] = useState('VIEW_ENTITY_PAGE');
    const [typed, setTyped] = useState('');
    const [resource, setResource] = useState('');
    const [busy, setBusy] = useState(false);
    const [answer, setAnswer] = useState<CheckAnswer | null>(null);
    const [error, setError] = useState<unknown>(null);
    const options = useMemo(() => {
        const out = ACTIONS.map((a) => ({ value: a.value, label: a.label }));
        const custom = typed.trim().toUpperCase();
        if (/^[A-Z][A-Z0-9_]{1,63}$/.test(custom) && !out.some((o) => o.value === custom)) out.push({ value: custom, label: custom });
        if (!out.some((o) => o.value === privilege)) out.push({ value: privilege, label: privilege });
        return out;
    }, [typed, privilege]);
    const run = async () => {
        setBusy(true);
        setError(null);
        try {
            setAnswer(await client.request<CheckAnswer>('POST', '/v1/people/access-check',
                { person, privilege, resource: resource.trim() || null }));
        } catch (e) {
            setAnswer(null);
            setError(e);
        } finally {
            setBusy(false);
        }
    };
    return (
        <>
            <Surface style={{ marginBottom: 12 }}>
                <Space wrap align="end">
                    <label>
                        <div>{t('people.access.person')}</div>
                        <PersonSelect value={person} onChange={setPerson} />
                    </label>
                    <label>
                        <div>{t('people.access.action')}</div>
                        <Select showSearch aria-label={t('people.access.action')} value={privilege} onChange={setPrivilege}
                            onSearch={setTyped} options={options} style={{ width: 280 }} optionFilterProp="label" />
                    </label>
                    <label>
                        <div>{t('people.access.resource')}</div>
                        <Input aria-label={t('people.access.resource')} value={resource} onChange={(e) => setResource(e.target.value)}
                            placeholder="urn:li:dataset:(…)" style={{ width: 360 }} />
                    </label>
                    <Button type="primary" loading={busy} disabled={!person} onClick={() => void run()}>{t('people.access.run')}</Button>
                </Space>
            </Surface>
            {error !== null && <ErrorNotice error={error} action={t('people.access.run')} area={t('people.tab.access')} docs={SPEC('people').docs} />}
            {answer && <CheckResult answer={answer} />}
        </>
    );
}

export function CheckResult({ answer }: { answer: CheckAnswer }) {
    const t = useT();
    return (
        <Surface role="status" aria-live="polite" aria-label="Access check answer">
            <Space align="baseline">
                <Tag color={answer.allowed ? 'green' : 'red'} style={{ fontSize: 16, padding: '2px 10px' }}>
                    {answer.allowed ? t('people.access.yes') : t('people.access.no')}
                </Tag>
                <span>{answer.summary}</span>
            </Space>
            {answer.because.length > 0 && (
                <>
                    <SectionTitle style={{ marginTop: 12 }}>{t('people.why')}</SectionTitle>
                    <List size="small" dataSource={answer.because} renderItem={(b) => (
                        <List.Item>
                            <List.Item.Meta title={`${b.kind} ${b.name}`} description={<><code>{b.policy_id}</code> {b.description}</>} />
                        </List.Item>
                    )} />
                </>
            )}
            {answer.considered.length > 0 && (
                <>
                    <SectionTitle style={{ marginTop: 12 }}>Also considered, did not apply</SectionTitle>
                    <List size="small" dataSource={answer.considered} renderItem={(b) => (
                        <List.Item><span><b>{b.kind} {b.name}</b>: <Muted>{b.why_not}</Muted></span></List.Item>
                    )} />
                </>
            )}
            {answer.obligations.length > 0 && (
                <p style={{ marginTop: 8 }}>Obligations: {answer.obligations.map((o) => o.type).join(', ')}</p>
            )}
            <p style={{ marginTop: 8, color: tokens.color.textMuted }}>
                Decided with groups {answer.subject.groups.map(actorName).join(', ') || 'none'}; personas{' '}
                {answer.subject.personas.join(', ') || 'none'}.
            </p>
        </Surface>
    );
}

// ---- the page ------------------------------------------------------------------------------------------------------
const TABS = ['people', 'groups', 'roles', 'principals', 'access'] as const;

/** People & roles: the directory, a person's page (what they hold and why), groups, roles, service accounts and agents,
 * and the access check. For administrators (MANAGE_POLICIES); the API decides every request. */
export function PeoplePage() {
    const t = useT();
    const [tab, setTab] = useSearchParam('tab');
    const [person, setPerson] = useSearchParam('person');
    const [checkFor] = useSearchParam('check');
    const [isNew, setNew] = useSearchParam('new');
    const active = (TABS as readonly string[]).includes(tab) ? tab : 'people';
    const probe = useDheQuery<Paged<PersonRow>>('/v1/people/users?probe', (c, signal) =>
        c.request('GET', '/v1/people/users?limit=1', undefined, { signal }));
    if (probe.error && errorStatus(probe.error) === 403) {
        return <Page><PageHeader title={t('people.title')} icon="user" help={pageHelp('/people')} /><Alert type="warning" showIcon message={t('people.forbidden')} /></Page>;
    }
    const open = (id: string) => setPerson(id);
    const check = (id: string) => setTab('access', { person: null, check: id });
    return (
        <Page>
            <PageHeader title={t('people.title')} description={t('people.description')} icon="user" help={pageHelp('/people')} />
            <Tabs
                activeKey={active}
                onChange={(k) => setTab(k, { check: null })}
                items={[
                    { key: 'people', label: t('people.tab.people'), children: active === 'people' ? <PeopleTab open={open} startNew={isNew === '1'} onNewClosed={() => setNew(null)} /> : null },
                    { key: 'groups', label: t('people.tab.groups'), children: active === 'groups' ? <GroupsTab openPerson={open} /> : null },
                    { key: 'roles', label: t('people.tab.roles'), children: active === 'roles' ? <RolesTab /> : null },
                    { key: 'principals', label: t('people.tab.principals'), children: active === 'principals' ? <PrincipalsTab /> : null },
                    { key: 'access', label: t('people.tab.access'),
                        children: active === 'access' ? <AccessCheckTab key={checkFor} person={checkFor} /> : null },
                ]}
            />
            {person && <PersonDrawer id={person} onClose={() => setPerson(null)} onCheck={check} />}
        </Page>
    );
}
