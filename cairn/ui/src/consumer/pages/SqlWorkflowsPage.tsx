import { Alert, Button, Checkbox, Input, InputNumber, Modal, Popconfirm, Select, Space, Table, Tag, message } from 'antd';
import React, { useState } from 'react';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import type { DerivedTable, SavedQuery, SqlWorkflow, SqlWorkflowRun, SqlWorkflowStep } from '../api/types';
import { Muted, Page, Surface } from '../components/tokens';

const OPS = ['>', '>=', '<', '<=', '==', '!='];
const STATUS_COLOR: Record<string, string> = { ok: 'green', failed: 'red', error: 'red', skipped: 'default' };

type Draft = {
    id?: string;
    name: string;
    cron: string;
    notify: 'failure' | 'always';
    enabled: boolean;
    recipients: string;
    steps: SqlWorkflowStep[];
};

const blankStep = (n: number): SqlWorkflowStep => ({ name: `step-${n}`, type: 'query', after: [], retries: 0 });

export const toDraft = (w?: SqlWorkflow): Draft =>
    w
        ? { id: w.id, name: w.name, cron: w.cron ?? '', notify: w.notify, enabled: w.enabled, recipients: w.recipients.join(', '), steps: w.steps }
        : { name: '', cron: '', notify: 'failure', enabled: true, recipients: '', steps: [blankStep(1)] };

/** The body the API takes: only the fields each step type uses. */
export function toBody(d: Draft): Record<string, unknown> {
    return {
        name: d.name,
        cron: d.cron.trim() || null,
        notify: d.notify,
        enabled: d.enabled,
        recipients: d.recipients
            .split(',')
            .map((r) => r.trim())
            .filter(Boolean),
        steps: d.steps.map((s) => {
            const base = { name: s.name, type: s.type, after: s.after, retries: s.retries };
            if (s.type === 'derived') return { ...base, table: s.table, full: !!s.full };
            if (s.type === 'check') return { ...base, query: s.query, measure: s.measure || 'rows', op: s.op || '>', value: s.value ?? 0 };
            return { ...base, query: s.query };
        }),
    };
}

function StepEditor({
    step,
    others,
    queries,
    derived,
    onChange,
    onRemove,
}: {
    step: SqlWorkflowStep;
    others: string[];
    queries: SavedQuery[];
    derived: DerivedTable[];
    onChange: (s: SqlWorkflowStep) => void;
    onRemove?: () => void;
}) {
    const set = (patch: Partial<SqlWorkflowStep>) => onChange({ ...step, ...patch });
    const querySelect = (
        <Select
            aria-label={`${step.name} saved query`}
            placeholder="Saved query"
            style={{ width: 220 }}
            value={step.query}
            onChange={(v) => set({ query: v })}
            options={queries.map((q) => ({ value: q.id, label: q.name }))}
        />
    );
    return (
        <Surface style={{ marginBottom: 8 }}>
            <Space wrap>
                <Input aria-label="Step name" value={step.name} onChange={(e) => set({ name: e.target.value })} style={{ width: 140 }} />
                <Select
                    aria-label={`${step.name} type`}
                    value={step.type}
                    style={{ width: 150 }}
                    onChange={(v) => set({ type: v })}
                    options={[
                        { value: 'derived', label: 'Refresh a table' },
                        { value: 'check', label: 'Check' },
                        { value: 'query', label: 'Report' },
                    ]}
                />
                {step.type === 'derived' && (
                    <>
                        <Select
                            aria-label={`${step.name} derived table`}
                            placeholder="Derived table"
                            style={{ width: 220 }}
                            value={step.table}
                            onChange={(v) => set({ table: v })}
                            options={derived.map((d) => ({ value: d.name, label: d.name }))}
                        />
                        <Checkbox checked={!!step.full} onChange={(e) => set({ full: e.target.checked })}>
                            Full rebuild
                        </Checkbox>
                    </>
                )}
                {step.type === 'query' && querySelect}
                {step.type === 'check' && (
                    <>
                        {querySelect}
                        <Input
                            aria-label={`${step.name} measure`}
                            placeholder="rows or a column"
                            style={{ width: 140 }}
                            value={step.measure ?? 'rows'}
                            onChange={(e) => set({ measure: e.target.value })}
                        />
                        <Select
                            aria-label={`${step.name} op`}
                            style={{ width: 70 }}
                            value={step.op ?? '>'}
                            onChange={(v) => set({ op: v })}
                            options={OPS.map((o) => ({ value: o, label: o }))}
                        />
                        <InputNumber aria-label={`${step.name} value`} value={step.value ?? 0} onChange={(v) => set({ value: Number(v ?? 0) })} />
                    </>
                )}
                <Select
                    mode="multiple"
                    aria-label={`${step.name} after`}
                    placeholder="After"
                    style={{ minWidth: 140 }}
                    value={step.after}
                    onChange={(v) => set({ after: v })}
                    options={others.map((o) => ({ value: o, label: o }))}
                />
                <span>
                    Retries{' '}
                    <InputNumber aria-label={`${step.name} retries`} min={0} max={3} value={step.retries} onChange={(v) => set({ retries: Number(v ?? 0) })} />
                </span>
                {onRemove && (
                    <Button danger size="small" onClick={onRemove}>
                        Remove
                    </Button>
                )}
            </Space>
        </Surface>
    );
}

