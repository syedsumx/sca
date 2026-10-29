import {
    Alert,
    Button,
    Drawer,
    Empty,
    Input,
    InputNumber,
    Modal,
    Popconfirm,
    Select,
    Space,
    Switch,
    Table,
    Tag,
    message,
} from 'antd';
import React, { useState } from 'react';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import type {
    Automation,
    AutomationAction,
    AutomationActionType,
    AutomationPreview,
    AutomationRun,
    AutomationSpec,
} from '../api/types';
import { datasetName, describeChange } from './BulkEdit';
import { Muted } from './tokens';

const ACTIONS: { value: AutomationActionType; label: string; placeholder: string }[] = [
    { value: 'set_domain', label: 'Set domain', placeholder: 'finance' },
    { value: 'add_owner', label: 'Add owner', placeholder: 'username or urn:li:corpGroup:…' },
    { value: 'add_tag', label: 'Add tag', placeholder: 'Confidential' },
    { value: 'add_term', label: 'Add glossary term', placeholder: 'Finance.Revenue' },
    { value: 'notify', label: 'Notify', placeholder: 'usernames, comma separated' },
    { value: 'ai_steward', label: 'Queue AI suggestions', placeholder: 'owner, classification, description: reviewed' },
];

export const TRIGGERS: { value: string; label: string }[] = [
    { value: 'schedule', label: 'On a schedule' },
    { value: 'dataset.created', label: 'A dataset appears' },
    { value: 'schema.changed', label: 'Its schema changes' },
    { value: 'pii.detected', label: 'Personal data is detected' },
    { value: 'tags.changed', label: 'Its tags change' },
    { value: 'terms.changed', label: 'Its glossary terms change' },
    { value: 'owners.changed', label: 'Its owners change' },
    { value: 'description.changed', label: 'Its description changes' },
    { value: 'quality.incident.raised', label: 'A quality incident is raised' },
    { value: 'contract.broken', label: 'Its contract breaks' },
    { value: 'asset.deprecated', label: 'It is deprecated' },
    { value: 'access.requested', label: 'Access is requested' },
    { value: 'certification.decided', label: 'Certification is decided' },
];

export interface AutomationDraft {
    name: string;
    nameContains: string;
    platform: string;
    domain: string;
    classification: string;
    tag: string;
    advanced: string;
    everyMinutes: number;
    triggers?: string[];
    actions: { type: AutomationActionType; value: string }[];
}

const EMPTY: AutomationDraft = {
    name: '',
    nameContains: '',
    platform: '',
    domain: '',
    classification: '',
    tag: '',
    advanced: '',
    everyMinutes: 60,
    triggers: ['schedule'],
    actions: [{ type: 'set_domain', value: '' }],
};

/** The builder's fields as one policy condition; the advanced JSON, when given, wins. */
export function draftToAutomation(d: AutomationDraft): AutomationSpec {
    let select: Record<string, unknown>;
    if (d.advanced.trim()) {
        select = JSON.parse(d.advanced);
    } else {
        const all: Record<string, unknown>[] = [];
        if (d.nameContains.trim()) all.push({ attr: 'resource.urn', op: 'contains', value: d.nameContains.trim() });
        if (d.platform.trim()) all.push({ attr: 'resource.platform', op: 'eq', value: d.platform.trim() });
        if (d.domain.trim())
            all.push({ attr: 'resource.domains', op: 'contains', value: `urn:li:domain:${d.domain.trim()}` });
        if (d.classification.trim())
            all.push({ attr: 'resource.classification', op: 'contains', value: d.classification.trim() });
        if (d.tag.trim()) all.push({ attr: 'resource.tags', op: 'contains', value: `urn:li:tag:${d.tag.trim()}` });
        select = all.length === 1 ? all[0] : { all };
    }
    const actions: AutomationAction[] = d.actions
        .filter((a) => a.value.trim() || a.type === 'ai_steward')
        .map((a) =>
            a.type === 'ai_steward'
                ? { type: 'ai_steward' }
                : a.type === 'notify'
                ? {
                      type: 'notify',
                      recipients: a.value
                          .split(',')
                          .map((r) => r.trim())
                          .filter(Boolean),
                  }
                : { type: a.type, value: a.value.trim() },
        );
    const triggers = d.triggers?.length ? d.triggers : ['schedule'];
    return { name: d.name.trim(), select, triggers, every_minutes: d.everyMinutes, actions };
}

