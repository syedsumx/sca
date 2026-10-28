import { Alert, Button, Checkbox, Drawer, Input, InputNumber, Modal, Progress, Radio, Select, Space, Table, Tag, message } from 'antd';
import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';

import { useConsumerApi, useDheQuery } from '../api/hooks';
import type {
    FormAssignResult,
    FormProgress,
    FormQuestion,
    FormQuestionType,
    FormSpec,
    FormSummary,
    FormTask,
    FormTaskDetail,
} from '../api/types';
import { actorName, shortUrn } from '../utils/format';
import { assetPath } from './Assets';
import { Muted } from './tokens';

const label = (urn: string) => (urn.startsWith('urn:li:corp') ? actorName(urn) : urn.split(':').pop() ?? urn);

/** antd swaps 100% for a check icon; always show the number. */
const pct = (p?: number) => `${Math.round(p ?? 0)}%`;

/** Users type usernames; the API wants corpuser urns. */
export function toUserUrn(v: string): string {
    const t = v.trim();
    return t.startsWith('urn:li:corp') ? t : `urn:li:corpuser:${t}`;
}

// ------------------------------------------------------------------ completing a task
function QuestionInput({ q, value, onChange }: { q: FormQuestion; value: unknown; onChange: (v: unknown) => void }) {
    const list = Array.isArray(value) ? (value as string[]) : value ? [String(value)] : [];
    if (q.type === 'description' || q.type === 'text')
        return <Input.TextArea aria-label={q.title} rows={3} value={(value as string) ?? ''} onChange={(e) => onChange(e.target.value)} />;
    if (q.type === 'yes_no')
        return (
            <Radio.Group aria-label={q.title} value={value} onChange={(e) => onChange(e.target.value)}>
                <Radio value>Yes</Radio>
                <Radio value={false}>No</Radio>
            </Radio.Group>
        );
    const single = !q.multiple || q.type === 'domain';
    const options = q.allowed.map((a) => ({ value: a, label: label(a) }));
    return (
        <Select
            aria-label={q.title}
            style={{ width: '100%' }}
            mode={q.allowed.length ? (single ? undefined : 'multiple') : 'tags'}
            allowClear
            options={options}
            value={single && q.allowed.length ? (list[0] ?? undefined) : list}
            placeholder={q.type === 'owners' ? 'Usernames, e.g. jane.doe' : 'Pick or type'}
            onChange={(v: string | string[]) => {
                const values = (Array.isArray(v) ? v : v ? [v] : []).map((x) => (q.type === 'owners' ? toUserUrn(x) : x));
                onChange(single ? (values[0] ?? null) : values);
            }}
        />
    );
}