function Runs({ id, tick }: { id: string; tick: number }) {
    const runs = useDheQuery<SqlWorkflowRun[]>(`/v1/sql-workflows/${id}/runs#${tick}`, (c, signal) => c.sqlWorkflowRuns(id, { signal }));
    if (runs.error) return <Alert type="error" message={runs.error.message} />;
    return (
        <Table<SqlWorkflowRun>
            rowKey="id"
            size="small"
            pagination={{ pageSize: 10 }}
            dataSource={runs.data ?? []}
            locale={{ emptyText: 'Not run yet.' }}
            columns={[
                { title: 'Started', dataIndex: 'started_ms', render: (v: number) => new Date(v).toLocaleString() },
                { title: 'Trigger', dataIndex: 'trigger' },
                { title: 'Status', dataIndex: 'status', render: (s: string) => <Tag color={STATUS_COLOR[s]}>{s}</Tag> },
                {
                    title: 'Steps',
                    dataIndex: 'steps',
                    render: (steps: SqlWorkflowRun['steps']) => (
                        <Space direction="vertical" size={0}>
                            {steps.map((s) => (
                                <span key={s.name}>
                                    <Tag color={STATUS_COLOR[s.status]}>{s.status}</Tag>
                                    {s.name}
                                    {s.error ? <Muted> — {s.error}</Muted> : s.detail ? <Muted> — {s.detail}</Muted> : s.rows != null ? <Muted> — {s.rows} rows</Muted> : null}
                                </span>
                            ))}
                        </Space>
                    ),
                },
            ]}
        />
    );
}