function Builder({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: () => void }) {
    const { client } = useConsumerApi();
    const [d, setD] = useState<AutomationDraft>(EMPTY);
    const [preview, setPreview] = useState<AutomationPreview | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [busy, setBusy] = useState(false);
    const set = (patch: Partial<AutomationDraft>) => {
        setD({ ...d, ...patch });
        setPreview(null);
    };
    const spec = (): AutomationSpec | null => {
        try {
            return draftToAutomation(d);
        } catch {
            setError('The advanced condition is not valid JSON');
            return null;
        }
    };
    const run = async (fn: (s: AutomationSpec) => Promise<void>) => {
        const s = spec();
        if (!s) return;
        setBusy(true);
        setError(null);
        try {
            await fn(s);
        } catch (e) {
            setError((e as Error).message);
        } finally {
            setBusy(false);
        }
    };
    const selector = (
        [
            ['nameContains', 'Name contains', 'finance.'],
            ['platform', 'Platform', 'snowflake'],
            ['domain', 'Domain', 'finance'],
            ['classification', 'Classification', 'PII'],
            ['tag', 'Has tag', 'gold'],
        ] as const
    ).map(([key, label, ph]) => (
        <Input
            key={key}
            aria-label={label}
            addonBefore={label}
            placeholder={ph}
            value={d[key]}
            onChange={(e) => set({ [key]: e.target.value })}
        />
    ));
    return (
        <Modal
            open={open}
            width={820}
            title="New automation"
            onCancel={onClose}
            footer={
                <Space>
                    <Button
                        onClick={() => void run(async (s) => setPreview(await client.previewAutomation(s)))}
                        loading={busy}
                    >
                        Preview matches
                    </Button>
                    <Button
                        type="primary"
                        disabled={!d.name.trim()}
                        loading={busy}
                        onClick={() =>
                            void run(async (s) => {
                                await client.createAutomation(s);
                                message.success(`Created “${s.name}”`);
                                setD(EMPTY);
                                setPreview(null);
                                onCreated();
                            })
                        }
                    >
                        Create automation
                    </Button>
                </Space>
            }
        >
            {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
            <Space direction="vertical" style={{ width: '100%' }}>
                <Input
                    aria-label="Automation name"
                    placeholder="Every finance table gets the Finance domain"
                    value={d.name}
                    onChange={(e) => set({ name: e.target.value })}
                />
                <h4 style={{ margin: '8px 0 0' }}>Which datasets</h4>
                {selector}
                <Input.TextArea
                    aria-label="Advanced condition"
                    rows={2}
                    placeholder='Advanced: a policy condition, e.g. {"attr": "resource.platform", "op": "in", "value": ["snowflake", "bigquery"]}'
                    value={d.advanced}
                    onChange={(e) => set({ advanced: e.target.value })}
                    style={{ fontFamily: 'monospace', fontSize: 12 }}
                />
                <h4 style={{ margin: '8px 0 0' }}>When</h4>
                <Select
                    aria-label="Triggers"
                    mode="multiple"
                    style={{ width: '100%' }}
                    value={d.triggers ?? ['schedule']}
                    options={TRIGGERS}
                    onChange={(triggers) => set({ triggers })}
                />
                <Muted>
                    Events run the automation on the one dataset they are about. Changes made by automations never
                    trigger automations.
                </Muted>
                <h4 style={{ margin: '8px 0 0' }}>Then</h4>
                {d.actions.map((a, i) => (
                    // eslint-disable-next-line react/no-array-index-key
                    <Space key={i} style={{ width: '100%' }}>
                        <Select
                            aria-label={`Action ${i + 1}`}
                            style={{ width: 180 }}
                            value={a.type}
                            options={ACTIONS.map(({ value, label }) => ({ value, label }))}
                            onChange={(type) =>
                                set({ actions: d.actions.map((x, j) => (j === i ? { ...x, type } : x)) })
                            }
                        />
                        <Input
                            aria-label={`Action ${i + 1} value`}
                            style={{ width: 360 }}
                            placeholder={ACTIONS.find((x) => x.value === a.type)?.placeholder}
                            value={a.value}
                            onChange={(e) =>
                                set({
                                    actions: d.actions.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)),
                                })
                            }
                        />
                        {d.actions.length > 1 && (
                            <Button type="text" onClick={() => set({ actions: d.actions.filter((_, j) => j !== i) })}>
                                Remove
                            </Button>
                        )}
                    </Space>
                ))}
                <Space>
                    <Button
                        size="small"
                        onClick={() => set({ actions: [...d.actions, { type: 'add_tag', value: '' }] })}
                    >
                        Add action
                    </Button>
                    <span>every</span>
                    <InputNumber
                        aria-label="Every minutes"
                        min={10}
                        max={10080}
                        value={d.everyMinutes}
                        onChange={(v) => set({ everyMinutes: Number(v) || 60 })}
                    />
                    <span>minutes</span>
                </Space>
                {preview && (
                    <>
                        <p style={{ marginTop: 12 }}>
                            {preview.matched} dataset(s) match{preview.truncated ? ' (more than the limit)' : ''}.{' '}
                            {preview.would_change} would change now; the rest already comply.
                        </p>
                        {preview.changes.length > 0 && (
                            <Table
                                size="small"
                                rowKey={(c) => `${c.urn}|${c.what}`}
                                dataSource={preview.changes}
                                pagination={
                                    preview.changes.length > 8 ? { pageSize: 8, showSizeChanger: false } : false
                                }
                                columns={[
                                    { title: 'Dataset', render: (_, c) => datasetName(c.urn) },
                                    { title: 'Change', render: (_, c) => describeChange(c) },
                                ]}
                            />
                        )}
                    </>
                )}
            </Space>
        </Modal>
    );
}