export function FormTaskDrawer({ task, onClose, onDone }: { task: FormTask | null; onClose: () => void; onDone: () => void }) {
    const { client } = useConsumerApi();
    const [detail, setDetail] = useState<FormTaskDetail | null>(null);
    const [answers, setAnswers] = useState<Record<string, unknown>>({});
    const [confirmed, setConfirmed] = useState(false);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);
    useEffect(() => {
        setDetail(null);
        setError(null);
        setConfirmed(false);
        if (!task) return;
        client.formTask(task.form_id, task.urn).then(
            (d) => {
                setDetail(d);
                setAnswers(d.answers ?? {});
            },
            (e) => setError((e as Error).message),
        );
    }, [client, task]);
    const submit = async () => {
        if (!task) return;
        setBusy(true);
        setError(null);
        try {
            const out = await client.submitFormTask(task.form_id, task.urn, answers, confirmed);
            message.success(out.state === 'VERIFIED' ? 'Answers saved and verified' : 'Answers saved to the catalog');
            onDone();
        } catch (e) {
            setError((e as Error).message);
        } finally {
            setBusy(false);
        }
    };
    return (
        <Drawer
            open={!!task}
            onClose={onClose}
            width={560}
            title={task ? `${task.form_name}: ${shortUrn(task.urn)}` : ''}
            destroyOnClose
            footer={
                detail && (
                    <Space style={{ justifyContent: 'space-between', width: '100%' }}>
                        {detail.kind === 'verification' ? (
                            <Checkbox checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)}>
                                I confirm these answers are correct
                            </Checkbox>
                        ) : (
                            <span />
                        )}
                        <Button type="primary" loading={busy} onClick={() => void submit()}>
                            {detail.kind === 'verification' && confirmed ? 'Submit and verify' : 'Submit'}
                        </Button>
                    </Space>
                )
            }
        >
            {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
            {!detail && !error && <Muted>Loading…</Muted>}
            {detail && (
                <>
                    <Muted style={{ display: 'block', marginBottom: 12 }}>
                        Current catalog values are filled in; confirm or change them. Answers are written to the catalog as
                        you. <Link to={assetPath(detail.urn)}>Open the asset</Link>
                    </Muted>
                    {detail.questions.map((q) => (
                        <div key={q.id} style={{ marginBottom: 16 }}>
                            <div style={{ fontWeight: 600, marginBottom: 4 }}>
                                {q.title}
                                {q.required && <span style={{ color: '#c62828' }}> *</span>}
                            </div>
                            {q.description && <Muted style={{ display: 'block', fontSize: 12, marginBottom: 4 }}>{q.description}</Muted>}
                            <QuestionInput q={q} value={answers[q.id]} onChange={(v) => setAnswers((a) => ({ ...a, [q.id]: v }))} />
                        </div>
                    ))}
                </>
            )}
        </Drawer>
    );
}

export function FormTasksList() {
    const { cache } = useConsumerApi();
    const tasks = useDheQuery<FormTask[]>('/governance/forms/tasks', (c, signal) => c.formTasks('PENDING', { signal }));
    const [open, setOpen] = useState<FormTask | null>(null);
    const now = Date.now();
    return (
        <>
            <Table<FormTask>
                rowKey={(t) => `${t.form_id}|${t.urn}`}
                loading={tasks.loading}
                dataSource={tasks.data ?? []}
                locale={{ emptyText: 'No forms waiting for you' }}
                pagination={{ pageSize: 20, hideOnSinglePage: true }}
                columns={[
                    { title: 'Form', dataIndex: 'form_name' },
                    {
                        title: 'Asset',
                        dataIndex: 'urn',
                        render: (u: string) => <Link to={assetPath(u)}>{shortUrn(u)}</Link>,
                    },
                    {
                        title: 'Due',
                        dataIndex: 'due_ms',
                        render: (d: number | null) =>
                            d ? <Tag color={d < now ? 'red' : 'default'}>{new Date(d).toLocaleDateString()}</Tag> : <Muted>—</Muted>,
                    },
                    {
                        title: '',
                        render: (_: unknown, t) => (
                            <Button size="small" type="primary" onClick={() => setOpen(t)}>
                                Complete
                            </Button>
                        ),
                    },
                ]}
            />
            <FormTaskDrawer
                task={open}
                onClose={() => setOpen(null)}
                onDone={() => {
                    setOpen(null);
                    cache.invalidate('/governance/forms');
                    cache.invalidate('/collab/enrich');
                }}
            />
        </>
    );
}

// ------------------------------------------------------------------ managing forms
const QUESTION_TYPES: { value: FormQuestionType; label: string }[] = [
    { value: 'description', label: 'Description' },
    { value: 'owners', label: 'Owners' },
    { value: 'glossary_terms', label: 'Glossary terms' },
    { value: 'tags', label: 'Tags' },
    { value: 'domain', label: 'Domain' },
    { value: 'structured_property', label: 'Structured property' },
    { value: 'text', label: 'Free text (kept in Cairn)' },
    { value: 'yes_no', label: 'Yes / no (kept in Cairn)' },
];