/** SQL workflows: table refreshes, checks and reports that run in order on a schedule, as their owner. */
export function SqlWorkflowsPage() {
    const { client } = useConsumerApi();
    const [tick, setTick] = useState(0);
    const list = useDheQuery<SqlWorkflow[]>(`/v1/sql-workflows#${tick}`, (c, signal) => c.sqlWorkflows({ signal }));
    const queries = useDheQuery<SavedQuery[]>('/sql/saved', (c, signal) => c.savedQueries({ signal }));
    const derived = useDheQuery<DerivedTable[]>('/v1/derived', (c, signal) => c.derivedTables({ signal }).catch(() => []));
    const [draft, setDraft] = useState<Draft | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [selected, setSelected] = useState<string | null>(null);
    const [running, setRunning] = useState<string | null>(null);

    const save = async () => {
        if (!draft) return;
        setError(null);
        try {
            const w = await client.saveSqlWorkflow(toBody(draft), draft.id);
            setDraft(null);
            setSelected(w.id);
            setTick((t) => t + 1);
            message.success('Workflow saved');
        } catch (e) {
            setError((e as Error).message);
        }
    };

    const run = async (id: string) => {
        setRunning(id);
        try {
            const r = await client.runSqlWorkflow(id);
            if (r.status === 'ok') message.success('The run succeeded');
            else message.error('The run failed: see its steps');
        } catch (e) {
            message.error((e as Error).message);
        } finally {
            setRunning(null);
            setSelected(id);
            setTick((t) => t + 1);
        }
    };

    const remove = async (id: string) => {
        try {
            await client.deleteSqlWorkflow(id);
            if (selected === id) setSelected(null);
            setTick((t) => t + 1);
        } catch (e) {
            message.error((e as Error).message);
        }
    };

    const setStep = (i: number, s: SqlWorkflowStep) =>
        setDraft((d) => {
            if (!d) return d;
            const old = d.steps[i].name;
            // A rename carries over to the steps that come after it.
            const steps = d.steps.map((x, j) => (j === i ? s : { ...x, after: x.after.map((a) => (a === old ? s.name : a)) }));
            return { ...d, steps };
        });

    return (
        <Page>
            <h1 style={{ marginBottom: 0 }}>SQL workflows</h1>
            <Muted>
                Refresh tables, check them and send reports, in order and on a schedule. Each runs as you, so your access applies; a
                failed check stops what comes after and tells you.
            </Muted>
            <Surface style={{ marginTop: 16 }}>
                <Space style={{ marginBottom: 12 }}>
                    <Button type="primary" onClick={() => setDraft(toDraft())}>
                        New workflow
                    </Button>
                </Space>
                {list.error && <Alert type="error" message={list.error.message} />}
                <Table<SqlWorkflow>
                    rowKey="id"
                    size="small"
                    loading={list.loading}
                    dataSource={list.data ?? []}
                    locale={{ emptyText: 'No workflows yet.' }}
                    rowClassName={(r) => (r.id === selected ? 'ant-table-row-selected' : '')}
                    onRow={(r) => ({ onClick: () => setSelected(r.id), style: { cursor: 'pointer' } })}
                    columns={[
                        { title: 'Name', dataIndex: 'name' },
                        { title: 'Steps', dataIndex: 'steps', render: (s: SqlWorkflowStep[]) => s.map((x) => x.name).join(' → ') },
                        { title: 'Schedule (UTC)', dataIndex: 'cron', render: (c: string | null) => c ?? <Muted>on demand</Muted> },
                        {
                            title: 'Next run',
                            dataIndex: 'next_run_ms',
                            render: (v: number | null, r) => (r.enabled && v ? new Date(v).toLocaleString() : <Muted>—</Muted>),
                        },
                        {
                            title: 'Last run',
                            dataIndex: 'last_status',
                            render: (s: string | null) => (s ? <Tag color={STATUS_COLOR[s]}>{s}</Tag> : <Muted>never</Muted>),
                        },
                        {
                            title: '',
                            key: 'actions',
                            render: (_: unknown, r) => (
                                <Space onClick={(e) => e.stopPropagation()}>
                                    <Button size="small" loading={running === r.id} onClick={() => void run(r.id)}>
                                        Run now
                                    </Button>
                                    <Button size="small" onClick={() => setDraft(toDraft(r))}>
                                        Edit
                                    </Button>
                                    <Popconfirm title="Delete this workflow and its runs?" onConfirm={() => void remove(r.id)}>
                                        <Button size="small" danger>
                                            Delete
                                        </Button>
                                    </Popconfirm>
                                </Space>
                            ),
                        },
                    ]}
                />
            </Surface>
            {selected && (
                <Surface style={{ marginTop: 16 }}>
                    <h3>Runs of {list.data?.find((w) => w.id === selected)?.name}</h3>
                    <Runs id={selected} tick={tick} />
                </Surface>
            )}
            <Modal
                title={draft?.id ? 'Edit workflow' : 'New workflow'}
                open={!!draft}
                width={980}
                okText="Save"
                onOk={() => void save()}
                onCancel={() => {
                    setDraft(null);
                    setError(null);
                }}>
                {draft && (
                    <>
                        {error && <Alert type="error" message={error} style={{ marginBottom: 12 }} />}
                        <Space wrap style={{ marginBottom: 12 }}>
                            <Input aria-label="Workflow name" placeholder="Name" value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} style={{ width: 240 }} />
                            <Input
                                aria-label="Schedule"
                                placeholder="cron, UTC (e.g. 0 6 * * *); empty: on demand"
                                value={draft.cron}
                                onChange={(e) => setDraft({ ...draft, cron: e.target.value })}
                                style={{ width: 300 }}
                            />
                            <Select
                                aria-label="Notify"
                                value={draft.notify}
                                style={{ width: 170 }}
                                onChange={(v) => setDraft({ ...draft, notify: v })}
                                options={[
                                    { value: 'failure', label: 'Tell me on failure' },
                                    { value: 'always', label: 'Tell me every run' },
                                ]}
                            />
                            <Checkbox checked={draft.enabled} onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })}>
                                Enabled
                            </Checkbox>
                        </Space>
                        <Input
                            aria-label="Recipients"
                            placeholder="Also tell: urn:li:corpuser:…, urn:li:corpGroup:… (comma-separated)"
                            value={draft.recipients}
                            onChange={(e) => setDraft({ ...draft, recipients: e.target.value })}
                            style={{ marginBottom: 12 }}
                        />
                        {draft.steps.map((s, i) => (
                            <StepEditor
                                key={i}
                                step={s}
                                others={draft.steps.filter((_, j) => j !== i).map((x) => x.name)}
                                queries={queries.data ?? []}
                                derived={derived.data ?? []}
                                onChange={(x) => setStep(i, x)}
                                onRemove={
                                    draft.steps.length > 1
                                        ? () =>
                                              setDraft({
                                                  ...draft,
                                                  steps: draft.steps
                                                      .filter((_, j) => j !== i)
                                                      .map((x) => ({ ...x, after: x.after.filter((a) => a !== s.name) })),
                                              })
                                        : undefined
                                }
                            />
                        ))}
                        <Button onClick={() => setDraft({ ...draft, steps: [...draft.steps, blankStep(draft.steps.length + 1)] })}>Add a step</Button>
                    </>
                )}
            </Modal>
        </Page>
    );
}
