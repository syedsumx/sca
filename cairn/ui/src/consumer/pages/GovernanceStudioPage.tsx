import { Alert, Button, Checkbox, Empty, Input, List, Popconfirm, Radio, Select, Space, Switch, Tabs, Tag, message } from 'antd';
import { FormsAdmin } from '../components/Forms';
import { TagSyncAdmin } from '../components/TagSync';
import React, { useEffect, useState } from 'react';
import styled from 'styled-components';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import type {
    AttributeRule,
    CompiledPolicy,
    MaskStrategy,
    PermissionBundles,
    Persona,
    PersonaInput,
    PersonaPreview,
    Purpose,
    PurposeInput,
} from '../api/types';
import { Muted, Page, SectionTitle, Surface, tokens } from '../components/tokens';
import { actorName } from '../utils/format';

const Split = styled.div`
    display: grid;
    grid-template-columns: 300px 1fr;
    gap: 16px;
    @media (max-width: 900px) {
        grid-template-columns: 1fr;
    }
`;

const Field = styled.div`
    margin-bottom: 14px;
    label {
        display: block;
        font-weight: 600;
        font-size: 13px;
        margin-bottom: 4px;
    }
    .help {
        font-size: 12px;
        color: ${tokens.color.textMuted};
        margin-top: 2px;
    }
`;

/** Business-friendly names for permission bundles (backend keys → copy). */
export const BUNDLE_COPY: Record<string, string> = {
    view: 'View & search',
    comment: 'Comment & discuss',
    request_access: 'Request access',
    edit_docs: 'Edit documentation',
    edit_tags: 'Edit tags',
    edit_terms: 'Edit business terms',
    edit_owners: 'Edit owners',
    certify: 'Certify assets',
    manage_quality: 'Manage data quality',
    view_profile: 'View profiles & statistics',
    view_samples: 'View sample values',
    view_queries: 'View queries',
};

const ATTRS = [
    { value: 'subject.attributes.department', label: 'Department' },
    { value: 'subject.attributes.title', label: 'Job title' },
    { value: 'subject.attributes.country', label: 'Country' },
    { value: 'subject.attributes.employment_type', label: 'Employment type' },
];

export const EMPTY_PERSONA: PersonaInput = {
    name: '',
    description: '',
    color: '#533fd1',
    members: { users: [], groups: [], attribute_rules: [] },
    scope: { domains: [], data_products: [], platforms: [], tags: [], glossary_terms: [], asset_urns: [] },
    permissions: ['view', 'comment', 'request_access'],
    enabled: true,
};

export const EMPTY_PURPOSE: PurposeInput = {
    name: '',
    description: '',
    tags: [],
    allowed: { users: [], groups: [], personas: [] },
    deny: ['view_samples', 'view_profile'],
    mask: { enabled: true, strategy: 'partial' },
    enabled: true,
};

/** Plain-English one-liner for a persona (shown in the list and the preview). */
export function describePersona(p: PersonaInput): string {
    const who = [
        ...p.members.groups.map((g) => `group ${actorName(g)}`),
        ...p.members.users.map((u) => actorName(u)),
        ...p.members.attribute_rules.map((r) => `${r.attr.split('.').pop()} = ${String(r.value)}`),
    ];
    const where = [
        ...p.scope.domains.map((d) => `domain ${d.split(':').pop()}`),
        ...p.scope.data_products.map((d) => `product ${d.split(':').pop()}`),
        ...p.scope.tags.map((t) => `tag ${t.split(':').pop()}`),
        ...p.scope.platforms.map((t) => t.split(':').pop() as string),
    ];
    const can = p.permissions.map((b) => (BUNDLE_COPY[b] ?? b).toLowerCase());
    return `${who.length ? who.join(', ') : 'Nobody yet'} can ${can.join(', ') || 'do nothing'} on ${
        where.length ? where.join(', ') : 'all assets'
    }.`;
}

function TagsSelect({ value, onChange, placeholder, ariaLabel }: { value: string[]; onChange: (v: string[]) => void; placeholder: string; ariaLabel: string }) {
    return (
        <Select
            mode="tags"
            aria-label={ariaLabel}
            style={{ width: '100%' }}
            placeholder={placeholder}
            value={value}
            onChange={onChange}
            tokenSeparators={[',']}
        />
    );
}