export interface FormDraft {
    name: string;
    kind: 'completion' | 'verification';
    dueDays: number | null;
    classification: string;
    domain: string;
    platform: string;
    tag: string;
    ownershipTypes: string[];
    fallbackUsers: string[];
    questions: FormQuestion[];
}

/** The simple selector fields become one policy condition (all must hold). */
export function draftToSpec(d: FormDraft): FormSpec {
    const all: Record<string, unknown>[] = [];
    if (d.classification) all.push({ attr: 'resource.classification', op: 'contains', value: d.classification });
    if (d.domain) all.push({ attr: 'resource.domains', op: 'contains', value: d.domain.startsWith('urn:') ? d.domain : `urn:li:domain:${d.domain}` });
    if (d.platform) all.push({ attr: 'resource.platform', op: 'eq', value: d.platform });
    if (d.tag) all.push({ attr: 'resource.tags', op: 'contains', value: d.tag.startsWith('urn:') ? d.tag : `urn:li:tag:${d.tag}` });
    return {
        name: d.name.trim(),
        kind: d.kind,
        due_days: d.dueDays,
        select: all.length ? { all } : { attr: 'resource.type', op: 'eq', value: 'dataset' },
        assignees: { owners: true, ownership_types: d.ownershipTypes, users: d.fallbackUsers.map(toUserUrn) },
        questions: d.questions.map((q, i) => ({ ...q, id: q.id || `q${i + 1}` })),
    };
}

const EMPTY_DRAFT: FormDraft = {
    name: '',
    kind: 'completion',
    dueDays: 14,
    classification: '',
    domain: '',
    platform: '',
    tag: '',
    ownershipTypes: [],
    fallbackUsers: [],
    questions: [
        { id: 'description', type: 'description', title: 'What is in this dataset?', required: true, allowed: [], multiple: true },
        { id: 'owner', type: 'owners', title: 'Who is accountable for it?', required: true, allowed: [], multiple: true, ownership_type: 'DATAOWNER' },
    ],
};