function RunsDrawer({ automation, onClose }: { automation: Automation | null; onClose: () => void }) {
    const { client, cache } = useConsumerApi();
    const key = automation ? `/governance/automations/${automation.id}/runs` : null;
    const runs = useDheQuery<AutomationRun[]>(key, (c, signal) => c.automationRuns(automation!.id, { signal }));
    const undo = async (r: AutomationRun) => {
        try {
            await client.undoAutomationRun(r.id);
            message.success('Run undone');
            if (key) cache.invalidate(key);
            cache.invalidate('/governance/automations');
        } catch (e) {
            message.error((e as Error).message);
        }
    };
    return (
        <Drawer
            open={!!automation}
            width={720}
            title={automation ? `Runs of “${automation.name}”` : ''}
            onClose={onClose}
        >
            {automation && (
                <Muted style={{ display: 'block', marginBottom: 8 }}>Changes are made as {automation.identity}.</Muted>
            )}
            <Table<AutomationRun>
                size="small"
                rowKey="id"
                dataSource={runs.data ?? []}
                loading={runs.loading}
                columns={[
                    { title: 'When', dataIndex: 'started_ms', render: (ms: number) => new Date(ms).toLocaleString() },
                    { title: 'Trigger', dataIndex: 'trigger' },
                    { title: 'Matched', dataIndex: 'matched' },
                    { title: 'Changed', dataIndex: 'changed' },
                    {
                        title: 'State',
                        render: (_, r) => (
                            <Tag
                                color={r.state === 'APPLIED' ? 'green' : r.state === 'ERROR' ? 'red' : 'default'}
                                title={r.detail.error}
                            >
                                {r.state}
                            </Tag>
                        ),
                    },
                    {
                        title: '',
                        render: (_, r) =>
                            r.state === 'APPLIED' && (
                                <Popconfirm
                                    title="Restore what this run changed?"
                                    okText="Undo"
                                    onConfirm={() => void undo(r)}
                                >
                                    <Button size="small">Undo</Button>
                                </Popconfirm>
                            ),
                    },
                ]}
            />
        </Drawer>
    );
}