function GeneratedPolicies({ prefix }: { prefix: string }) {
    const compiled = useDheQuery<{ policies: CompiledPolicy[]; compiled?: CompiledPolicy[] }>(
        '/governance/policies?include_compiled=true',
        (c, signal) => c.compiledPolicies({ signal }),
    );
    const policies = (compiled.data?.compiled ?? []).filter((p) => p.id.startsWith(prefix) || p.source?.startsWith(prefix));
    if (compiled.error) return <Alert type="warning" message={`Generated policies unavailable: ${compiled.error.message}`} />;
    return (
        <pre style={{ background: tokens.color.surfaceAlt, padding: 12, borderRadius: 8, maxHeight: 320, overflow: 'auto', fontSize: 12 }}>
            {policies.length ? JSON.stringify(policies, null, 2) : 'Save to see the generated ABAC policies.'}
        </pre>
    );
}

function PersonaEditor({ persona, bundles, onSaved, onDeleted }: { persona: PersonaInput; bundles: PermissionBundles; onSaved: (p: Persona) => void; onDeleted: () => void }) {
    const { client } = useConsumerApi();
    const [draft, setDraft] = useState<PersonaInput>(persona);
    const [preview, setPreview] = useState<PersonaPreview | null>(null);
    const [busy, setBusy] = useState(false);
    const [advanced, setAdvanced] = useState(false);
    useEffect(() => {
        setDraft(persona);
        setPreview(null);
    }, [persona]);

    const set = <K extends keyof PersonaInput>(k: K, v: PersonaInput[K]) => setDraft((d) => ({ ...d, [k]: v }));
    const setRule = (i: number, r: AttributeRule | null) =>
        set('members', {
            ...draft.members,
            attribute_rules: r
                ? draft.members.attribute_rules.map((x, j) => (j === i ? r : x))
                : draft.members.attribute_rules.filter((_, j) => j !== i),
        });

    const save = async () => {
        if (!draft.name.trim()) {
            message.warning('Give the persona a name');
            return;
        }
        setBusy(true);
        try {
            const saved = await client.savePersona(draft);
            message.success('Persona saved — policies recompiled');
            onSaved(saved);
            setDraft(saved);
            setPreview(await client.previewPersona(saved.id).catch(() => null));
        } catch (e) {
            message.error((e as Error).message);
        } finally {
            setBusy(false);
        }
    };

    return (
        <Surface style={{ padding: 18 }}>
            <Field>
                <label htmlFor="persona-name">Name</label>
                <Input id="persona-name" value={draft.name} onChange={(e) => set('name', e.target.value)} placeholder="e.g. Finance Analysts" />
            </Field>
            <Field>
                <label htmlFor="persona-desc">Description</label>
                <Input.TextArea id="persona-desc" autoSize value={draft.description} onChange={(e) => set('description', e.target.value)} />
            </Field>
            <SectionTitle>Who</SectionTitle>
            <Field>
                <label>Groups</label>
                <TagsSelect ariaLabel="Groups" value={draft.members.groups} onChange={(v) => set('members', { ...draft.members, groups: v })} placeholder="urn:li:corpGroup:finance" />
            </Field>
            <Field>
                <label>People</label>
                <TagsSelect ariaLabel="People" value={draft.members.users} onChange={(v) => set('members', { ...draft.members, users: v })} placeholder="urn:li:corpuser:ana" />
            </Field>
            <Field>
                <label>Attribute rules</label>
                {draft.members.attribute_rules.map((r, i) => (
                    // eslint-disable-next-line react/no-array-index-key
                    <Space key={i} style={{ marginBottom: 6 }}>
                        <Select aria-label="Attribute" style={{ width: 170 }} value={r.attr} options={ATTRS} onChange={(v) => setRule(i, { ...r, attr: v })} />
                        <span>is</span>
                        <Input aria-label="Attribute value" value={String(r.value ?? '')} onChange={(e) => setRule(i, { ...r, op: 'eq', value: e.target.value })} />
                        <Button size="small" onClick={() => setRule(i, null)} aria-label="Remove rule">
                            ✕
                        </Button>
                    </Space>
                ))}
                <div>
                    <Button
                        size="small"
                        onClick={() =>
                            set('members', {
                                ...draft.members,
                                attribute_rules: [...draft.members.attribute_rules, { attr: ATTRS[0].value, op: 'eq', value: '' }],
                            })
                        }
                    >
                        + Add rule
                    </Button>
                </div>
                <div className="help">Anyone matching a rule is a member, e.g. Department is Finance.</div>
            </Field>
            <SectionTitle>Where</SectionTitle>
            {(
                [
                    ['domains', 'Domains', 'urn:li:domain:finance'],
                    ['data_products', 'Data products', 'urn:li:dataProduct:customer360'],
                    ['platforms', 'Platforms', 'urn:li:dataPlatform:snowflake'],
                    ['tags', 'Tags', 'urn:li:tag:Gold'],
                    ['glossary_terms', 'Business terms', 'urn:li:glossaryTerm:Revenue'],
                ] as [keyof PersonaInput['scope'], string, string][]
            ).map(([k, label, ph]) => (
                <Field key={k}>
                    <label>{label}</label>
                    <TagsSelect ariaLabel={label} value={draft.scope[k]} onChange={(v) => set('scope', { ...draft.scope, [k]: v })} placeholder={ph} />
                </Field>
            ))}
            <SectionTitle>Can</SectionTitle>
            <Checkbox.Group
                aria-label="Permissions"
                value={draft.permissions}
                onChange={(v) => set('permissions', v as string[])}
                style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))', gap: 6 }}
            >
                {Object.keys(bundles).map((b) => (
                    <Checkbox key={b} value={b}>
                        <span title={bundles[b].join(', ')}>{BUNDLE_COPY[b] ?? b}</span>
                    </Checkbox>
                ))}
            </Checkbox.Group>
            <Alert style={{ margin: '16px 0' }} type="info" showIcon message={describePersona(draft)} />
            {preview && (
                <Alert
                    type="success"
                    style={{ marginBottom: 12 }}
                    message={`${preview.members_count} people · ${preview.assets_in_scope.toLocaleString()} assets in scope`}
                    description={`e.g. ${preview.sample_members.map(actorName).join(', ') || '—'} · ${preview.sample_assets.map((a) => a.name).join(', ') || '—'}`}
                />
            )}
            <Space>
                <Switch checked={draft.enabled} onChange={(v) => set('enabled', v)} aria-label="Enabled" /> Enabled
                <Button type="primary" loading={busy} onClick={() => void save()}>
                    Save persona
                </Button>
                {draft.id && (
                    <>
                        <Button onClick={() => client.previewPersona(draft.id as string).then(setPreview, (e: Error) => message.error(e.message))}>
                            Preview members & assets
                        </Button>
                        <Popconfirm
                            title="Delete this persona? Members lose its permissions immediately."
                            onConfirm={() => client.deletePersona(draft.id as string).then(onDeleted, (e: Error) => message.error(e.message))}
                        >
                            <Button danger>Delete</Button>
                        </Popconfirm>
                    </>
                )}
                <Button type="link" onClick={() => setAdvanced(!advanced)}>
                    {advanced ? 'Hide' : 'Advanced: view'} generated policy
                </Button>
            </Space>
            {advanced && draft.id && <GeneratedPolicies prefix={`persona:${draft.id}`} />}
        </Surface>
    );
}