function FormBuilder({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: () => void }) {
    const { client } = useConsumerApi();
    const [d, setD] = useState<FormDraft>(EMPTY_DRAFT);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const setQ = (i: number, patch: Partial<FormQuestion>) =>
        setD({ ...d, questions: d.questions.map((q, j) => (j === i ? { ...q, ...patch } : q)) });
    const create = async () => {
        setBusy(true);
        setError(null);
        try {
            await client.createForm(draftToSpec(d));
            message.success(`Created “${d.name}”`);
            setD(EMPTY_DRAFT);
            onCreated();
        } catch (e) {
            setError((e as Error).message);
        } finally {
            setBusy(false);
        }
    };
    return (
        <Modal open={open} onCancel={onClose} onOk={() => void create()} okText="Create form" confirmLoading={busy} title="New compliance form" width={760} okButtonProps={{ disabled: !d.name.trim() || !d.questions.length }}>
            {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
            <Space direction="vertical" style={{ width: '100%' }}>
                <Input aria-label="Form name" placeholder="Form name, e.g. PII ownership and retention" value={d.name} onChange={(e) => setD({ ...d, name: e.target.value })} />
                <Space wrap>
                    <Radio.Group value={d.kind} onChange={(e) => setD({ ...d, kind: e.target.value })}>
                        <Radio value="completion">Completion</Radio>
                        <Radio value="verification">Completion + sign-off</Radio>
                    </Radio.Group>
                    <span>
                        Due in <InputNumber aria-label="Due days" min={1} max={365} value={d.dueDays ?? undefined} onChange={(v) => setD({ ...d, dueDays: v ? Number(v) : null })} /> days
                    </span>
                </Space>
                <strong>Which datasets</strong>
                <Space wrap>
                    <Input aria-label="Classification" style={{ width: 160 }} placeholder="Classification, e.g. PII" value={d.classification} onChange={(e) => setD({ ...d, classification: e.target.value })} />
                    <Input aria-label="Domain" style={{ width: 160 }} placeholder="Domain id" value={d.domain} onChange={(e) => setD({ ...d, domain: e.target.value })} />
                    <Input aria-label="Platform" style={{ width: 140 }} placeholder="Platform" value={d.platform} onChange={(e) => setD({ ...d, platform: e.target.value })} />
                    <Input aria-label="Tag" style={{ width: 140 }} placeholder="Tag" value={d.tag} onChange={(e) => setD({ ...d, tag: e.target.value })} />
                </Space>
                <strong>Who completes it</strong>
                <Space wrap>
                    <Select
                        aria-label="Ownership types"
                        mode="multiple"
                        style={{ minWidth: 260 }}
                        placeholder="Owners of the asset (all types)"
                        value={d.ownershipTypes}
                        onChange={(v) => setD({ ...d, ownershipTypes: v })}
                        options={['DATAOWNER', 'TECHNICAL_OWNER', 'BUSINESS_OWNER', 'DATA_STEWARD'].map((t) => ({ value: t, label: t.replace('_', ' ').toLowerCase() }))}
                    />
                    <Select aria-label="Fallback people" mode="tags" style={{ minWidth: 260 }} placeholder="When an asset has no such owner: usernames" value={d.fallbackUsers} onChange={(v) => setD({ ...d, fallbackUsers: v })} />
                </Space>
                <strong>Questions</strong>
                {d.questions.map((q, i) => (
                    <Space key={i} wrap align="start" style={{ width: '100%' }}>
                        <Select aria-label={`Question ${i + 1} type`} style={{ width: 200 }} value={q.type} options={QUESTION_TYPES} onChange={(v) => setQ(i, { type: v })} />
                        <Input aria-label={`Question ${i + 1} title`} style={{ width: 260 }} value={q.title} onChange={(e) => setQ(i, { title: e.target.value })} />
                        {q.type === 'structured_property' && (
                            <Input aria-label={`Question ${i + 1} property`} style={{ width: 240 }} placeholder="urn:li:structuredProperty:…" value={q.property_urn ?? ''} onChange={(e) => setQ(i, { property_urn: e.target.value })} />
                        )}
                        {['glossary_terms', 'tags', 'domain', 'structured_property'].includes(q.type) && (
                            <Select aria-label={`Question ${i + 1} choices`} mode="tags" style={{ width: 240 }} placeholder="Allowed choices (optional)" value={q.allowed} onChange={(v) => setQ(i, { allowed: v })} />
                        )}
                        <Checkbox checked={q.required} onChange={(e) => setQ(i, { required: e.target.checked })}>
                            Required
                        </Checkbox>
                        <Button size="small" onClick={() => setD({ ...d, questions: d.questions.filter((_, j) => j !== i) })}>
                            Remove
                        </Button>
                    </Space>
                ))}
                <Button onClick={() => setD({ ...d, questions: [...d.questions, { id: '', type: 'text', title: '', required: false, allowed: [], multiple: true }] })}>
                    Add question
                </Button>
            </Space>
        </Modal>
    );
}

function ProgressDrawer({ form, onClose }: { form: FormSummary | null; onClose: () => void }) {
    const progress = useDheQuery<FormProgress>(form ? `/governance/forms/${form.id}/progress` : null, (c, signal) => c.formProgress(form!.id, { signal }));
    const rows = (m?: Record<string, FormProgress['total']>) => Object.entries(m ?? {}).map(([k, v]) => ({ key: k, ...v }));
    const cols = (title: string) => [
        { title, dataIndex: 'key', render: (k: string) => label(k) },
        { title: 'Assets', dataIndex: 'assets' },
        { title: 'Open', dataIndex: 'PENDING' },
        { title: 'Done', dataIndex: 'done_pct', render: (p: number) => <Progress percent={p} format={pct} size="small" /> },
    ];
    return (
        <Drawer open={!!form} onClose={onClose} width={640} title={form ? `Progress: ${form.name}` : ''} destroyOnClose>
            {progress.data && (
                <>
                    <Progress percent={progress.data.total.done_pct} format={pct} />
                    <Table size="small" pagination={false} dataSource={rows(progress.data.by_domain)} columns={cols('Domain')} />
                    <div style={{ height: 16 }} />
                    <Table size="small" pagination={false} dataSource={rows(progress.data.by_assignee)} columns={cols('Assignee')} />
                </>
            )}
        </Drawer>
    );
}

export function FormsAdmin() {
    const { client, cache } = useConsumerApi();
    const forms = useDheQuery<FormSummary[]>('/governance/forms', (c, signal) => c.listForms({ signal }));
    const [building, setBuilding] = useState(false);
    const [viewing, setViewing] = useState<FormSummary | null>(null);
    const [preview, setPreview] = useState<{ form: FormSummary; result: FormAssignResult } | null>(null);
    const refresh = () => cache.invalidate('/governance/forms');
    const assign = async (f: FormSummary, dryRun: boolean) => {
        try {
            const result = await client.assignForm(f.id, dryRun);
            if (dryRun) setPreview({ form: f, result });
            else {
                message.success(`${result.assigned.length} new task(s); ${result.withdrawn.length} withdrawn`);
                setPreview(null);
                refresh();
            }
        } catch (e) {
            message.error((e as Error).message);
        }
    };
    if (forms.error && /403|forbidden|denied/i.test(forms.error.message))
        return <Alert type="info" message="Managing compliance forms needs the Manage Global Settings privilege." />;
    return (
        <>
            <Space style={{ marginBottom: 12 }}>
                <Button type="primary" onClick={() => setBuilding(true)}>
                    New form
                </Button>
                <Muted>Forms assign required metadata to every matching dataset; new matches are picked up hourly.</Muted>
            </Space>
            <Table<FormSummary>
                rowKey="id"
                loading={forms.loading}
                dataSource={forms.data ?? []}
                locale={{ emptyText: 'No compliance forms yet' }}
                columns={[
                    { title: 'Form', dataIndex: 'name' },
                    { title: 'Kind', render: (_: unknown, f) => (f.spec.kind === 'verification' ? 'Sign-off' : 'Completion') },
                    { title: 'Questions', render: (_: unknown, f) => f.spec.questions.length },
                    { title: 'Assets', render: (_: unknown, f) => f.progress.assets },
                    { title: 'Done', width: 160, render: (_: unknown, f) => <Progress percent={f.progress.done_pct} format={pct} size="small" /> },
                    {
                        title: '',
                        render: (_: unknown, f) => (
                            <Space>
                                <Button size="small" onClick={() => void assign(f, true)}>
                                    Assign…
                                </Button>
                                <Button size="small" onClick={() => setViewing(f)}>
                                    Progress
                                </Button>
                                <Button size="small" danger onClick={() => void client.archiveForm(f.id).then(refresh)}>
                                    Archive
                                </Button>
                            </Space>
                        ),
                    },
                ]}
            />
            <FormBuilder open={building} onClose={() => setBuilding(false)} onCreated={() => { setBuilding(false); refresh(); }} />
            <ProgressDrawer form={viewing} onClose={() => setViewing(null)} />
            <Modal open={!!preview} title={preview ? `Assign “${preview.form.name}”` : ''} okText="Assign" onOk={() => preview && void assign(preview.form, false)} onCancel={() => setPreview(null)}>
                {preview && (
                    <>
                        <p>
                            {preview.result.matched} dataset(s) match. {preview.result.assigned.length} will get a new task
                            {preview.result.withdrawn.length ? `; ${preview.result.withdrawn.length} untouched task(s) no longer match and will be withdrawn` : ''}.
                        </p>
                        {preview.result.without_assignee.length > 0 && (
                            <Alert type="warning" showIcon message={`${preview.result.without_assignee.length} dataset(s) have no matching owner and no fallback person.`} />
                        )}
                    </>
                )}
            </Modal>
        </>
    );
}