/** Governance studio: rules that keep metadata true across every matching dataset. */
export function AutomationsAdmin() {
    const { client, cache } = useConsumerApi();
    const list = useDheQuery<Automation[]>('/governance/automations', (c, signal) => c.listAutomations({ signal }));
    const [building, setBuilding] = useState(false);
    const [viewing, setViewing] = useState<Automation | null>(null);
    const refresh = () => cache.invalidate('/governance/automations');
    const act = async (fn: () => Promise<unknown>, ok: string) => {
        try {
            await fn();
            message.success(ok);
            refresh();
        } catch (e) {
            message.error((e as Error).message);
        }
    };
    return (
        <>
            <Space style={{ marginBottom: 12 }}>
                <Button type="primary" onClick={() => setBuilding(true)}>
                    New automation
                </Button>
                <Muted>
                    Selected datasets get the domain, owners, tags or terms you choose: now, and as new ones appear.
                </Muted>
            </Space>
            {list.error && <Alert type="error" message={list.error.message} />}
            {list.data && !list.data.length && <Empty description="No automations yet" />}
            {list.data && list.data.length > 0 && (
                <Table<Automation>
                    size="small"
                    rowKey="id"
                    dataSource={list.data}
                    pagination={false}
                    columns={[
                        { title: 'Name', dataIndex: 'name' },
                        {
                            title: 'Does',
                            render: (_, a) =>
                                a.spec.actions
                                    .map(
                                        (x) =>
                                            `${ACTIONS.find((o) => o.value === x.type)?.label} ${x.value ? x.value.split(':').pop() : (x.recipients ?? []).map((r) => r.split(':').pop()).join(', ')}`,
                                    )
                                    .join(' · '),
                        },
                        {
                            title: 'When',
                            render: (_, a) =>
                                (a.spec.triggers ?? ['schedule'])
                                    .map((x) =>
                                        x === 'schedule'
                                            ? `every ${a.spec.every_minutes} min`
                                            : (TRIGGERS.find((o) => o.value === x)?.label ?? x).toLowerCase(),
                                    )
                                    .join(', '),
                        },
                        {
                            title: 'Last run',
                            render: (_, a) =>
                                a.last_run ? (
                                    `${new Date(a.last_run.started_ms).toLocaleString()}: ${a.last_run.changed} changed`
                                ) : (
                                    <Muted>never</Muted>
                                ),
                        },
                        {
                            title: 'On',
                            render: (_, a) => (
                                <Switch
                                    aria-label={`Enable ${a.name}`}
                                    checked={a.enabled}
                                    onChange={(on) =>
                                        void act(() => client.setAutomationEnabled(a.id, on), on ? 'Enabled' : 'Paused')
                                    }
                                />
                            ),
                        },
                        {
                            title: '',
                            render: (_, a) => (
                                <Space>
                                    <Button
                                        size="small"
                                        onClick={() => void act(() => client.runAutomation(a.id), 'Ran')}
                                    >
                                        Run now
                                    </Button>
                                    <Button size="small" onClick={() => setViewing(a)}>
                                        Runs
                                    </Button>
                                    <Popconfirm
                                        title="Delete this automation? What it changed stays."
                                        onConfirm={() => void act(() => client.deleteAutomation(a.id), 'Deleted')}
                                    >
                                        <Button size="small" danger>
                                            Delete
                                        </Button>
                                    </Popconfirm>
                                </Space>
                            ),
                        },
                    ]}
                />
            )}
            <Builder
                open={building}
                onClose={() => setBuilding(false)}
                onCreated={() => {
                    setBuilding(false);
                    refresh();
                }}
            />
            <RunsDrawer automation={viewing} onClose={() => setViewing(null)} />
        </>
    );
}