function PurposeEditor({ purpose, bundles, personas, onSaved, onDeleted }: { purpose: PurposeInput; bundles: PermissionBundles; personas: Persona[]; onSaved: (p: Purpose) => void; onDeleted: () => void }) {
    const { client } = useConsumerApi();
    const [draft, setDraft] = useState<PurposeInput>(purpose);
    const [busy, setBusy] = useState(false);
    const [advanced, setAdvanced] = useState(false);
    useEffect(() => setDraft(purpose), [purpose]);
    const set = <K extends keyof PurposeInput>(k: K, v: PurposeInput[K]) => setDraft((d) => ({ ...d, [k]: v }));

    const save = async () => {
        if (!draft.name.trim() || !draft.tags.length) {
            message.warning('A purpose needs a name and at least one tag or classification');
            return;
        }
        setBusy(true);
        try {
            const saved = await client.savePurpose(draft);
            message.success('Purpose saved — deny rules take effect immediately');
            onSaved(saved);
            setDraft(saved);
        } catch (e) {
            message.error((e as Error).message);
        } finally {
            setBusy(false);
        }
    };

    return (
        <Surface style={{ padding: 18 }}>
            <Field>
                <label htmlFor="purpose-name">Name</label>
                <Input id="purpose-name" value={draft.name} onChange={(e) => set('name', e.target.value)} placeholder="e.g. GDPR Personal Data" />
            </Field>
            <Field>
                <label htmlFor="purpose-desc">Description</label>
                <Input.TextArea id="purpose-desc" autoSize value={draft.description} onChange={(e) => set('description', e.target.value)} />
            </Field>
            <Field>
                <label>Applies to data tagged</label>
                <TagsSelect ariaLabel="Tags or classifications" value={draft.tags} onChange={(v) => set('tags', v)} placeholder="PII, PII.*, urn:li:tag:PCI" />
                <div className="help">Tags, glossary terms or classification labels. Use “.*” for a whole family.</div>
            </Field>
            <Field>
                <label>Allowed for personas</label>
                <Select
                    mode="multiple"
                    aria-label="Allowed personas"
                    style={{ width: '100%' }}
                    value={draft.allowed.personas}
                    onChange={(v: string[]) => set('allowed', { ...draft.allowed, personas: v })}
                    options={personas.map((p) => ({ value: p.id, label: p.name }))}
                />
            </Field>
            <Field>
                <label>Allowed for groups</label>
                <TagsSelect ariaLabel="Allowed groups" value={draft.allowed.groups} onChange={(v) => set('allowed', { ...draft.allowed, groups: v })} placeholder="urn:li:corpGroup:dpo" />
            </Field>
            <Field>
                <label>Allowed for people</label>
                <TagsSelect ariaLabel="Allowed people" value={draft.allowed.users} onChange={(v) => set('allowed', { ...draft.allowed, users: v })} placeholder="urn:li:corpuser:chen" />
            </Field>
            <SectionTitle>Everyone else</SectionTitle>
            <Field>
                <label>⛔ Deny</label>
                <Checkbox.Group
                    aria-label="Denied permissions"
                    value={draft.deny}
                    onChange={(v) => set('deny', v as string[])}
                    style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))', gap: 6 }}
                >
                    {Object.keys(bundles).map((b) => (
                        <Checkbox key={b} value={b}>
                            {BUNDLE_COPY[b] ?? b}
                        </Checkbox>
                    ))}
                </Checkbox.Group>
            </Field>
            <Field>
                <label>🎭 Mask sample values</label>
                <Space>
                    <Switch checked={draft.mask.enabled} onChange={(v) => set('mask', { ...draft.mask, enabled: v })} aria-label="Masking enabled" />
                    <Radio.Group
                        aria-label="Masking strategy"
                        disabled={!draft.mask.enabled}
                        value={draft.mask.strategy}
                        onChange={(e) => set('mask', { ...draft.mask, strategy: e.target.value as MaskStrategy })}
                        optionType="button"
                        options={[
                            { label: 'Partial (j•••@•••.com)', value: 'partial' },
                            { label: 'Redact', value: 'redact' },
                            { label: 'Hash', value: 'hash' },
                            { label: 'Null', value: 'null' },
                        ]}
                    />
                </Space>
            </Field>
            <Alert
                type="warning"
                showIcon
                style={{ marginBottom: 12 }}
                message="Deny always wins"
                description="Purpose rules override every persona or role grant for people not listed above."
            />
            <Space>
                <Switch checked={draft.enabled} onChange={(v) => set('enabled', v)} aria-label="Enabled" /> Enabled
                <Button type="primary" loading={busy} onClick={() => void save()}>
                    Save purpose
                </Button>
                {draft.id && (
                    <Popconfirm title="Delete this purpose?" onConfirm={() => client.deletePurpose(draft.id as string).then(onDeleted, (e: Error) => message.error(e.message))}>
                        <Button danger>Delete</Button>
                    </Popconfirm>
                )}
                <Button type="link" onClick={() => setAdvanced(!advanced)}>
                    {advanced ? 'Hide' : 'Advanced: view'} generated policy
                </Button>
            </Space>
            {advanced && draft.id && <GeneratedPolicies prefix={`purpose:${draft.id}`} />}
        </Surface>
    );
}

export function GovernanceStudioPage() {
    const { cache } = useConsumerApi();
    const personas = useDheQuery<Persona[]>('/governance/personas', (c, signal) => c.listPersonas({ signal }));
    const purposes = useDheQuery<Purpose[]>('/governance/purposes', (c, signal) => c.listPurposes({ signal }));
    const bundles = useDheQuery<PermissionBundles>('/governance/personas/permission-bundles', (c, signal) => c.permissionBundles({ signal }));
    const [personaSel, setPersonaSel] = useState<PersonaInput | null>(null);
    const [purposeSel, setPurposeSel] = useState<PurposeInput | null>(null);
    const refresh = () => {
        cache.invalidate('/governance');
    };

    const forbidden = [personas.error, purposes.error].find((e) => e && /403|forbidden|denied/i.test(e.message));
    if (forbidden) return <Page><Alert type="warning" message="You need governance permissions (MANAGE_POLICIES) to edit personas and purposes." /></Page>;

    return (
        <Page>
            <SectionTitle as="h1" style={{ fontSize: 24 }}>
                Persona & Purpose Studio
            </SectionTitle>
            <Muted>Personas grant what a role can do; purposes protect sensitive data. Both compile to access policies automatically.</Muted>
            <Tabs
                style={{ marginTop: 12 }}
                items={[
                    {
                        key: 'personas',
                        label: 'Personas',
                        children: (
                            <Split>
                                <Surface style={{ padding: 8 }}>
                                    <List
                                        loading={personas.loading}
                                        dataSource={personas.data ?? []}
                                        locale={{ emptyText: <Empty description="No personas yet" /> }}
                                        renderItem={(p) => (
                                            <List.Item
                                                style={{ cursor: 'pointer', padding: '8px 10px', background: personaSel?.id === p.id ? tokens.color.primarySoft : undefined }}
                                                onClick={() => setPersonaSel(p)}
                                            >
                                                <List.Item.Meta
                                                    avatar={<span style={{ display: 'inline-block', width: 10, height: 10, borderRadius: 5, background: p.color, marginTop: 6 }} />}
                                                    title={p.name}
                                                    description={p.enabled ? describePersona(p) : <Tag>disabled</Tag>}
                                                />
                                            </List.Item>
                                        )}
                                    />
                                    <Button block type="dashed" onClick={() => setPersonaSel({ ...EMPTY_PERSONA })}>
                                        + New persona
                                    </Button>
                                </Surface>
                                {personaSel ? (
                                    <PersonaEditor
                                        persona={personaSel}
                                        bundles={bundles.data ?? {}}
                                        onSaved={(p) => {
                                            setPersonaSel(p);
                                            refresh();
                                        }}
                                        onDeleted={() => {
                                            setPersonaSel(null);
                                            refresh();
                                        }}
                                    />
                                ) : (
                                    <Empty description="Select a persona or create one" />
                                )}
                            </Split>
                        ),
                    },
                    { key: 'forms', label: 'Compliance forms', children: <FormsAdmin /> },
                    { key: 'tag-sync', label: 'Warehouse tags', children: <TagSyncAdmin /> },
                    {
                        key: 'purposes',
                        label: 'Purposes',
                        children: (
                            <Split>
                                <Surface style={{ padding: 8 }}>
                                    <List
                                        loading={purposes.loading}
                                        dataSource={purposes.data ?? []}
                                        locale={{ emptyText: <Empty description="No purposes yet" /> }}
                                        renderItem={(p) => (
                                            <List.Item
                                                style={{ cursor: 'pointer', padding: '8px 10px', background: purposeSel?.id === p.id ? tokens.color.primarySoft : undefined }}
                                                onClick={() => setPurposeSel(p)}
                                            >
                                                <List.Item.Meta title={`🔒 ${p.name}`} description={`tags: ${p.tags.join(', ')}`} />
                                            </List.Item>
                                        )}
                                    />
                                    <Button block type="dashed" onClick={() => setPurposeSel({ ...EMPTY_PURPOSE })}>
                                        + New purpose
                                    </Button>
                                </Surface>
                                {purposeSel ? (
                                    <PurposeEditor
                                        purpose={purposeSel}
                                        bundles={bundles.data ?? {}}
                                        personas={personas.data ?? []}
                                        onSaved={(p) => {
                                            setPurposeSel(p);
                                            refresh();
                                        }}
                                        onDeleted={() => {
                                            setPurposeSel(null);
                                            refresh();
                                        }}
                                    />
                                ) : (
                                    <Empty description="Select a purpose or create one" />
                                )}
                            </Split>
                        ),
                    },
                ]}
            />
        </Page>
    );
}
